"""Админский десктоп-клиент «Чайного учёта»: таблица сортов.

Отдельный процесс: Django не загружается, данные берутся из REST API.
Карточная версия окна сохранена в ``desktop_admin.py`` как наработка
для клиентской (витринной) части. Здесь из неё переиспользуются только
транспортный слой (``TeaApi``, ``ApiError``), сборка сессии и словари
русских подписей — никакой карточной вёрстки этот модуль не трогает.

Запуск:
    .venv\\Scripts\\python.exe desktop_table.py
"""

import logging
import math
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk
import requests
from dotenv import load_dotenv

from desktop_admin import (
    BASE_URL,
    REGION_LABELS,
    TEA_TYPE_LABELS,
    ApiError,
    TeaApi,
    build_session,
    parse_datetime,
    plural_tea,
)

APP_TITLE = "Чайная лавка · администрирование"

# Сколько записей забираем у сервера за один запрос. Совпадает с
# Catalog.pagination.TeaPagination.max_page_size: сервер больше не отдаст.
FETCH_PAGE_SIZE = 500
# Сколько строк рисуем на одной странице таблицы. Это уже клиентская
# навигация по полностью загруженному набору, а не серверная пагинация.
ROWS_PER_PAGE = 50

SEARCH_DEBOUNCE_MS = 250
DRAIN_INTERVAL_MS = 16
REQUEST_TIMEOUT = 10.0

FONT = "Segoe UI"

COLOR_BG = "#F6F0E3"
COLOR_SURFACE = "#FDFAF2"
COLOR_TEXT = "#2A3122"
COLOR_MUTED = "#847D6B"
COLOR_BORDER = "#E7DDC9"
COLOR_ACCENT = "#5F7534"
COLOR_ACCENT_HOVER = "#4C5F29"
COLOR_CHIP = "#EDE6D6"
COLOR_CHIP_HOVER = "#E1D7C2"
COLOR_ROW_ALT = "#FBF5E8"
COLOR_DANGER = "#A23B2A"
COLOR_WARN = "#B9822E"

ENV_PATH = Path(__file__).resolve().parent / ".env"

LOGGER = logging.getLogger("desktop_table")

FILTER_OPTIONS = (
    ("Все", "all"),
    ("В наличии", "available"),
    ("Нет в наличии", "unavailable"),
    ("Новинки", "new"),
    ("Старые", "old"),
)

# (ключ поля, заголовок, ширина, выравнивание, растягивать ли колонку)
COLUMNS = (
    ("id", "ID", 56, "center", False),
    ("availability", "Наличие", 92, "center", False),
    ("name", "Название", 300, "w", True),
    ("tea_type", "Тип", 170, "w", False),
    ("place_of_harvest", "Регион", 140, "w", False),
    ("year_of_harvest", "Год", 64, "center", False),
    ("price", "Цена", 96, "e", False),
    ("mark", "Оценка", 84, "center", False),
    ("created_at", "Создано", 136, "center", False),
    ("photo", "Фото", 56, "center", False),
)


# ---------------------------------------------------------------------------
# Учётные данные и загрузка данных
# ---------------------------------------------------------------------------


def load_credentials() -> tuple[str, str]:
    """Читает TEA_API_USER / TEA_API_PASSWORD из .env или окружения."""
    load_dotenv(ENV_PATH)
    username = os.getenv("TEA_API_USER", "").strip()
    # Пароль не обрезаем: пробел может быть его частью.
    password = os.getenv("TEA_API_PASSWORD", "")
    if not username or not password:
        raise ValueError(
            "Не найдены TEA_API_USER / TEA_API_PASSWORD в .env или окружении."
        )
    return username, password


def fetch_all(
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT,
    page_size: int = FETCH_PAGE_SIZE,
) -> tuple[list[dict], int]:
    """Забирает весь каталог, по возможности одним запросом.

    Сервер поддерживает ``page_size`` до 500 (Catalog.pagination).
    Если записей больше, продолжаем по ``next_page``; повторяющиеся id
    отбрасываем — страницы могут сдвинуться, если данные правят параллельно.
    """
    api = TeaApi(session, timeout=timeout)
    items: list[dict] = []
    seen: set[int] = set()
    total = 0
    page_number = 1

    for _ in range(1000):
        page = api.list_teas(page_number, page_size=page_size)
        total = page.count
        for tea in page.items:
            tea_id = tea.get("id")
            if isinstance(tea_id, int):
                if tea_id in seen:
                    continue
                seen.add(tea_id)
            items.append(tea)
        if page.next_page is None or not page.items:
            return items, total
        if page.next_page == page_number:
            raise ApiError("Сервер вернул ссылку на ту же страницу.")
        page_number = page.next_page

    raise ApiError("Слишком много страниц: загрузка остановлена.")


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def format_price(value) -> str:
    number = _to_decimal(value)
    return "—" if number is None else f"{number:.2f}".replace(".", ",")


def format_mark(value) -> str:
    number = _to_decimal(value)
    return "—" if number is None else f"{number:.1f}".replace(".", ",")


def format_created(value) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%d.%m.%Y %H:%M") if parsed else "—"


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------


class TeaTableApp(ctk.CTk):
    """Главное окно админ-клиента: таблица сортов с фильтрами и сортировкой."""

    def __init__(self, session: requests.Session) -> None:
        super().__init__()
        self.session = session
        self._read_only = session.auth is None

        self.title(APP_TITLE)
        self.geometry("1240x780")
        self.minsize(980, 600)
        self.configure(fg_color=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Данные
        self._teas: list[dict] = []
        self._filtered: list[dict] = []
        self._haystack: dict[str, str] = {}
        self._total = 0
        self._fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
        self._old_before = datetime.now(timezone.utc) - timedelta(days=90)

        # Состояние представления
        self._filter_key = "all"
        self._active_query = ""
        self._page = 1
        self._sort_column = "id"
        self._sort_reverse = False
        self._selected_iid: str | None = None

        # Состояние загрузки
        self._busy = False
        self._load_token = 0
        self._closing = False
        self._search_job = None
        self._drain_job = None
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()

        self._build_ui()
        self._drain_job = self.after(DRAIN_INTERVAL_MS, self._drain_ui_queue)
        self.after(150, self.refresh)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_toolbar()
        self._build_table()
        self._build_footer()

        self.bind("<F5>", lambda _event: self.refresh(), add="+")
        self.bind("<Control-f>", lambda _event: self.search_entry.focus_set(), add="+")

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color=COLOR_SURFACE, corner_radius=0, height=64)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=(20, 16), pady=12, sticky="w")

        ctk.CTkLabel(
            brand,
            text="Чайная лавка",
            font=(FONT, 16, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            brand,
            text="администрирование каталога",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        ).grid(row=1, column=0, sticky="w")

        search_box = ctk.CTkFrame(header, fg_color="transparent")
        search_box.grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=14)
        search_box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            search_box,
            text="Поиск",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        ).grid(row=0, column=0, padx=(0, 8))

        self.search_entry = ctk.CTkEntry(
            search_box,
            placeholder_text="название, тип, регион, магазин",
            height=36,
            corner_radius=18,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_BG,
            text_color=COLOR_TEXT,
            placeholder_text_color=COLOR_MUTED,
            font=(FONT, 12),
        )
        self.search_entry.grid(row=0, column=1, sticky="ew")
        # Без textvariable: CTkEntry показывает placeholder только тогда,
        # когда переменная не задана. Ловим и набор, и вставку.
        self.search_entry.bind("<KeyRelease>", self._on_search_changed)
        self.search_entry.bind("<Return>", lambda _event: self._apply_search())

        self.refresh_button = ctk.CTkButton(
            header,
            text="Обновить (F5)",
            command=self.refresh,
            width=150,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 12, "bold"),
        )
        self.refresh_button.grid(row=0, column=2, padx=(0, 20), pady=14)

    def _build_toolbar(self) -> None:
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=16, pady=(12, 8))
        toolbar.grid_columnconfigure(len(FILTER_OPTIONS), weight=1)

        self.filter_buttons: dict[str, ctk.CTkButton] = {}
        for index, (label, key) in enumerate(FILTER_OPTIONS):
            button = ctk.CTkButton(
                toolbar,
                text=label,
                command=lambda value=key: self._set_filter(value),
                width=max(58, int(len(label) * 7.4) + 22),
                height=30,
                corner_radius=15,
                fg_color=COLOR_CHIP,
                hover_color=COLOR_CHIP_HOVER,
                text_color=COLOR_TEXT,
                font=(FONT, 11),
            )
            button.grid(row=0, column=index, padx=(0, 6), sticky="w")
            self.filter_buttons[key] = button

        pager = ctk.CTkFrame(toolbar, fg_color="transparent")
        pager.grid(row=0, column=len(FILTER_OPTIONS) + 1, sticky="e")

        self.prev_button = ctk.CTkButton(
            pager,
            text="‹",
            width=34,
            height=30,
            corner_radius=10,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 14, "bold"),
            command=self._on_prev,
            state="disabled",
        )
        self.prev_button.grid(row=0, column=0, padx=(0, 4))

        self.page_label = ctk.CTkLabel(
            pager,
            text="Стр. 1 из 1",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            width=96,
        )
        self.page_label.grid(row=0, column=1)

        self.next_button = ctk.CTkButton(
            pager,
            text="›",
            width=34,
            height=30,
            corner_radius=10,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 14, "bold"),
            command=self._on_next,
            state="disabled",
        )
        self.next_button.grid(row=0, column=2, padx=(4, 12))

        self.count_label = ctk.CTkLabel(
            pager,
            text="0 из 0",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            width=110,
            anchor="e",
        )
        self.count_label.grid(row=0, column=3)

    def _build_table(self) -> None:
        wrapper = ctk.CTkFrame(
            self,
            fg_color=COLOR_SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        wrapper.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 12))
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(0, weight=1)

        style = ttk.Style(self)
        # На Windows тема по умолчанию (vista) игнорирует часть настроек
        # Treeview; clam ведёт себя предсказуемо на всех платформах.
        style.theme_use("clam")
        style.configure(
            "Admin.Treeview",
            background=COLOR_SURFACE,
            fieldbackground=COLOR_SURFACE,
            foreground=COLOR_TEXT,
            rowheight=30,
            borderwidth=0,
            font=(FONT, 10),
        )
        style.configure(
            "Admin.Treeview.Heading",
            background=COLOR_CHIP,
            foreground=COLOR_TEXT,
            font=(FONT, 10, "bold"),
            relief="flat",
            padding=(6, 6),
        )
        style.map(
            "Admin.Treeview",
            background=[("selected", COLOR_ACCENT)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.map(
            "Admin.Treeview.Heading",
            background=[("active", COLOR_CHIP_HOVER)],
        )

        self.tree = ttk.Treeview(
            wrapper,
            columns=[column[0] for column in COLUMNS],
            show="headings",
            selectmode="browse",
            style="Admin.Treeview",
        )
        for key, heading, width, anchor, stretch in COLUMNS:
            self.tree.heading(
                key,
                text=heading,
                anchor=anchor,
                command=lambda column=key: self._on_heading(column),
            )
            self.tree.column(
                key,
                width=width,
                minwidth=48,
                anchor=anchor,
                stretch=stretch,
            )

        self.tree.tag_configure("even", background=COLOR_SURFACE)
        self.tree.tag_configure("odd", background=COLOR_ROW_ALT)
        self.tree.tag_configure("unavailable", foreground=COLOR_MUTED)

        vertical = ttk.Scrollbar(wrapper, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(wrapper, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

        self.tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        vertical.grid(row=0, column=1, sticky="ns", pady=(1, 0), padx=(0, 1))
        horizontal.grid(row=1, column=0, sticky="ew", padx=(1, 0), pady=(0, 1))

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.empty_label = ctk.CTkLabel(
            wrapper,
            text="",
            font=(FONT, 13),
            text_color=COLOR_MUTED,
            fg_color=COLOR_SURFACE,
            justify="center",
        )

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))
        footer.grid_columnconfigure(1, weight=1)

        self.status_dot = ctk.CTkLabel(
            footer,
            text="●",
            width=12,
            font=(FONT, 10),
            text_color=COLOR_WARN,
        )
        self.status_dot.grid(row=0, column=0, padx=(0, 5))

        self.status_label = ctk.CTkLabel(
            footer,
            text="Подключение…",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        self.status_label.grid(row=0, column=1, sticky="w")

        self.selection_label = ctk.CTkLabel(
            footer,
            text="Ничего не выбрано",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="e",
        )
        self.selection_label.grid(row=0, column=2, sticky="e")

    # ------------------------------------------------------------- загрузка

    def refresh(self) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self._load_token += 1
        token = self._load_token
        self._set_busy_ui(True)
        self._set_status("Загрузка каталога…", COLOR_WARN)
        threading.Thread(
            target=self._load_worker,
            args=(token,),
            daemon=True,
            name="table-loader",
        ).start()

    def _load_worker(self, token: int) -> None:
        try:
            items, total = fetch_all(self.session)
        except ApiError as exc:
            message = exc.message
            self._post(lambda: self._fail(token, message))
            return
        except Exception as exc:  # воркер не должен умирать молча
            LOGGER.exception("Не удалось загрузить каталог")
            self._post(lambda: self._fail(token, f"Неожиданная ошибка: {exc}"))
            return
        self._post(lambda: self._apply(token, items, total))

    def _apply(self, token: int, items: list[dict], total: int) -> None:
        if token != self._load_token or self._closing:
            return

        self._teas = items
        self._total = total
        self._fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
        self._old_before = datetime.now(timezone.utc) - timedelta(days=90)
        self._rebuild_haystack()
        self._page = 1
        self._rebuild_view(reset_page=True)
        self._busy = False
        self._set_busy_ui(False)

        suffix = " · только просмотр" if self._read_only else ""
        loaded = len(items)
        if total and total != loaded:
            count_text = f"{loaded} из {total} {plural_tea(total)}"
        else:
            count_text = f"{loaded} {plural_tea(loaded)}"
        self._set_status(f"Сервер доступен · {count_text}{suffix}", COLOR_ACCENT)

    def _fail(self, token: int, message: str) -> None:
        if token != self._load_token or self._closing:
            return
        self._busy = False
        self._set_busy_ui(False)
        self._set_status(f"Ошибка загрузки: {message}", COLOR_DANGER)
        if not self._teas:
            self._show_empty(
                "Не удалось загрузить каталог.\n"
                f"{message}\n\n"
                f"Проверьте, что сервер запущен: {BASE_URL}"
            )

    def _set_busy_ui(self, busy: bool) -> None:
        self.refresh_button.configure(
            state="disabled" if busy else "normal",
            text="Загрузка…" if busy else "Обновить (F5)",
        )

    # ------------------------------------------------------------- данные

    @staticmethod
    def _tea_key(tea: dict) -> str:
        value = tea.get("id")
        return str(value) if value is not None else str(id(tea))

    def _rebuild_haystack(self) -> None:
        self._haystack = {}
        for tea in self._teas:
            parts = (
                tea.get("name"),
                tea.get("description"),
                tea.get("place_of_purchase"),
                TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type")),
                REGION_LABELS.get(tea.get("place_of_harvest"), tea.get("place_of_harvest")),
                tea.get("year_of_harvest"),
                tea.get("price"),
            )
            self._haystack[self._tea_key(tea)] = " ".join(
                str(part) for part in parts if part
            ).casefold()

    def _matches(self, tea: dict) -> bool:
        mode = self._filter_key
        available = bool(tea.get("availability"))

        if mode == "available" and not available:
            return False
        if mode == "unavailable" and available:
            return False
        if mode in ("new", "old"):
            # То же правило, что в TeaViewSet.new / available_old:
            # только доступные сорта и порог по created_at.
            if not available:
                return False
            created = parse_datetime(tea.get("created_at"))
            if created is None:
                return False
            if mode == "new" and created < self._fresh_after:
                return False
            if mode == "old" and created >= self._old_before:
                return False

        query = self._active_query
        if query and query not in self._haystack.get(self._tea_key(tea), ""):
            return False
        return True

    def _raw_sort_value(self, tea: dict, column: str):
        if column == "id":
            value = tea.get("id")
            return value if isinstance(value, int) else None
        if column == "availability":
            return 1 if tea.get("availability") else 0
        if column == "name":
            return (tea.get("name") or "").casefold()
        if column == "tea_type":
            return TEA_TYPE_LABELS.get(
                tea.get("tea_type"), tea.get("tea_type") or ""
            ).casefold()
        if column == "place_of_harvest":
            return REGION_LABELS.get(
                tea.get("place_of_harvest"), tea.get("place_of_harvest") or ""
            ).casefold()
        if column == "year_of_harvest":
            value = tea.get("year_of_harvest")
            return value if isinstance(value, int) else None
        if column == "price":
            return _to_decimal(tea.get("price"))
        if column == "mark":
            return _to_decimal(tea.get("mark"))
        if column == "created_at":
            return parse_datetime(tea.get("created_at"))
        if column == "photo":
            return 1 if tea.get("photo") else 0
        return None

    def _sorted_rows(self, rows: list[dict]) -> list[dict]:
        decorated = [(self._raw_sort_value(tea, self._sort_column), tea) for tea in rows]
        present = [(value, tea) for value, tea in decorated if value is not None]
        missing = [tea for value, tea in decorated if value is None]
        present.sort(key=lambda pair: pair[0], reverse=self._sort_reverse)
        # Пустые значения всегда внизу, независимо от направления.
        return [tea for _, tea in present] + missing

    def _row_values(self, tea: dict) -> tuple:
        year = tea.get("year_of_harvest")
        return (
            tea.get("id", ""),
            "Да" if tea.get("availability") else "Нет",
            tea.get("name") or "—",
            TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type") or "—"),
            REGION_LABELS.get(
                tea.get("place_of_harvest"), tea.get("place_of_harvest") or "—"
            ),
            year if year not in (None, "") else "—",
            format_price(tea.get("price")),
            format_mark(tea.get("mark")),
            format_created(tea.get("created_at")),
            "✓" if tea.get("photo") else "",
        )

    # ------------------------------------------------------------ рендеринг

    def _rebuild_view(self, reset_page: bool = False) -> None:
        rows = [tea for tea in self._teas if self._matches(tea)]
        self._filtered = self._sorted_rows(rows)

        pages = max(1, math.ceil(len(self._filtered) / ROWS_PER_PAGE))
        if reset_page:
            self._page = 1
        self._page = min(max(1, self._page), pages)

        self._fill_table()
        self._update_pager(pages)
        self._update_headings()

    def _fill_table(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._selected_iid = None

        if not self._filtered:
            if self._teas:
                self._show_empty("Ничего не найдено.\nИзмените запрос или фильтр.")
            else:
                self._show_empty("Каталог пуст.\nДобавьте сорта через админку Django или API.")
            return

        self._hide_empty()

        start = (self._page - 1) * ROWS_PER_PAGE
        visible = self._filtered[start:start + ROWS_PER_PAGE]
        known: set[str] = set()

        for offset, tea in enumerate(visible):
            iid = self._tea_key(tea)
            if iid in known:  # защита от повторяющихся id в ответе
                continue
            known.add(iid)
            zebra = "odd" if (start + offset) % 2 else "even"
            tags = (zebra, "unavailable") if not tea.get("availability") else (zebra,)
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=self._row_values(tea),
                tags=tags,
            )

        self._selected_iid = None
        self.selection_label.configure(text="Ничего не выбрано")

    def _update_pager(self, pages: int) -> None:
        total = len(self._filtered)
        if total:
            start = (self._page - 1) * ROWS_PER_PAGE + 1
            end = min(self._page * ROWS_PER_PAGE, total)
            count_text = f"{start}–{end} из {total}"
        else:
            count_text = "0 из 0"

        self.page_label.configure(text=f"Стр. {self._page} из {pages}")
        self.count_label.configure(text=count_text)
        self.prev_button.configure(
            state="normal" if self._page > 1 else "disabled"
        )
        self.next_button.configure(
            state="normal" if self._page < pages else "disabled"
        )

    def _update_headings(self) -> None:
        for key, heading, _width, _anchor, _stretch in COLUMNS:
            if key == self._sort_column:
                arrow = " ▼" if self._sort_reverse else " ▲"
            else:
                arrow = ""
            self.tree.heading(key, text=heading + arrow)

    def _show_empty(self, text: str) -> None:
        self.empty_label.configure(text=text)
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_empty(self) -> None:
        self.empty_label.place_forget()

    # ------------------------------------------------------------- события

    def _set_filter(self, key: str) -> None:
        if key == self._filter_key:
            return
        self._filter_key = key
        for value, button in self.filter_buttons.items():
            selected = value == key
            button.configure(
                fg_color=COLOR_ACCENT if selected else COLOR_CHIP,
                hover_color=COLOR_ACCENT_HOVER if selected else COLOR_CHIP_HOVER,
                text_color="#FFFFFF" if selected else COLOR_TEXT,
                font=(FONT, 11, "bold" if selected else "normal"),
            )
        self._rebuild_view(reset_page=True)

    def _on_search_changed(self, *_args) -> None:
        if self._closing:
            return
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(SEARCH_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self) -> None:
        self._search_job = None
        if self._closing:
            return
        query = self.search_entry.get().strip().casefold()
        if query == self._active_query:
            return
        self._active_query = query
        self._rebuild_view(reset_page=True)

    def _on_heading(self, column: str) -> None:
        if column == self._sort_column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._rebuild_view(reset_page=True)

    def _on_prev(self) -> None:
        if self._page <= 1:
            return
        self._page -= 1
        self._fill_table()
        self._update_pager(max(1, math.ceil(len(self._filtered) / ROWS_PER_PAGE)))

    def _on_next(self) -> None:
        pages = max(1, math.ceil(len(self._filtered) / ROWS_PER_PAGE))
        if self._page >= pages:
            return
        self._page += 1
        self._fill_table()
        self._update_pager(pages)

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            self._selected_iid = None
            self.selection_label.configure(text="Ничего не выбрано")
            return

        iid = selection[0]
        self._selected_iid = iid
        tea = self.selected_tea()
        if tea is None:
            self.selection_label.configure(text=f"Выбрано: #{iid}")
            return
        name = tea.get("name") or "без названия"
        self.selection_label.configure(text=f"Выбрано: #{tea.get('id')} · {name}")

    def selected_tea(self) -> dict | None:
        """Текущая выбранная запись — понадобится на шагах редактирования."""
        if self._selected_iid is None:
            return None
        for tea in self._filtered:
            if self._tea_key(tea) == self._selected_iid:
                return tea
        return None

    # ------------------------------------------------------------- очередь

    def _post(self, callback) -> None:
        """Из любого потока в UI: только кладём в очередь, Tk не трогаем."""
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        if self._closing:
            return
        for _ in range(128):
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                pass
            except Exception:
                LOGGER.exception("Ошибка UI-колбэка")
        try:
            self._drain_job = self.after(DRAIN_INTERVAL_MS, self._drain_ui_queue)
        except tk.TclError:
            self._drain_job = None

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.configure(text=text, text_color=color)
        self.status_dot.configure(text_color=color)

    def on_close(self) -> None:
        self._closing = True
        for job in (self._search_job, self._drain_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            self.destroy()
        except tk.TclError:
            pass


def main() -> None:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        username, password = load_credentials()
    except (ValueError, OSError) as exc:
        print(f"[i] {exc}")
        print("[i] Запуск без учётных данных: доступен только просмотр.")
        session = build_session()
    else:
        session = build_session(username, password)

    app = TeaTableApp(session)
    app.mainloop()


if __name__ == "__main__":
    main()
