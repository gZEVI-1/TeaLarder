"""Админский десктоп-клиент «Чайного учёта»: таблица сортов.

Отдельный процесс: Django не загружается, данные берутся из REST API.
Карточная версия окна сохранена в ``desktop_admin.py`` как наработка
для клиентской (витринной) части. Здесь из неё переиспользуются только
транспортный слой (``TeaApi``, ``ApiError``), сборка сессии и словари
русских подписей — никакой карточной вёрстки этот модуль не трогает.

Функциональность повторяет Django Admin (``Catalog/admin.py``):

* все колонки ``list_display``, сортировка по клику на заголовок;
* фильтры ``list_filter`` (наличие, тип, регион, год, место покупки,
  название) и ``InputFilter`` (новинки / старые / без года сбора),
  с количеством записей в каждой категории;
* поиск по ``search_fields``;
* массовые действия: наличие, даты создания, копирование, удаление;
* форма изменения со всеми полями, фото, inline-галереей ``TeaImage``,
  readonly-датами и кнопками сохранения сверху и снизу;
* подтверждения удаления и отдельных действий.

Запуск:
    .venv\\Scripts\\python.exe desktop_table.py
"""

import io
import logging
import math
import mimetypes
import os
import queue
import threading
import tkinter as tk
import webbrowser
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.parse import unquote, urlparse

import customtkinter as ctk
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps

from desktop_admin import (
    BASE_URL,
    REGION_LABELS,
    TEA_TYPE_LABELS,
    ApiError,
    TeaApi,
    build_session,
    media_url,
    parse_datetime,
    plural_tea,
)

APP_TITLE = "Чайная лавка · администрирование"

# Сколько записей забираем у сервера за один запрос. Совпадает с
# Catalog.pagination.TeaPagination.max_page_size: сервер больше не отдаст.
FETCH_PAGE_SIZE = 500
# Сколько строк рисуем на одной странице таблицы. Это уже клиентская
# навигация по полностью загруженному набору, а не серверная пагинация.
DEFAULT_PAGE_SIZE = 50
PAGE_SIZE_OPTIONS = (
    (25, "25"),
    (50, "50"),
    (100, "100"),
    (200, "200"),
    (0, "Все"),
)

SEARCH_DEBOUNCE_MS = 250
DRAIN_INTERVAL_MS = 16
REQUEST_TIMEOUT = 10.0

# Локальный предел до отправки: сервер тоже проверяет изображение.
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_PREVIEW_SIZE = (300, 150)
GALLERY_THUMB_SIZE = (150, 96)
PHOTO_FILETYPES = (
    ("Изображения", "*.jpg *.jpeg *.png *.webp *.gif *.bmp"),
    ("Все файлы", "*.*"),
)

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
COLOR_ROW_CHECKED = "#EAF0DC"
COLOR_DANGER = "#A23B2A"
COLOR_DANGER_HOVER = "#8A2F20"
COLOR_DANGER_SOFT = "#F4E0DA"
COLOR_WARN = "#B9822E"
COLOR_INFO = "#3E5C76"

ENV_PATH = Path(__file__).resolve().parent / ".env"

LOGGER = logging.getLogger("desktop_table")

# Обратные словари: комбобокс хранит русские подписи, API ждёт коды.
TYPE_CODES = {label: code for code, label in TEA_TYPE_LABELS.items()}
REGION_CODES = {label: code for code, label in REGION_LABELS.items()}

# (ключ поля, заголовок, ширина, выравнивание, растягивать ли колонку,
#  можно ли сортировать)
COLUMNS = (
    ("check", "☐", 40, "center", False, False),
    ("id", "ID", 52, "center", False, True),
    ("availability", "Наличие", 80, "center", False, True),
    ("tea_type", "Тип", 152, "w", False, True),
    ("name", "Название", 240, "w", True, True),
    ("place_of_purchase", "Место покупки", 154, "w", False, True),
    ("price", "Цена", 80, "e", False, True),
    ("mark", "Оценка", 72, "center", False, True),
    ("photo", "Фото", 50, "center", False, True),
    ("place_of_harvest", "Регион", 130, "w", False, True),
    ("year_of_harvest", "Год", 56, "center", False, True),
    ("created_at", "Создано", 120, "center", False, True),
    ("updated_at", "Обновлено", 120, "center", False, True),
    ("images", "Галерея", 92, "center", False, True),
)

# Порядок повторяет list_filter из Catalog/admin.py.
FILTER_FACETS = (
    ("availability", "Наличие"),
    ("preset", "Быстрый фильтр"),
    ("tea_type", "Тип чая"),
    ("place_of_harvest", "Регион"),
    ("year_of_harvest", "Год сбора"),
    ("place_of_purchase", "Место покупки"),
    ("name", "Название"),
)
PRESET_OPTIONS = (
    ("new", "Новинки"),
    ("old", "Старые"),
    ("no_harvest_date", "Без года сбора"),
)

# Поля, которые переносятся при копировании записи.
COPY_FIELDS = (
    "availability",
    "name",
    "place_of_purchase",
    "description",
    "tea_type",
    "price",
    "mark",
    "year_of_harvest",
    "place_of_harvest",
)


@dataclass(frozen=True)
class ActionSpec:
    """Одно массовое действие — аналог @admin.action из admin.py."""

    key: str
    label: str
    description: str
    dangerous: bool = False
    needs_date: bool = False


# Порядок и набор совпадают с TeaAdmin.actions.
ACTIONS = (
    ActionSpec(
        "make_available",
        "Сделать в наличии",
        "У выбранных сортов поле «Наличие» станет «Да».",
    ),
    ActionSpec(
        "make_unavailable",
        "Сделать отсутствующими",
        "У выбранных сортов поле «Наличие» станет «Нет».",
    ),
    ActionSpec(
        "mark_as_new",
        "Пометить как новые",
        "Дата создания выбранных сортов станет текущей.",
    ),
    ActionSpec(
        "mark_as_old",
        "Пометить как старые",
        "Дата создания выбранных сортов станет на 91 день раньше.",
    ),
    ActionSpec(
        "set_year_created_at",
        "Дата создания ← год сбора",
        "Год в дате создания заменится на год сбора; месяц и день "
        "сохранятся. Записи без года сбора будут пропущены.",
    ),
    ActionSpec(
        "set_year_of_harvest",
        "Год сбора ← год создания",
        "Год сбора станет равен году из даты создания.",
    ),
    ActionSpec(
        "change_creation_date",
        "Изменить дату создания…",
        "Откроется форма; выбранным записям будет выставлена одна "
        "и та же дата создания (UTC, как в Django Admin).",
        needs_date=True,
    ),
    ActionSpec(
        "copy_selected",
        "Копировать выбранные",
        "Будут созданы новые записи со всеми полями. Исходные записи "
        "не изменятся.",
    ),
    ActionSpec(
        "copy_selected_with_images",
        "Копировать с изображениями",
        "Будут созданы новые записи, а изображения галереи скопируются "
        "в их inline-галереи.",
    ),
    ActionSpec(
        "copy_and_mark_as_new",
        "Копировать и пометить новыми",
        "Копии получат текущую дату создания (как это делает админка "
        "при copy_selected).",
    ),
    ActionSpec(
        "delete_selected",
        "Удалить выбранные",
        "Записи будут удалены без возможности восстановления. "
        "Изображения галереи удалятся каскадно.",
        dangerous=True,
    ),
    ActionSpec(
        "delete_selected_with_images",
        "Удалить выбранные с изображениями",
        "То же удаление: TeaImage связаны с Tea через on_delete=CASCADE.",
        dangerous=True,
    ),
)
ACTIONS_BY_KEY = {action.key: action for action in ACTIONS}
ACTIONS_BY_LABEL = {action.label: action for action in ACTIONS}


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
# Форматирование и небольшие помощники
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


def format_readonly(value) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "—"
    return parsed.strftime("%d.%m.%Y %H:%M:%S UTC")


def tea_key(tea: dict) -> str:
    value = tea.get("id")
    return str(value) if value is not None else str(id(tea))


def tea_int_id(tea: dict) -> int | None:
    value = tea.get("id")
    return value if isinstance(value, int) else None


def tea_images(tea: dict) -> list[dict]:
    images = tea.get("images")
    return images if isinstance(images, list) else []


def tea_title(tea: dict) -> str:
    name = (tea.get("name") or "без названия").strip()
    return f"#{tea.get('id')} · {name}"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def image_name_from_url(url: str | None, fallback: str = "image.jpg") -> str:
    if not url:
        return fallback
    name = unquote(Path(urlparse(url).path).name)
    return name or fallback


def build_thumb(image: Image.Image, size: tuple[int, int]) -> ctk.CTkImage:
    """Уменьшает PIL-изображение и оборачивает в CTkImage."""
    preview = image.copy()
    preview.thumbnail(size, Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=preview, dark_image=preview, size=preview.size)


def load_image_thumb(path: Path, size: tuple[int, int]) -> ctk.CTkImage:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image.load()
        return build_thumb(image.convert("RGB"), size)


def validate_image_file(path: Path) -> tuple[str | None, str | None]:
    """Проверяет файл до отправки. Возвращает (ошибка, mime-тип)."""
    try:
        if not path.is_file():
            return "Файл не найден.", None
        size = path.stat().st_size
    except OSError as exc:
        return f"Не удалось прочитать файл: {exc}", None
    if size == 0:
        return "Файл пуст.", None
    if size > PHOTO_MAX_BYTES:
        return f"Файл больше {PHOTO_MAX_BYTES // (1024 * 1024)} МБ.", None
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:  # Pillow бросает разные типы исключений
        return f"Не удалось открыть изображение: {exc}", None
    content_type = (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    return None, content_type


def center_window(window: tk.Misc, width: int, height: int) -> None:
    """Ставит окно по центру родителя (или экрана)."""
    try:
        window.update_idletasks()
        parent = window.master
        if parent is not None and parent.winfo_viewable():
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        else:
            x = (window.winfo_screenwidth() - width) // 2
            y = (window.winfo_screenheight() - height) // 2
        window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
    except tk.TclError:
        window.geometry(f"{width}x{height}")


class TeaTableApp(ctk.CTk):
    """Главное окно админ-клиента: таблица сортов с фильтрами и действиями."""

    def __init__(self, session: requests.Session) -> None:
        super().__init__()
        self.session = session
        self._read_only = session.auth is None
        self.api = TeaApi(session, timeout=REQUEST_TIMEOUT)
        self._form_dialog: "TeaFormDialog | None" = None
        self._pending_select_id: int | None = None
        self._pending_message: str | None = None
        self._refresh_job = None

        self.title(APP_TITLE)
        # Не вылезаем за пределы экрана на ноутбуках: 1540×880 — комфортный
        # размер, но на 1366×768 окно должно ужаться.
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = min(1540, max(1100, screen_width - 80))
        window_height = min(880, max(640, screen_height - 80))
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(min(1180, window_width), min(680, window_height))
        self.configure(fg_color=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Данные
        self._teas: list[dict] = []
        self._filtered: list[dict] = []
        self._by_id: dict[int, dict] = {}
        self._haystack: dict[int, str] = {}
        self._total = 0
        self._fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
        self._old_before = datetime.now(timezone.utc) - timedelta(days=90)

        # Состояние представления
        self._filters: dict[str, str] = {
            key: "" for key, _label in FILTER_FACETS
        }
        self._filter_labels: dict[str, dict[str, str]] = {}
        self._filter_combos: dict[str, ctk.CTkComboBox] = {}
        self._filter_open = True
        self._active_query = ""
        self._page = 1
        self._page_size = DEFAULT_PAGE_SIZE
        self._sort_column = "created_at"
        self._sort_reverse = True
        self._checked_ids: set[int] = set()
        self._focused_id: int | None = None

        # Состояние загрузки
        self._busy = False
        self._load_token = 0
        self._action_token = 0
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
        self._build_controls()
        self._build_table()
        self._build_footer()

        self.bind("<F5>", lambda _event: self.refresh(), add="+")
        self.bind("<Control-f>", self._focus_search, add="+")
        self.bind("<Control-n>", lambda _event: self._open_create(), add="+")
        self.bind("<Control-e>", lambda _event: self._open_edit(), add="+")
        self._update_actions()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(
            self, fg_color=COLOR_SURFACE, corner_radius=0, height=72
        )
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
            placeholder_text="название, описание, магазин, ID…",
            height=38,
            corner_radius=19,
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
        self.search_entry.bind("<Escape>", self._clear_search)

        self.create_button = self._header_button(
            header,
            "Добавить",
            self._open_create,
            width=104,
            primary=True,
        )
        self.create_button.grid(row=0, column=2, padx=(0, 8), pady=14)

        self.edit_button = self._header_button(
            header, "Изменить", self._open_edit, width=104
        )
        self.edit_button.grid(row=0, column=3, padx=(0, 8), pady=14)

        self.availability_button = self._header_button(
            header,
            "Наличие",
            self._toggle_availability,
            width=152,
        )
        self.availability_button.grid(row=0, column=4, padx=(0, 8), pady=14)

        self.delete_button = self._header_button(
            header,
            "Удалить",
            self._delete_from_ui,
            width=96,
            danger=True,
        )
        self.delete_button.grid(row=0, column=5, padx=(0, 8), pady=14)

        self.refresh_button = self._header_button(
            header, "Обновить (F5)", self.refresh, width=136, primary=True
        )
        self.refresh_button.grid(row=0, column=6, padx=(0, 20), pady=14)

        self.create_button.configure(
            state="disabled" if self._read_only else "normal"
        )
        self.edit_button.configure(state="disabled")
        self.availability_button.configure(state="disabled")
        self.delete_button.configure(state="disabled")
        self.refresh_button.configure(state="normal")

    def _header_button(
        self,
        parent,
        text: str,
        command,
        *,
        width: int,
        primary: bool = False,
        danger: bool = False,
    ) -> ctk.CTkButton:
        if primary:
            fg, hover, text_color = COLOR_ACCENT, COLOR_ACCENT_HOVER, "#FFFFFF"
        elif danger:
            fg, hover, text_color = COLOR_CHIP, COLOR_DANGER_SOFT, COLOR_DANGER
        else:
            fg, hover, text_color = COLOR_CHIP, COLOR_CHIP_HOVER, COLOR_TEXT
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=38,
            corner_radius=19,
            fg_color=fg,
            hover_color=hover,
            text_color=text_color,
            font=(FONT, 12, "bold" if primary else "normal"),
            state="normal",
        )

    def _build_controls(self) -> None:
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=16, pady=(12, 8))
        controls.grid_columnconfigure(0, weight=1)

        toolbar = ctk.CTkFrame(controls, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(
            toolbar,
            text="Действие:",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
        ).grid(row=0, column=0, padx=(0, 8))

        self.action_menu = ctk.CTkOptionMenu(
            toolbar,
            values=["Выберите действие…"] + [action.label for action in ACTIONS],
            width=360,
            height=32,
            corner_radius=8,
            fg_color=COLOR_CHIP,
            button_color=COLOR_CHIP_HOVER,
            button_hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            dropdown_fg_color=COLOR_SURFACE,
            dropdown_text_color=COLOR_TEXT,
            dropdown_hover_color=COLOR_ROW_ALT,
            font=(FONT, 11),
            dropdown_font=(FONT, 11),
            dynamic_resizing=False,
            command=self._on_action_choice,
        )
        self.action_menu.set("Выберите действие…")
        self.action_menu.grid(row=0, column=1, padx=(0, 8))

        self.action_button = ctk.CTkButton(
            toolbar,
            text="Выполнить",
            command=self._execute_action,
            width=112,
            height=32,
            corner_radius=16,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 11, "bold"),
            state="disabled",
        )
        self.action_button.grid(row=0, column=2, padx=(0, 18))

        self.filter_button = ctk.CTkButton(
            toolbar,
            text="Фильтры ▴",
            command=self._toggle_filter_panel,
            width=136,
            height=32,
            corner_radius=16,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
        )
        self.filter_button.grid(row=0, column=3, padx=(0, 8))

        self.reset_filters_button = ctk.CTkButton(
            toolbar,
            text="Сбросить",
            command=self._reset_filters,
            width=96,
            height=32,
            corner_radius=16,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
            state="disabled",
        )
        self.reset_filters_button.grid(row=0, column=4, padx=(0, 8))

        ctk.CTkLabel(
            toolbar,
            text="На странице:",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
        ).grid(row=0, column=6, padx=(0, 6))

        self.page_size_menu = ctk.CTkOptionMenu(
            toolbar,
            values=[label for _size, label in PAGE_SIZE_OPTIONS],
            width=82,
            height=32,
            corner_radius=8,
            fg_color=COLOR_CHIP,
            button_color=COLOR_CHIP_HOVER,
            button_hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            dropdown_fg_color=COLOR_SURFACE,
            dropdown_text_color=COLOR_TEXT,
            dropdown_hover_color=COLOR_ROW_ALT,
            font=(FONT, 11),
            dropdown_font=(FONT, 11),
            dynamic_resizing=False,
            command=self._on_page_size_changed,
        )
        self.page_size_menu.set(str(DEFAULT_PAGE_SIZE))
        self.page_size_menu.grid(row=0, column=7)

        selection = ctk.CTkFrame(controls, fg_color="transparent")
        selection.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        selection.grid_columnconfigure(3, weight=1)

        self.selection_label = ctk.CTkLabel(
            selection,
            text="Отмечено: 0",
            font=(FONT, 11, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        )
        self.selection_label.grid(row=0, column=0, padx=(2, 12))

        self.select_all_button = ctk.CTkButton(
            selection,
            text="Выбрать все",
            command=self._select_all_filtered,
            width=112,
            height=28,
            corner_radius=14,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 10),
            state="disabled",
        )
        self.select_all_button.grid(row=0, column=1, padx=(0, 6))

        self.clear_checks_button = ctk.CTkButton(
            selection,
            text="Снять",
            command=self._clear_checks,
            width=72,
            height=28,
            corner_radius=14,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 10),
            state="disabled",
        )
        self.clear_checks_button.grid(row=0, column=2)

        self.result_label = ctk.CTkLabel(
            selection,
            text="Найдено: 0",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="e",
        )
        self.result_label.grid(row=0, column=4, sticky="e", padx=(12, 2))

        self._build_filter_panel(controls)

    def _build_filter_panel(self, parent) -> None:
        panel = ctk.CTkFrame(
            parent,
            fg_color=COLOR_SURFACE,
            corner_radius=12,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        panel.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        panel.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.filter_panel = panel

        for index, (key, title) in enumerate(FILTER_FACETS):
            row, column = divmod(index, 4)
            cell = ctk.CTkFrame(panel, fg_color="transparent")
            cell.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(14 if column == 0 else 8, 14 if column == 3 else 8),
                pady=(10, 8),
            )
            cell.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                cell,
                text=title,
                font=(FONT, 10),
                text_color=COLOR_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")

            combo = ctk.CTkComboBox(
                cell,
                values=["Все"],
                state="readonly",
                width=238,
                height=30,
                corner_radius=8,
                border_width=1,
                border_color=COLOR_BORDER,
                fg_color=COLOR_BG,
                text_color=COLOR_TEXT,
                font=(FONT, 11),
                dropdown_fg_color=COLOR_SURFACE,
                dropdown_text_color=COLOR_TEXT,
                dropdown_hover_color=COLOR_ROW_ALT,
                dropdown_font=(FONT, 11),
                command=lambda display, facet=key: self._on_filter_selected(
                    facet, display
                ),
            )
            combo.grid(row=1, column=0, sticky="ew", pady=(3, 0))
            combo.set("Все")
            self._filter_combos[key] = combo

        reset_cell = ctk.CTkFrame(panel, fg_color="transparent")
        reset_cell.grid(
            row=1, column=3, sticky="ew", padx=(8, 14), pady=(10, 8)
        )
        reset_cell.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            reset_cell,
            text=" ",
            font=(FONT, 10),
            text_color=COLOR_MUTED,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            reset_cell,
            text="Сбросить фильтры",
            command=self._reset_filters,
            height=30,
            corner_radius=8,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
        ).grid(row=1, column=0, sticky="ew", pady=(3, 0))

    def _build_table(self) -> None:
        wrapper = ctk.CTkFrame(
            self,
            fg_color=COLOR_SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        wrapper.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
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
            rowheight=32,
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
            selectmode="extended",
            style="Admin.Treeview",
        )
        for key, heading, width, anchor, stretch, sortable in COLUMNS:
            command = (
                self._toggle_page_checks
                if key == "check"
                else (lambda column=key: self._on_heading(column))
            )
            self.tree.heading(
                key,
                text=heading,
                anchor=anchor,
                command=command,
            )
            self.tree.column(
                key,
                width=width,
                minwidth=44 if key == "check" else 52,
                anchor=anchor,
                stretch=stretch,
            )

        self.tree.tag_configure("even", background=COLOR_SURFACE)
        self.tree.tag_configure("odd", background=COLOR_ROW_ALT)
        self.tree.tag_configure("checked", background=COLOR_ROW_CHECKED)
        self.tree.tag_configure("unavailable", foreground=COLOR_MUTED)

        vertical = ttk.Scrollbar(
            wrapper, orient="vertical", command=self.tree.yview
        )
        horizontal = ttk.Scrollbar(
            wrapper, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )

        self.tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        vertical.grid(row=0, column=1, sticky="ns", pady=(1, 0), padx=(0, 1))
        horizontal.grid(row=1, column=0, sticky="ew", padx=(1, 0), pady=(0, 1))

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<Double-1>", self._on_tree_double_click, add="+")
        self.tree.bind("<Return>", lambda _event: self._open_edit(), add="+")
        self.tree.bind("<Delete>", lambda _event: self._delete_from_ui(), add="+")
        self.tree.bind("<space>", self._on_tree_space, add="+")
        self.tree.bind("<Control-a>", self._on_tree_select_all, add="+")

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
        footer.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 10))
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

        self.count_label = ctk.CTkLabel(
            footer,
            text="0 из 0",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            width=120,
            anchor="e",
        )
        self.count_label.grid(row=0, column=2, sticky="e", padx=(8, 10))

        pager = ctk.CTkFrame(footer, fg_color="transparent")
        pager.grid(row=0, column=3, sticky="e")

        self.first_button = self._pager_button(pager, "«", self._on_first, 0)
        self.prev_button = self._pager_button(pager, "‹", self._on_prev, 1)
        self.page_label = ctk.CTkLabel(
            pager,
            text="Стр. 1 из 1",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            width=112,
        )
        self.page_label.grid(row=0, column=2, padx=2)
        self.next_button = self._pager_button(pager, "›", self._on_next, 3)
        self.last_button = self._pager_button(pager, "»", self._on_last, 4)

    def _pager_button(self, parent, text: str, command, column: int):
        button = ctk.CTkButton(
            parent,
            text=text,
            width=32,
            height=28,
            corner_radius=9,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 13, "bold"),
            command=command,
            state="disabled",
        )
        button.grid(row=0, column=column, padx=2)
        return button

    # --------------------------------------------------------------- поиск

    def _focus_search(self, _event=None):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")
        return "break"

    def _clear_search(self, _event=None):
        self.search_entry.delete(0, "end")
        self._apply_search()
        return "break"


    # ------------------------------------------------------------- загрузка

    def refresh(self) -> None:
        if self._busy or self._closing:
            return
        # Пока открыта модальная форма, не запускаем вторую операцию с
        # requests.Session: она не потокобезопасна.
        if self._form_dialog is not None:
            try:
                if self._form_dialog.winfo_exists():
                    return
            except tk.TclError:
                self._form_dialog = None
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
        self._by_id = {
            tea_id: tea
            for tea in items
            for tea_id in [tea_int_id(tea)]
            if tea_id is not None
        }
        self._fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
        self._old_before = datetime.now(timezone.utc) - timedelta(days=90)
        self._checked_ids &= set(self._by_id)
        self._rebuild_haystack()
        self._busy = False
        self._set_busy_ui(False)
        self._rebuild_filter_combos()
        self._rebuild_view()

        suffix = " · только просмотр" if self._read_only else ""
        loaded = len(items)
        if total and total != loaded:
            count_text = f"{loaded} из {total} {plural_tea(total)}"
        else:
            count_text = f"{loaded} {plural_tea(loaded)}"
        if self._pending_message:
            # Не теряем сообщение о создании за статусом «Сервер доступен».
            self._set_status(
                f"{self._pending_message} · всего {count_text}",
                COLOR_ACCENT,
            )
            self._pending_message = None
        else:
            self._set_status(
                f"Сервер доступен · {count_text}{suffix}", COLOR_ACCENT
            )
        self._reveal_pending()

    def _fail(self, token: int, message: str) -> None:
        if token != self._load_token or self._closing:
            return
        self._pending_select_id = None
        self._pending_message = None
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
        self.create_button.configure(
            state="disabled" if busy or self._read_only else "normal"
        )
        if self._read_only:
            self.action_menu.configure(state="disabled")
        else:
            self.action_menu.configure(state="disabled" if busy else "normal")
        self._update_actions()

    def _update_actions(self) -> None:
        enabled = (
            not self._read_only
            and not self._busy
            and self._form_dialog is None
        )
        edit_target = self._edit_target()
        self.edit_button.configure(
            state="normal" if enabled and edit_target is not None else "disabled"
        )
        delete_targets = self._delete_targets()
        self.delete_button.configure(
            state="normal" if enabled and delete_targets else "disabled",
            text=(
                f"Удалить ({len(delete_targets)})"
                if len(delete_targets) > 1
                else "Удалить"
            ),
        )
        focused = self._focused_tea()
        if focused is None:
            availability_text = "Наличие"
        elif focused.get("availability"):
            availability_text = "Убрать из наличия"
        else:
            availability_text = "Вернуть в наличие"
        self.availability_button.configure(
            state="normal" if enabled and focused is not None else "disabled",
            text=availability_text,
        )
        has_checked = bool(self._checked_ids)
        action_choice = self.action_menu.get()
        action_selected = action_choice in ACTIONS_BY_LABEL
        can_run = enabled and has_checked and action_selected
        self.action_button.configure(
            state="normal" if can_run else "disabled",
            fg_color=COLOR_ACCENT if can_run else COLOR_CHIP,
            hover_color=COLOR_ACCENT_HOVER if can_run else COLOR_CHIP_HOVER,
            text_color="#FFFFFF" if can_run else COLOR_MUTED,
        )
        if not enabled:
            self.action_menu.configure(state="disabled")
        else:
            self.action_menu.configure(state="normal")
        if self._read_only:
            self.create_button.configure(
                fg_color=COLOR_CHIP,
                hover_color=COLOR_CHIP_HOVER,
                text_color=COLOR_MUTED,
            )
        self.select_all_button.configure(
            state=(
                "normal"
                if enabled and self._filtered and self.selected_count() < len(self._filtered)
                else "disabled"
            )
        )
        self.clear_checks_button.configure(
            state="normal" if enabled and has_checked else "disabled"
        )

    # ------------------------------------------------------------- данные

    def _rebuild_haystack(self) -> None:
        self._haystack = {}
        for tea in self._teas:
            tea_id = tea_int_id(tea)
            if tea_id is None:
                continue
            availability = "в наличии" if tea.get("availability") else "нет в наличии"
            parts = (
                tea.get("id"),
                tea.get("name"),
                tea.get("description"),
                tea.get("place_of_purchase"),
                tea.get("tea_type"),
                TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type")),
                tea.get("place_of_harvest"),
                REGION_LABELS.get(
                    tea.get("place_of_harvest"), tea.get("place_of_harvest")
                ),
                tea.get("year_of_harvest"),
                tea.get("price"),
                tea.get("mark"),
                availability,
                format_created(tea.get("created_at")),
                format_created(tea.get("updated_at")),
            )
            self._haystack[tea_id] = " ".join(
                str(part) for part in parts if part
            ).casefold()

    def _preset_matches(self, tea: dict, preset: str) -> bool:
        if preset == "no_harvest_date":
            return tea.get("year_of_harvest") in (None, "")
        # Правила совпадают с InputFilter и TeaViewSet.new / available_old:
        # только доступные сорта и порог по created_at.
        if not tea.get("availability"):
            return False
        created = parse_datetime(tea.get("created_at"))
        if created is None:
            return False
        if preset == "new":
            return created >= self._fresh_after
        if preset == "old":
            return created < self._old_before
        return True

    def _facet_matches(self, tea: dict, facet: str) -> bool:
        raw = self._filters.get(facet, "")
        if not raw:
            return True
        if facet == "availability":
            return bool(tea.get("availability")) == (raw == "1")
        if facet == "preset":
            return self._preset_matches(tea, raw)
        if facet == "tea_type":
            return (tea.get("tea_type") or "") == raw
        if facet == "place_of_harvest":
            return (tea.get("place_of_harvest") or "") == raw
        if facet == "year_of_harvest":
            return str(tea.get("year_of_harvest")) == raw
        if facet == "place_of_purchase":
            return (tea.get("place_of_purchase") or "") == raw
        if facet == "name":
            return (tea.get("name") or "") == raw
        return True

    def _matches(self, tea: dict, ignore: str | None = None) -> bool:
        for facet, _label in FILTER_FACETS:
            if facet == ignore:
                continue
            if not self._facet_matches(tea, facet):
                return False

        query = self._active_query
        if query:
            tea_id = tea_int_id(tea)
            haystack = self._haystack.get(tea_id, "") if tea_id else ""
            if not all(term in haystack for term in query.split()):
                return False
        return True

    def _facet_value(self, tea: dict, facet: str) -> str:
        if facet == "availability":
            return "1" if tea.get("availability") else "0"
        if facet == "preset":
            # Запись может попадать сразу в несколько пресетов (например,
            # «без года сбора» и «новинки»), как и в независимых фильтрах
            # Django Admin. Для счётчика берём первый подходящий.
            if tea.get("year_of_harvest") in (None, ""):
                return "no_harvest_date"
            if self._preset_matches(tea, "new"):
                return "new"
            if self._preset_matches(tea, "old"):
                return "old"
            return ""
        if facet == "tea_type":
            return tea.get("tea_type") or ""
        if facet == "place_of_harvest":
            return tea.get("place_of_harvest") or ""
        if facet == "year_of_harvest":
            return str(tea.get("year_of_harvest") or "")
        if facet == "place_of_purchase":
            return tea.get("place_of_purchase") or ""
        if facet == "name":
            return tea.get("name") or ""
        return ""

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
        if column == "place_of_purchase":
            return (tea.get("place_of_purchase") or "").casefold()
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
        if column in ("created_at", "updated_at"):
            return parse_datetime(tea.get(column))
        if column == "photo":
            return 1 if tea.get("photo") else 0
        if column == "images":
            return len(tea_images(tea))
        return None

    def _sorted_rows(self, rows: list[dict]) -> list[dict]:
        decorated = [
            (self._raw_sort_value(tea, self._sort_column), tea) for tea in rows
        ]
        present = [(value, tea) for value, tea in decorated if value is not None]
        missing = [tea for value, tea in decorated if value is None]
        # Стабильный tie-breaker: для дат — id по убыванию (свежие записи
        # выше при одинаковой дате), для остальных колонок — по возрастанию.
        present.sort(
            key=lambda pair: pair[1].get("id") or 0,
            reverse=self._sort_column in ("created_at", "updated_at"),
        )
        present.sort(key=lambda pair: pair[0], reverse=self._sort_reverse)
        # Пустые значения всегда внизу, независимо от направления.
        return [tea for _value, tea in present] + missing

    def _row_values(self, tea: dict) -> tuple:
        year = tea.get("year_of_harvest")
        images = tea_images(tea)
        checked = tea_int_id(tea) in self._checked_ids
        return (
            "☑" if checked else "☐",
            str(tea.get("id", "")),
            "Да" if tea.get("availability") else "Нет",
            TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type") or "—"),
            tea.get("name") or "—",
            tea.get("place_of_purchase") or "—",
            format_price(tea.get("price")),
            format_mark(tea.get("mark")),
            "✓" if tea.get("photo") else "",
            REGION_LABELS.get(
                tea.get("place_of_harvest"), tea.get("place_of_harvest") or "—"
            ),
            str(year) if year not in (None, "") else "—",
            format_created(tea.get("created_at")),
            format_created(tea.get("updated_at")),
            str(len(images)) if images else "—",
        )

    def _row_tags(self, tea: dict, index: int) -> tuple[str, ...]:
        checked = tea_int_id(tea) in self._checked_ids
        zebra = "odd" if index % 2 else "even"
        tags: tuple[str, ...] = (zebra,)
        if checked:
            tags = ("checked", zebra)
        if not tea.get("availability"):
            tags += ("unavailable",)
        return tags

    # ------------------------------------------------------------ рендеринг

    def _pages(self) -> int:
        if not self._filtered:
            return 1
        if self._page_size <= 0:
            return 1
        return max(1, math.ceil(len(self._filtered) / self._page_size))

    def _page_size_effective(self) -> int:
        return self._page_size if self._page_size > 0 else max(1, len(self._filtered))

    def _visible_rows(self) -> list[dict]:
        start = (self._page - 1) * self._page_size_effective()
        return self._filtered[start:start + self._page_size_effective()]

    def _rebuild_view(self, reset_page: bool = False) -> None:
        rows = [tea for tea in self._teas if self._matches(tea)]
        self._filtered = self._sorted_rows(rows)

        pages = self._pages()
        if reset_page:
            self._page = 1
        self._page = min(max(1, self._page), pages)

        self._fill_table()
        self._update_pager()
        self._update_headings()
        self._update_result_label()
        self._update_selection_ui()

    def _fill_table(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._focused_id = None

        if not self._filtered:
            if self._teas:
                self._show_empty(
                    "Ничего не найдено.\n"
                    "Измените запрос или сбросьте фильтры."
                )
            else:
                self._show_empty(
                    "Каталог пуст.\n"
                    "Нажмите «Добавить», чтобы создать первый сорт."
                )
            self._update_check_heading()
            self._update_actions()
            return

        self._hide_empty()
        start = (self._page - 1) * self._page_size_effective()
        visible = self._visible_rows()
        known: set[str] = set()

        for offset, tea in enumerate(visible):
            iid = tea_key(tea)
            if iid in known:  # защита от повторяющихся id в ответе
                continue
            known.add(iid)
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=self._row_values(tea),
                tags=self._row_tags(tea, start + offset),
            )

        self._update_check_heading()
        self._update_actions()

    def _update_pager(self) -> None:
        total = len(self._filtered)
        pages = self._pages()
        if total:
            effective = self._page_size_effective()
            start = (self._page - 1) * effective + 1
            end = min(self._page * effective, total)
            count_text = f"{start}–{end} из {total}"
        else:
            count_text = "0 из 0"

        self.page_label.configure(text=f"Стр. {self._page} из {pages}")
        self.count_label.configure(text=count_text)
        self.first_button.configure(
            state="normal" if self._page > 1 else "disabled"
        )
        self.prev_button.configure(
            state="normal" if self._page > 1 else "disabled"
        )
        self.next_button.configure(
            state="normal" if self._page < pages else "disabled"
        )
        self.last_button.configure(
            state="normal" if self._page < pages else "disabled"
        )

    def _update_headings(self) -> None:
        for key, heading, _width, _anchor, _stretch, sortable in COLUMNS:
            if key == "check":
                continue
            if sortable and key == self._sort_column:
                arrow = " ▼" if self._sort_reverse else " ▲"
            else:
                arrow = ""
            self.tree.heading(key, text=heading + arrow)
        self._update_check_heading()

    def _update_check_heading(self) -> None:
        children = self.tree.get_children()
        total = len(children)
        checked = sum(
            1 for iid in children if self._iid_to_id(iid) in self._checked_ids
        )
        if total and checked == total:
            glyph = "☑"
        elif checked:
            glyph = "◐"
        else:
            glyph = "☐"
        self.tree.heading("check", text=glyph)

    def _update_result_label(self) -> None:
        loaded = len(self._teas)
        filtered = len(self._filtered)
        active = any(self._filters.values()) or bool(self._active_query)
        if active:
            text = f"Найдено: {filtered} из {loaded}"
        else:
            text = f"Всего: {loaded}"
        self.result_label.configure(text=text)

    def _show_empty(self, text: str) -> None:
        self.empty_label.configure(text=text)
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_empty(self) -> None:
        self.empty_label.place_forget()

    # --------------------------------------------------------------- фильтры

    def _facet_options(
        self,
        facet: str,
        counts: Counter,
        total: int,
    ) -> list[tuple[str, str]]:
        current = self._filters.get(facet, "")
        if facet == "availability":
            specs = [
                ("Все", ""),
                ("В наличии", "1"),
                ("Нет в наличии", "0"),
            ]
        elif facet == "preset":
            specs = [("Все", "")] + [
                (label, key) for key, label in PRESET_OPTIONS
            ]
        elif facet == "tea_type":
            specs = [("Все типы", "")] + [
                (label, code) for code, label in TEA_TYPE_LABELS.items()
            ]
        elif facet == "place_of_harvest":
            specs = [("Все регионы", "")] + [
                (label, code) for code, label in REGION_LABELS.items()
            ]
        elif facet == "year_of_harvest":
            years = sorted(
                {
                    int(tea["year_of_harvest"])
                    for tea in self._teas
                    if isinstance(tea.get("year_of_harvest"), int)
                },
                reverse=True,
            )
            specs = [("Все годы", "")] + [(str(year), str(year)) for year in years]
        elif facet == "place_of_purchase":
            values = sorted(
                {
                    tea.get("place_of_purchase") or ""
                    for tea in self._teas
                    if tea.get("place_of_purchase")
                },
                key=str.casefold,
            )
            specs = [("Все места", "")] + [(value, value) for value in values]
        elif facet == "name":
            values = sorted(
                {tea.get("name") or "" for tea in self._teas if tea.get("name")},
                key=str.casefold,
            )
            specs = [("Все названия", "")] + [
                (value, value) for value in values
            ]
        else:
            specs = [("Все", "")]

        options: list[tuple[str, str]] = []
        for label, raw in specs:
            count = total if raw == "" else counts.get(raw, 0)
            if raw and raw != current and count == 0:
                continue
            options.append((f"{label} ({count})", raw))

        if not any(raw == current for _display, raw in options):
            self._filters[facet] = ""
            current = ""
        return options

    def _rebuild_filter_combos(self) -> None:
        for facet, _title in FILTER_FACETS:
            combo = self._filter_combos.get(facet)
            if combo is None:
                continue
            counts: Counter = Counter()
            total = 0
            for tea in self._teas:
                if self._matches(tea, ignore=facet):
                    total += 1
                    counts[self._facet_value(tea, facet)] += 1

            options = self._facet_options(facet, counts, total)
            self._filter_labels[facet] = {
                display: raw for display, raw in options
            }
            current = self._filters[facet]
            selected_display = next(
                (
                    display
                    for display, raw in options
                    if raw == current
                ),
                options[0][0] if options else "",
            )
            try:
                combo.configure(values=[display for display, _raw in options])
                combo.set(selected_display)
            except tk.TclError:
                return
        self._update_filter_badge()

    def _on_filter_selected(self, facet: str, display: str) -> None:
        raw = self._filter_labels.get(facet, {}).get(display, "")
        if raw == self._filters.get(facet):
            return
        self._filters[facet] = raw
        self._checked_ids.clear()
        self._page = 1
        self._rebuild_view()
        self._rebuild_filter_combos()

    def _toggle_filter_panel(self) -> None:
        self._filter_open = not self._filter_open
        if self._filter_open:
            self.filter_panel.grid()
        else:
            self.filter_panel.grid_remove()
        self._update_filter_badge()

    def _reset_filters(self) -> None:
        if not any(self._filters.values()):
            return
        for key in self._filters:
            self._filters[key] = ""
        self._checked_ids.clear()
        self._page = 1
        self._rebuild_view()
        self._rebuild_filter_combos()
        self._set_status("Фильтры сброшены", COLOR_ACCENT)

    def _update_filter_badge(self) -> None:
        active = sum(1 for value in self._filters.values() if value)
        arrow = "▴" if self._filter_open else "▾"
        text = f"Фильтры ({active}) {arrow}" if active else f"Фильтры {arrow}"
        try:
            self.filter_button.configure(text=text)
            self.reset_filters_button.configure(
                state="normal" if active else "disabled"
            )
        except tk.TclError:
            pass

    # --------------------------------------------------------------- поиск

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
        query = " ".join(self.search_entry.get().split()).casefold()
        if query == self._active_query:
            return
        self._active_query = query
        self._checked_ids.clear()
        self._page = 1
        self._rebuild_view()
        self._rebuild_filter_combos()

    # ---------------------------------------------------------- сортировка

    def _on_heading(self, column: str) -> None:
        spec = next(
            (
                item
                for item in COLUMNS
                if item[0] == column and item[5]
            ),
            None,
        )
        if spec is None:
            return
        if column == self._sort_column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._page = 1
        self._rebuild_view()

    # ------------------------------------------------------------ страницы

    def _on_page_size_changed(self, display: str) -> None:
        for size, label in PAGE_SIZE_OPTIONS:
            if label == display:
                if size == self._page_size:
                    return
                self._page_size = size
                break
        self._page = 1
        self._rebuild_view()

    def _goto_page(self, page: int) -> None:
        pages = self._pages()
        page = min(max(1, page), pages)
        if page == self._page:
            return
        self._page = page
        self._fill_table()
        self._update_pager()
        self._update_headings()
        self._update_selection_ui()

    def _on_first(self) -> None:
        self._goto_page(1)

    def _on_prev(self) -> None:
        self._goto_page(self._page - 1)

    def _on_next(self) -> None:
        self._goto_page(self._page + 1)

    def _on_last(self) -> None:
        self._goto_page(self._pages())

    # ------------------------------------------------------------ выделение

    def _iid_to_id(self, iid: str) -> int | None:
        try:
            return int(iid)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _column_key(column: str) -> str | None:
        """Превращает '#9' из Treeview в ключ колонки ('photo')."""
        try:
            index = int(column.lstrip("#")) - 1
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(COLUMNS):
            return COLUMNS[index][0]
        return None

    def selected_count(self) -> int:
        return len(self._checked_ids)

    def _checked_teas(self) -> list[dict]:
        return [
            tea
            for tea in self._filtered
            if tea_int_id(tea) in self._checked_ids
        ]

    def _action_targets(self) -> list[dict]:
        return self._checked_teas()

    def _focused_tea(self) -> dict | None:
        if self._focused_id is None:
            return None
        return self._by_id.get(self._focused_id)

    def _edit_target(self) -> dict | None:
        focused = self._focused_tea()
        if focused is not None:
            return focused
        checked = self._checked_teas()
        if len(checked) == 1:
            return checked[0]
        return None

    def _delete_targets(self) -> list[dict]:
        checked = self._checked_teas()
        if checked:
            return checked
        focused = self._focused_tea()
        return [focused] if focused is not None else []

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if selection:
            tea_id = self._iid_to_id(selection[0])
            self._focused_id = tea_id
        else:
            self._focused_id = None
        self._update_selection_ui()
        self._update_actions()

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._focused_id = self._iid_to_id(iid)
        self._toggle_check(iid)
        return "break"

    def _on_tree_double_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        column = self.tree.identify_column(event.x)
        if column == "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self._focused_id = self._iid_to_id(iid)
        if self._column_key(column) == "photo":
            tea = self._focused_tea()
            if tea is not None:
                self._attach_photo_to(tea)
            return
        self._open_edit()

    def _on_tree_space(self, _event=None):
        if self._focused_id is None:
            return "break"
        self._toggle_check(str(self._focused_id))
        return "break"

    def _on_tree_select_all(self, _event=None):
        self._select_all_filtered()
        return "break"

    def _toggle_check(self, iid: str) -> None:
        tea_id = self._iid_to_id(iid)
        if tea_id is None:
            return
        if tea_id in self._checked_ids:
            self._checked_ids.remove(tea_id)
        else:
            self._checked_ids.add(tea_id)
        tea = self._by_id.get(tea_id)
        if tea is not None:
            self._update_row_check(tea)
        self._update_check_heading()
        self._update_selection_ui()
        self._update_actions()

    def _update_row_check(self, tea: dict) -> None:
        iid = tea_key(tea)
        if not self.tree.exists(iid):
            return
        values = list(self.tree.item(iid, "values"))
        if values:
            values[0] = (
                "☑"
                if tea_int_id(tea) in self._checked_ids
                else "☐"
            )
        try:
            index = self.tree.index(iid)
        except tk.TclError:
            index = 0
        self.tree.item(iid, values=values, tags=self._row_tags(tea, index))

    def _refresh_visible_checks(self) -> None:
        for iid in self.tree.get_children():
            tea_id = self._iid_to_id(iid)
            tea = self._by_id.get(tea_id) if tea_id is not None else None
            if tea is not None:
                self._update_row_check(tea)

    def _toggle_page_checks(self) -> None:
        children = self.tree.get_children()
        if not children:
            return
        visible_ids = {
            tea_id
            for iid in children
            for tea_id in [self._iid_to_id(iid)]
            if tea_id is not None
        }
        if visible_ids and visible_ids <= self._checked_ids:
            self._checked_ids -= visible_ids
        else:
            self._checked_ids |= visible_ids
        self._refresh_visible_checks()
        self._update_check_heading()
        self._update_selection_ui()
        self._update_actions()

    def _select_all_filtered(self) -> None:
        if not self._filtered:
            return
        self._checked_ids = {
            tea_id
            for tea in self._filtered
            for tea_id in [tea_int_id(tea)]
            if tea_id is not None
        }
        self._refresh_visible_checks()
        self._update_check_heading()
        self._update_selection_ui()
        self._update_actions()

    def _clear_checks(self) -> None:
        if not self._checked_ids:
            return
        self._checked_ids.clear()
        self._refresh_visible_checks()
        self._update_check_heading()
        self._update_selection_ui()
        self._update_actions()

    def _update_selection_ui(self) -> None:
        checked = len(self._checked_ids)
        focused = self._focused_tea()
        if checked:
            text = f"Отмечено: {checked}"
            color = COLOR_ACCENT
        elif focused is not None:
            text = f"В фокусе: {tea_title(focused)}"
            color = COLOR_TEXT
        else:
            text = "Ничего не выбрано"
            color = COLOR_MUTED
        try:
            self.selection_label.configure(text=text, text_color=color)
            if self._filtered:
                self.select_all_button.configure(
                    text=f"Выбрать все ({len(self._filtered)})"
                )
            else:
                self.select_all_button.configure(text="Выбрать все")
        except tk.TclError:
            pass
        self._update_actions()


    # ------------------------------------------------- создание и правка

    def _existing_form(self) -> bool:
        if self._form_dialog is None:
            return False
        try:
            if self._form_dialog.winfo_exists():
                self._form_dialog.lift()
                self._form_dialog.focus_force()
                return True
        except tk.TclError:
            pass
        self._form_dialog = None
        return False

    def _place_options(self) -> list[str]:
        """Уникальные места покупки из уже загруженного каталога."""
        unique: dict[str, str] = {}
        for tea in self._teas:
            value = (tea.get("place_of_purchase") or "").strip()
            if value:
                unique.setdefault(value.casefold(), value)
        return sorted(unique.values(), key=str.casefold)

    def _open_create(self) -> None:
        if self._closing:
            return
        if self._read_only:
            self._set_status("Режим просмотра: добавление недоступно", COLOR_WARN)
            return
        if self._busy:
            self._set_status("Дождитесь окончания операции", COLOR_WARN)
            return
        if self._existing_form():
            return

        self._form_dialog = TeaFormDialog(
            self,
            self.api,
            on_saved=self._on_saved,
            on_deleted=self._on_deleted,
            place_options=self._place_options(),
        )

    def _open_edit(self) -> None:
        if self._closing:
            return
        if self._read_only:
            self._set_status("Режим просмотра: редактирование недоступно", COLOR_WARN)
            return
        if self._busy:
            self._set_status("Дождитесь окончания операции", COLOR_WARN)
            return

        tea = self._edit_target()
        if tea is None:
            self._set_status("Выберите одну запись в таблице", COLOR_WARN)
            return
        if self._existing_form():
            return

        self._form_dialog = TeaFormDialog(
            self,
            self.api,
            tea=tea,
            on_saved=self._on_saved,
            on_deleted=self._on_deleted,
            place_options=self._place_options(),
        )

    def _on_saved(
        self,
        tea: dict,
        warnings: list[str],
        is_edit: bool,
    ) -> None:
        self._form_dialog = None
        tea_id = tea_int_id(tea)
        self._pending_select_id = tea_id
        action = "обновлён" if is_edit else "добавлен"
        message = f"Сорт {action}: {tea_title(tea)}"
        if warnings:
            message += f" · замечаний: {len(warnings)}"
            for warning in warnings:
                LOGGER.warning("Сохранение сорта: %s", warning)
        self._pending_message = message
        self._set_status(message, COLOR_ACCENT)
        self._refresh_when_idle()

    def _on_deleted(self, tea_id: int) -> None:
        self._form_dialog = None
        self._checked_ids.discard(tea_id)
        if self._pending_select_id == tea_id:
            self._pending_select_id = None
        self._pending_message = f"Сорт #{tea_id} удалён"
        self._set_status(self._pending_message, COLOR_ACCENT)
        self._refresh_when_idle()

    def _refresh_when_idle(self) -> None:
        """refresh() молча выходит при _busy, поэтому ждём завершения загрузки."""
        if self._closing:
            return
        if self._busy:
            self._refresh_job = self.after(300, self._refresh_when_idle)
        else:
            self._refresh_job = None
            self.refresh()

    def _reveal_pending(self) -> None:
        """После refresh показывает и выделяет сохранённую запись."""
        tea_id = self._pending_select_id
        if tea_id is None:
            return
        self._pending_select_id = None

        index = next(
            (
                index
                for index, item in enumerate(self._filtered)
                if item.get("id") == tea_id
            ),
            None,
        )
        if index is None:
            # Запись скрыта фильтром или поиском — статус уже сообщил о ней.
            return

        self._page = index // self._page_size_effective() + 1
        self._fill_table()
        self._update_pager()
        iid = str(tea_id)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self._focused_id = tea_id
            self._on_select()

    # ----------------------------------------------------- массовые действия

    def _on_action_choice(self, _display: str) -> None:
        self._update_actions()

    def _execute_action(self) -> None:
        if self._closing or self._busy:
            return
        if self._read_only:
            self._set_status("Режим просмотра: действия недоступны", COLOR_WARN)
            return
        spec = ACTIONS_BY_LABEL.get(self.action_menu.get())
        if spec is None:
            self._set_status("Сначала выберите действие в списке", COLOR_WARN)
            return
        targets = self._action_targets()
        if not targets:
            self._set_status(
                "Отметьте записи флажками в первом столбце", COLOR_WARN
            )
            return

        if spec.needs_date:
            ChangeDateDialog(
                self,
                title=spec.label,
                on_confirm=lambda value: self._confirm_action(
                    spec, targets, value
                ),
            )
            return
        self._confirm_action(spec, targets, None)

    def _confirm_action(
        self,
        spec: ActionSpec,
        targets: list[dict],
        created_at: str | None,
    ) -> None:
        preview = "\n".join(f"• {tea_title(tea)}" for tea in targets[:5])
        if len(targets) > 5:
            preview += f"\n… и ещё {len(targets) - 5}"
        details = f"Записей: {len(targets)}"
        if created_at:
            parsed = parse_datetime(created_at)
            if parsed is not None:
                details += f"\nНовая дата создания: {parsed:%d.%m.%Y %H:%M} UTC"
        details += f"\n\n{preview}"
        ConfirmDialog(
            self,
            title="Массовое действие",
            message=spec.description,
            details=details,
            confirm_text="Удалить" if spec.dangerous else "Выполнить",
            danger=spec.dangerous,
            require_phrase=(
                "УДАЛИТЬ" if spec.dangerous and len(targets) > 10 else None
            ),
            on_confirm=lambda: self._run_action(spec, targets, created_at),
        )

    def _run_action(
        self,
        spec: ActionSpec,
        targets: list[dict],
        created_at: str | None,
    ) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self._action_token += 1
        token = self._action_token
        snapshot = [dict(tea) for tea in targets]
        self._set_busy_ui(True)
        self._set_status(f"Выполняется: {spec.label}…", COLOR_WARN)
        threading.Thread(
            target=self._action_worker,
            args=(token, spec, snapshot, created_at),
            daemon=True,
            name=f"action-{spec.key}",
        ).start()

    def _action_worker(
        self,
        token: int,
        spec: ActionSpec,
        targets: list[dict],
        created_at: str | None,
    ) -> None:
        done = 0
        failures: list[tuple[int | None, str]] = []
        new_ids: list[int] = []
        total = len(targets)

        for index, tea in enumerate(targets, 1):
            if self._closing or token != self._action_token:
                return
            if index == 1 or index % 5 == 0 or index == total:
                progress = f"{spec.label}: {index} из {total}…"
                self._post(
                    lambda text=progress: self._set_status(text, COLOR_WARN)
                )
            try:
                new_id = self._perform_action(spec.key, tea, created_at)
            except ActionSkip as exc:
                failures.append((tea_int_id(tea), str(exc)))
            except ApiError as exc:
                if spec.key.startswith("delete_selected") and exc.status == 404:
                    # Запись уже удалена — результат достигнут.
                    done += 1
                else:
                    failures.append((tea_int_id(tea), exc.message))
            except Exception as exc:  # воркер не должен умирать молча
                LOGGER.exception("Ошибка массового действия %s", spec.key)
                failures.append((tea_int_id(tea), f"Неожиданная ошибка: {exc}"))
            else:
                done += 1
                if new_id is not None:
                    new_ids.append(new_id)

        self._post(
            lambda: self._finish_action(
                token, spec, done, failures, new_ids, total
            )
        )

    def _perform_action(
        self,
        key: str,
        tea: dict,
        created_at: str | None,
    ) -> int | None:
        tea_id = tea_int_id(tea)
        if tea_id is None:
            raise ActionSkip("у записи нет числового ID")

        if key == "make_available":
            self.api.update_tea(tea_id, {"availability": True})
        elif key == "make_unavailable":
            self.api.update_tea(tea_id, {"availability": False})
        elif key == "mark_as_new":
            value = now_utc_iso()
            updated = self.api.update_tea(tea_id, {"created_at": value})
            self._check_created_at(updated, value)
        elif key == "mark_as_old":
            value = (
                datetime.now(timezone.utc) - timedelta(days=91)
            ).isoformat()
            updated = self.api.update_tea(tea_id, {"created_at": value})
            self._check_created_at(updated, value)
        elif key == "set_year_created_at":
            year = tea.get("year_of_harvest")
            if not isinstance(year, int):
                raise ActionSkip("не указан год сбора")
            created = parse_datetime(tea.get("created_at"))
            if created is None:
                raise ActionSkip("не указана дата создания")
            try:
                value = created.replace(year=year)
            except ValueError:
                # 29 февраля в невисокосном году — переносим на 28-е.
                value = created.replace(year=year, day=28)
            updated = self.api.update_tea(
                tea_id, {"created_at": value.isoformat()}
            )
            self._check_created_at(updated, value)
        elif key == "set_year_of_harvest":
            created = parse_datetime(tea.get("created_at"))
            if created is None:
                raise ActionSkip("не указана дата создания")
            self.api.update_tea(tea_id, {"year_of_harvest": created.year})
        elif key == "change_creation_date":
            if not created_at:
                raise ActionSkip("не задана новая дата")
            updated = self.api.update_tea(
                tea_id, {"created_at": created_at}
            )
            self._check_created_at(updated, created_at)
        elif key in (
            "copy_selected",
            "copy_selected_with_images",
            "copy_and_mark_as_new",
        ):
            with_images = key == "copy_selected_with_images"
            return self._copy_tea(tea, with_images=with_images)
        elif key in ("delete_selected", "delete_selected_with_images"):
            self.api.delete_tea(tea_id)
        else:
            raise ActionSkip(f"неизвестное действие {key}")
        return None

    @staticmethod
    def _check_created_at(updated: dict, expected: str | datetime) -> None:
        actual_value = updated.get("created_at")
        actual = parse_datetime(actual_value)
        wanted = expected if isinstance(expected, datetime) else parse_datetime(expected)
        if actual is None or wanted is None:
            raise ApiError("Сервер не вернул дату создания.")
        if abs((actual - wanted).total_seconds()) > 1:
            raise ApiError(
                "Сервер не сохранил дату создания: похоже, запущена старая "
                "версия API. Перезапустите runserver."
            )

    def _copy_payload(self, tea: dict) -> dict:
        payload: dict = {}
        for field in COPY_FIELDS:
            value = tea.get(field)
            if field == "description":
                payload[field] = value or ""
            elif value is not None and value != "":
                payload[field] = value
        payload.setdefault("availability", bool(tea.get("availability")))
        return payload

    def _copy_tea(self, tea: dict, *, with_images: bool) -> int:
        payload = self._copy_payload(tea)
        files = None
        photo = media_url(tea.get("photo"))
        if photo:
            try:
                content = self.api.fetch_media(photo)
            except ApiError as exc:
                LOGGER.warning("Фото не скопировано: %s", exc.message)
            else:
                filename = image_name_from_url(photo)
                files = {
                    "photo": (
                        filename,
                        io.BytesIO(content),
                        mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                    )
                }
        created = self.api.create_tea(payload, files=files)
        new_id = tea_int_id(created)
        if new_id is None:
            raise ApiError("Сервер не вернул ID копии.")

        if with_images:
            for image in tea_images(tea):
                url = media_url(image.get("image"))
                if not url:
                    continue
                try:
                    content = self.api.fetch_media(url)
                    filename = image_name_from_url(url)
                    self.api.create_image_bytes(
                        new_id,
                        filename,
                        content,
                        mimetypes.guess_type(filename)[0],
                    )
                except ApiError as exc:
                    LOGGER.warning(
                        "Изображение %s не скопировано: %s",
                        image.get("id"),
                        exc.message,
                    )
        return new_id

    def _finish_action(
        self,
        token: int,
        spec: ActionSpec,
        done: int,
        failures: list[tuple[int | None, str]],
        new_ids: list[int],
        total: int,
    ) -> None:
        if token != self._action_token or self._closing:
            return
        self._busy = False
        self._set_busy_ui(False)

        changed = (
            "удалено"
            if spec.key.startswith("delete_selected")
            else ("создано" if new_ids else "обработано")
        )
        parts = [f"{spec.label}: {changed} {done} из {total}"]
        if new_ids:
            parts.append(f"новых записей: {len(new_ids)}")
        if failures:
            parts.append(f"ошибок: {len(failures)}")
        message = " · ".join(parts)
        self._pending_message = message
        self._pending_select_id = new_ids[0] if len(new_ids) == 1 else None
        self._set_status(message, COLOR_DANGER if failures else COLOR_ACCENT)

        if spec.key.startswith("delete_selected") and done:
            self._checked_ids.clear()

        self.refresh()
        if failures:
            details = "\n".join(
                f"#{tea_id if tea_id is not None else '?'}: {message}"
                for tea_id, message in failures[:10]
            )
            if len(failures) > 10:
                details += f"\n… и ещё {len(failures) - 10}"
            InfoDialog(
                self,
                title="Действие завершено с ошибками",
                message=(
                    f"Успешно: {done} из {total}. "
                    f"Ошибок: {len(failures)}."
                ),
                details=details,
                danger=True,
            )

    # ------------------------------------------------------------ наличие

    def _toggle_availability(self) -> None:
        """Быстрое переключение наличия для сфокусированной строки."""
        if self._closing or self._busy or self._read_only:
            return
        if self._form_dialog is not None:
            return
        tea = self._focused_tea()
        if tea is None:
            self._set_status("Выберите сорт в таблице", COLOR_WARN)
            return
        tea_id = tea_int_id(tea)
        if tea_id is None:
            return

        new_value = not bool(tea.get("availability"))
        self._busy = True
        self._action_token += 1
        token = self._action_token
        self._set_busy_ui(True)
        self._set_status("Меняем наличие…", COLOR_WARN)
        threading.Thread(
            target=self._availability_worker,
            args=(token, tea_id, new_value),
            daemon=True,
            name="toggle-availability",
        ).start()

    def _availability_worker(
        self, token: int, tea_id: int, value: bool
    ) -> None:
        try:
            self.api.update_tea(tea_id, {"availability": value})
        except ApiError as exc:
            self._post(
                lambda error=exc: self._finish_availability(
                    token, tea_id, value, error
                )
            )
        except Exception as exc:
            error = ApiError(f"Неожиданная ошибка: {exc}")
            self._post(
                lambda error=error: self._finish_availability(
                    token, tea_id, value, error
                )
            )
        else:
            self._post(
                lambda: self._finish_availability(token, tea_id, value, None)
            )

    def _finish_availability(
        self,
        token: int,
        tea_id: int,
        value: bool,
        error: ApiError | None,
    ) -> None:
        if token != self._action_token or self._closing:
            return
        self._busy = False
        self._set_busy_ui(False)
        if error is not None:
            self._set_status(
                f"Наличие не изменено: {error.message}", COLOR_DANGER
            )
            return
        state = "в наличии" if value else "нет в наличии"
        self._pending_message = f"Сорт #{tea_id}: {state}"
        self._set_status(self._pending_message, COLOR_ACCENT)
        self.refresh()

    # ------------------------------------------------------------- фото

    def _attach_photo_to(self, tea: dict) -> None:
        """Добавляет/заменяет основное фото прямо из таблицы."""
        if self._closing:
            return
        if self._read_only:
            self._set_status("Режим просмотра: фото недоступно", COLOR_WARN)
            return
        if self._busy:
            self._set_status("Дождитесь окончания операции", COLOR_WARN)
            return
        if self._form_dialog is not None:
            return
        tea_id = tea_int_id(tea)
        if tea_id is None:
            return

        selected = filedialog.askopenfilename(
            parent=self,
            title="Фото сорта",
            filetypes=PHOTO_FILETYPES,
        )
        if not selected:
            return
        path = Path(selected)
        error, content_type = validate_image_file(path)
        if error:
            self._set_status(f"Фото не выбрано: {error}", COLOR_DANGER)
            return
        if content_type is None:
            return

        self._busy = True
        self._action_token += 1
        token = self._action_token
        self._set_busy_ui(True)
        self._set_status(f"Загрузка фото: #{tea_id}…", COLOR_WARN)
        threading.Thread(
            target=self._photo_attach_worker,
            args=(token, tea_id, path, content_type),
            daemon=True,
            name="table-photo",
        ).start()

    def _photo_attach_worker(
        self,
        token: int,
        tea_id: int,
        path: Path,
        content_type: str,
    ) -> None:
        try:
            with path.open("rb") as handle:
                files = {
                    "photo": (path.name, handle, content_type)
                }
                self.api.update_tea(tea_id, {}, files=files)
        except ApiError as exc:
            self._post(
                lambda error=exc: self._finish_photo_attach(
                    token, tea_id, error
                )
            )
        except OSError as exc:
            error = ApiError(f"Не удалось прочитать фото: {exc}")
            self._post(
                lambda error=error: self._finish_photo_attach(
                    token, tea_id, error
                )
            )
        except Exception as exc:
            error = ApiError(f"Неожиданная ошибка: {exc}")
            self._post(
                lambda error=error: self._finish_photo_attach(
                    token, tea_id, error
                )
            )
        else:
            self._post(
                lambda: self._finish_photo_attach(token, tea_id, None)
            )

    def _finish_photo_attach(
        self, token: int, tea_id: int, error: ApiError | None
    ) -> None:
        if token != self._action_token or self._closing:
            return
        self._busy = False
        self._set_busy_ui(False)
        if error is not None:
            self._set_status(
                f"Фото не загружено: {error.message}", COLOR_DANGER
            )
            return
        self._pending_message = f"Фото обновлено: #{tea_id}"
        self._set_status(self._pending_message, COLOR_ACCENT)
        self.refresh()

    # ------------------------------------------------------------- удаление

    def _delete_from_ui(self) -> None:
        if self._closing or self._busy:
            return
        if self._read_only:
            self._set_status("Режим просмотра: удаление недоступно", COLOR_WARN)
            return
        targets = self._delete_targets()
        if not targets:
            self._set_status("Выберите запись или отметьте флажки", COLOR_WARN)
            return
        preview = "\n".join(f"• {tea_title(tea)}" for tea in targets[:5])
        if len(targets) > 5:
            preview += f"\n… и ещё {len(targets) - 5}"
        ConfirmDialog(
            self,
            title="Удаление",
            message=(
                "Записи будут удалены без возможности восстановления. "
                "Изображения галереи удалятся каскадно."
            ),
            details=f"Записей: {len(targets)}\n\n{preview}",
            confirm_text="Удалить",
            danger=True,
            require_phrase="УДАЛИТЬ" if len(targets) > 10 else None,
            on_confirm=lambda: self._run_delete(targets),
        )

    def _run_delete(self, targets: list[dict]) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self._action_token += 1
        token = self._action_token
        snapshot = [dict(tea) for tea in targets]
        self._set_busy_ui(True)
        self._set_status(f"Удаление: 0 из {len(snapshot)}…", COLOR_WARN)
        threading.Thread(
            target=self._delete_worker,
            args=(token, snapshot),
            daemon=True,
            name="table-delete",
        ).start()

    def _delete_worker(self, token: int, targets: list[dict]) -> None:
        done = 0
        failures: list[tuple[int | None, str]] = []
        already_gone: list[int] = []
        total = len(targets)
        for index, tea in enumerate(targets, 1):
            if self._closing or token != self._action_token:
                return
            tea_id = tea_int_id(tea)
            if tea_id is None:
                failures.append((None, "нет числового ID"))
                continue
            try:
                self.api.delete_tea(tea_id)
            except ApiError as exc:
                if exc.status == 404:
                    # Запись уже удалили (например, из админки) — цель
                    # достигнута, это не ошибка массового удаления.
                    done += 1
                    already_gone.append(tea_id)
                else:
                    failures.append((tea_id, exc.message))
            except Exception as exc:
                LOGGER.exception("Ошибка удаления")
                failures.append((tea_id, f"Неожиданная ошибка: {exc}"))
            else:
                done += 1
            self._post(
                lambda n=index: self._set_status(
                    f"Удаление: {n} из {total}…", COLOR_WARN
                )
            )

        self._post(
            lambda: self._finish_delete(
                token, done, failures, total, targets, already_gone
            )
        )

    def _finish_delete(
        self,
        token: int,
        done: int,
        failures: list[tuple[int | None, str]],
        total: int,
        targets: list[dict],
        already_gone: list[int],
    ) -> None:
        if token != self._action_token or self._closing:
            return
        for tea in targets:
            tea_id = tea_int_id(tea)
            if tea_id is not None:
                self._checked_ids.discard(tea_id)
        self._busy = False
        self._set_busy_ui(False)
        message = f"Удалено {done} из {total}"
        if already_gone:
            message += f" · уже отсутствовало: {len(already_gone)}"
        if failures:
            message += f" · ошибок: {len(failures)}"
        self._pending_message = message
        self._set_status(message, COLOR_DANGER if failures else COLOR_ACCENT)
        self.refresh()
        if failures or already_gone:
            details: list[str] = []
            if already_gone:
                details.append(
                    "Уже отсутствовали: "
                    + ", ".join(f"#{tea_id}" for tea_id in already_gone[:10])
                )
            details.extend(
                f"#{tea_id if tea_id is not None else '?'}: {text}"
                for tea_id, text in failures[:10]
            )
            if len(failures) > 10:
                details.append(f"… и ещё {len(failures) - 10} ошибок")
            InfoDialog(
                self,
                title=(
                    "Удаление завершено с ошибками"
                    if failures
                    else "Удаление завершено"
                ),
                message=(
                    f"Удалено: {done} из {total}."
                    + (
                        f" Ошибок: {len(failures)}."
                        if failures
                        else ""
                    )
                ),
                details="\n".join(details),
                danger=bool(failures),
            )

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
        # Очень длинный текст ошибки/итога не должен раздвигать футер.
        if len(text) > 180:
            text = text[:177].rstrip() + "…"
        try:
            self.status_label.configure(text=text, text_color=color)
            self.status_dot.configure(text_color=color)
        except tk.TclError:
            pass

    def on_close(self) -> None:
        self._closing = True
        self._action_token += 1
        self._load_token += 1
        if self._form_dialog is not None:
            try:
                self._form_dialog._close(force=True)
            except Exception:
                pass
            self._form_dialog = None
        for job in (self._search_job, self._drain_job, self._refresh_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            self.destroy()
        except tk.TclError:
            pass


class ActionSkip(RuntimeError):
    """Ожидаемый пропуск записи при массовом действии."""


class ModalDialog(ctk.CTkToplevel):
    """Общая основа модальных окон: центр, grab, Escape, аккуратное закрытие."""

    def __init__(
        self,
        master,
        *,
        title: str,
        width: int,
        height: int,
    ) -> None:
        super().__init__(master)
        self._closed = False
        self.title(title)
        self.configure(fg_color=COLOR_BG)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        center_window(self, width, height)
        self.minsize(width, 220)
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            self._closed = True

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        # Если диалог открыт поверх модальной формы, возвращаем ей grab.
        parent = self.master
        if isinstance(parent, ctk.CTkToplevel):
            try:
                if parent.winfo_exists() and parent.winfo_viewable():
                    parent.grab_set()
            except tk.TclError:
                pass
        try:
            self.destroy()
        except tk.TclError:
            pass


class ConfirmDialog(ModalDialog):
    """Подтверждение массового действия или удаления."""

    def __init__(
        self,
        master,
        *,
        title: str,
        message: str,
        details: str = "",
        confirm_text: str = "Выполнить",
        danger: bool = False,
        on_confirm: Callable[[], None] | None = None,
        require_phrase: str | None = None,
    ) -> None:
        super().__init__(
            master,
            title=title,
            width=580,
            height=470 if require_phrase else 400,
        )
        self._on_confirm = on_confirm
        self._require_phrase = require_phrase
        self._phrase_ok = require_phrase is None

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(22, 12))

        ctk.CTkLabel(
            body,
            text=message,
            font=(FONT, 13),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=520,
        ).pack(fill="x")

        if details:
            box = ctk.CTkFrame(
                body,
                fg_color=COLOR_SURFACE,
                corner_radius=10,
                border_width=1,
                border_color=COLOR_BORDER,
            )
            box.pack(fill="both", expand=True, pady=(14, 0))
            ctk.CTkLabel(
                box,
                text=details,
                font=(FONT, 11),
                text_color=COLOR_TEXT,
                anchor="nw",
                justify="left",
                wraplength=510,
            ).pack(fill="both", expand=True, padx=14, pady=12)

        if self._require_phrase:
            phrase_box = ctk.CTkFrame(self, fg_color="transparent")
            phrase_box.pack(fill="x", padx=24, pady=(0, 10))
            ctk.CTkLabel(
                phrase_box,
                text=f"Для подтверждения введите «{self._require_phrase}»",
                font=(FONT, 11),
                text_color=COLOR_DANGER if danger else COLOR_MUTED,
                anchor="w",
            ).pack(fill="x")
            self.phrase_entry = ctk.CTkEntry(
                phrase_box,
                height=34,
                corner_radius=8,
                border_width=1,
                border_color=COLOR_BORDER,
                fg_color=COLOR_SURFACE,
                text_color=COLOR_TEXT,
                font=(FONT, 12),
            )
            self.phrase_entry.pack(fill="x", pady=(4, 0))
            self.phrase_entry.bind("<KeyRelease>", self._on_phrase_changed)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(0, 18))

        cancel_button = ctk.CTkButton(
            buttons,
            text="Отмена",
            command=self._close,
            width=120,
            height=38,
            corner_radius=19,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 12),
        )
        cancel_button.pack(side="right", padx=(8, 0))

        confirm = ctk.CTkButton(
            buttons,
            text=confirm_text,
            command=self._confirm,
            width=150,
            height=38,
            corner_radius=19,
            fg_color=COLOR_DANGER if danger else COLOR_ACCENT,
            hover_color=COLOR_DANGER_HOVER if danger else COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 12, "bold"),
            state="normal" if self._phrase_ok else "disabled",
        )
        confirm.pack(side="right")
        self._confirm_button = confirm

        # Безопасное поведение: по умолчанию фокус на «Отмене», Enter
        # срабатывает только на кнопке, которая реально в фокусе.
        cancel_button.focus_set()
        cancel_button.bind("<Return>", lambda _event: self._close())
        confirm.bind("<Return>", lambda _event: self._confirm())
        if self._require_phrase:
            self.phrase_entry.focus_set()

    def _on_phrase_changed(self, _event=None) -> None:
        if not self._require_phrase:
            return
        self._phrase_ok = self.phrase_entry.get().strip() == self._require_phrase
        button = getattr(self, "_confirm_button", None)
        if button is not None:
            button.configure(
                state="normal" if self._phrase_ok else "disabled"
            )

    def _confirm(self) -> None:
        if not self._phrase_ok:
            return
        callback = self._on_confirm
        self._close()
        if callback is not None:
            callback()


class InfoDialog(ModalDialog):
    """Сообщение о результате с прокручиваемыми подробностями."""

    def __init__(
        self,
        master,
        *,
        title: str,
        message: str,
        details: str = "",
        danger: bool = False,
    ) -> None:
        super().__init__(master, title=title, width=640, height=460)

        ctk.CTkLabel(
            self,
            text=message,
            font=(FONT, 13),
            text_color=COLOR_DANGER if danger else COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=580,
        ).pack(fill="x", padx=24, pady=(22, 12))

        if details:
            box = ctk.CTkTextbox(
                self,
                corner_radius=10,
                border_width=1,
                border_color=COLOR_BORDER,
                fg_color=COLOR_SURFACE,
                text_color=COLOR_TEXT,
                font=(FONT, 11),
                wrap="word",
            )
            box.pack(fill="both", expand=True, padx=24, pady=(0, 12))
            box.insert("1.0", details)
            box.configure(state="disabled")

        ctk.CTkButton(
            self,
            text="Закрыть",
            command=self._close,
            width=130,
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 12, "bold"),
        ).pack(pady=(0, 18))
        self.bind("<Return>", lambda _event: self._close())


class ChangeDateDialog(ModalDialog):
    """Аналог ChangeCreationDateForm: одна дата для выбранных записей."""

    def __init__(
        self,
        master,
        *,
        title: str,
        on_confirm: Callable[[str], None],
    ) -> None:
        super().__init__(master, title=title, width=580, height=420)
        self._on_confirm = on_confirm
        now = datetime.now(timezone.utc)

        ctk.CTkLabel(
            self,
            text="Новая дата создания",
            font=(FONT, 16, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).pack(fill="x", padx=24, pady=(20, 2))
        ctk.CTkLabel(
            self,
            text=(
                "Дата применится ко всем выбранным записям. "
                "Время указывается в UTC — так же, как в Django Admin."
            ),
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=520,
        ).pack(fill="x", padx=24, pady=(0, 16))

        fields = ctk.CTkFrame(self, fg_color="transparent")
        fields.pack(fill="x", padx=24)
        specs = (
            ("year", "Год", 92, now.year),
            ("month", "Месяц", 72, now.month),
            ("day", "День", 72, now.day),
            ("hour", "Часы", 72, now.hour),
            ("minute", "Минуты", 80, now.minute),
        )
        self._date_vars: dict[str, tk.StringVar] = {}
        for column, (key, label, width, value) in enumerate(specs):
            cell = ctk.CTkFrame(fields, fg_color="transparent")
            cell.grid(row=0, column=column, padx=(0, 8), sticky="w")
            ctk.CTkLabel(
                cell,
                text=label,
                font=(FONT, 10),
                text_color=COLOR_MUTED,
                anchor="w",
            ).pack(fill="x")
            var = tk.StringVar(value=str(value))
            self._date_vars[key] = var
            ctk.CTkEntry(
                cell,
                textvariable=var,
                width=width,
                height=34,
                corner_radius=8,
                border_width=1,
                border_color=COLOR_BORDER,
                fg_color=COLOR_SURFACE,
                text_color=COLOR_TEXT,
                font=(FONT, 12),
            ).pack(fill="x", pady=(3, 0))

        self.error_label = ctk.CTkLabel(
            self,
            text="",
            font=(FONT, 11),
            text_color=COLOR_DANGER,
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.error_label.pack(fill="x", padx=24, pady=(14, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(16, 18))

        ctk.CTkButton(
            buttons,
            text="Сейчас",
            command=self._fill_now,
            width=100,
            height=38,
            corner_radius=19,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 12),
        ).pack(side="left")

        ctk.CTkButton(
            buttons,
            text="Отмена",
            command=self._close,
            width=110,
            height=38,
            corner_radius=19,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 12),
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            buttons,
            text="Установить",
            command=self._submit,
            width=140,
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 12, "bold"),
        ).pack(side="right")
        self.bind("<Return>", lambda _event: self._submit())

    def _fill_now(self) -> None:
        now = datetime.now(timezone.utc)
        for key, value in (
            ("year", now.year),
            ("month", now.month),
            ("day", now.day),
            ("hour", now.hour),
            ("minute", now.minute),
        ):
            self._date_vars[key].set(str(value))

    def _submit(self) -> None:
        try:
            values = {
                key: int(var.get().strip())
                for key, var in self._date_vars.items()
            }
        except (TypeError, ValueError):
            self.error_label.configure(text="Все поля должны быть целыми числами.")
            return
        try:
            parsed = datetime(
                values["year"],
                values["month"],
                values["day"],
                values["hour"],
                values["minute"],
                tzinfo=timezone.utc,
            )
        except ValueError as exc:
            self.error_label.configure(text=f"Некорректная дата: {exc}.")
            return
        if not 1900 <= parsed.year <= 2100:
            self.error_label.configure(text="Год должен быть от 1900 до 2100.")
            return
        callback = self._on_confirm
        self._close()
        callback(parsed.isoformat())


class TeaFormDialog(ctk.CTkToplevel):
    """Модальная форма создания и редактирования сорта.

    ``tea is None`` — создание. Иначе — редактирование: PATCH только
    изменённых полей, замена/очистка фото, inline-галерея TeaImage и
    удаление записи. Отправка идёт в отдельном потоке, UI обновляется
    только через очередь приложения.
    """

    def __init__(
        self,
        master: TeaTableApp,
        api: TeaApi,
        *,
        tea: dict | None = None,
        on_saved: Callable[[dict, list[str], bool], None],
        on_deleted: Callable[[int], None],
        place_options: list[str] | None = None,
    ) -> None:
        super().__init__(master)
        self.app = master
        self.api = api
        self.tea = dict(tea) if tea is not None else None
        self.on_saved = on_saved
        self.on_deleted = on_deleted
        self.is_edit = tea is not None
        self._place_options = list(place_options or [])

        self._busy = False
        self._closed = False
        self._errors: dict[str, ctk.CTkLabel] = {}
        self._save_buttons: list[ctk.CTkButton] = []
        self._cancel_buttons: list[ctk.CTkButton] = []

        # Фото
        self._photo_path: Path | None = None
        self._photo_action = "keep"
        self._photo_preview_image: ctk.CTkImage | None = None
        self._photo_existing_url = (
            media_url(self.tea.get("photo")) if self.tea else None
        )

        # Галерея
        self._gallery_existing: list[dict] = []
        self._gallery_deleted: set[int] = set()
        self._gallery_new: list[Path] = []
        self._gallery_loaded = not self.is_edit
        self._gallery_error: str | None = None
        self._thumb_cache: dict[str, ctk.CTkImage] = {}
        self._thumb_loading: set[str] = set()
        self._thumb_failed: set[str] = set()

        self.title("Новый сорт" if not self.is_edit else "Редактирование сорта")
        self._window_width = min(
            780, max(660, self.winfo_screenwidth() - 80)
        )
        self._window_height = min(
            900, max(600, self.winfo_screenheight() - 80)
        )
        self.geometry(f"{self._window_width}x{self._window_height}")
        self.minsize(
            min(660, self._window_width),
            min(640, self._window_height),
        )
        self.configure(fg_color=COLOR_BG)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Control-Return>", lambda _event: self._submit())

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_ui()
        self._fill_from_tea()

        center_window(self, self._window_width, self._window_height)
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            self._closed = True
            return
        self.name_entry.focus_set()
        if self.is_edit:
            self.after(120, self._start_photo_preview)
            self.after(160, self._start_gallery_load)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 8))
        header.grid_columnconfigure(0, weight=1)

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            titles,
            text="Новый сорт" if not self.is_edit else "Редактирование сорта",
            font=(FONT, 18, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text=(
                "Поля со звёздочкой обязательны · "
                "Ctrl+Enter — сохранить"
            ),
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        top_actions = ctk.CTkFrame(header, fg_color="transparent")
        top_actions.grid(row=0, column=1, sticky="e")
        self._make_action_buttons(top_actions)

        self.readonly_label = ctk.CTkLabel(
            header,
            text="",
            font=(FONT, 10),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
        )
        self.readonly_label.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        self.body = ctk.CTkScrollableFrame(
            self,
            fg_color=COLOR_BG,
            corner_radius=0,
            scrollbar_button_color=COLOR_CHIP,
            scrollbar_button_hover_color=COLOR_CHIP_HOVER,
        )
        self.body.grid(row=1, column=0, sticky="nsew", padx=(16, 8))

        self.banner = ctk.CTkLabel(
            self.body,
            text="",
            font=(FONT, 11),
            text_color=COLOR_DANGER,
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.banner.pack(fill="x", pady=(0, 10))

        fields = self._section(
            "Основные поля",
            "Те же поля и проверки, что в форме Django Admin.",
        )
        place_options = list(self._place_options)
        current_place = (self.tea or {}).get("place_of_purchase") or ""
        if current_place and current_place not in place_options:
            place_options.insert(0, current_place)
        self._place_options = place_options

        self.name_entry = self._field(
            fields, "name", "Название *", self._entry
        )
        self.place_entry = self._field(
            fields,
            "place_of_purchase",
            "Место покупки * (выберите или введите новое)",
            lambda parent: self._combo(
                parent, self._place_options, editable=True
            ),
        )
        self.type_combo = self._field(
            fields,
            "tea_type",
            "Тип чая *",
            lambda parent: self._combo(parent, list(TEA_TYPE_LABELS.values())),
        )
        self.region_combo = self._field(
            fields,
            "place_of_harvest",
            "Регион *",
            lambda parent: self._combo(parent, list(REGION_LABELS.values())),
        )
        self.availability_switch = self._field(
            fields,
            "availability",
            "Наличие",
            lambda parent: ctk.CTkSwitch(
                parent,
                text="В наличии",
                font=(FONT, 12),
                text_color=COLOR_TEXT,
                progress_color=COLOR_ACCENT,
                button_color=COLOR_ACCENT,
                button_hover_color=COLOR_ACCENT_HOVER,
            ),
        )

        metrics = ctk.CTkFrame(fields, fg_color="transparent")
        metrics.pack(fill="x", pady=(0, 10))
        metrics.grid_columnconfigure((0, 1, 2), weight=1)
        self.year_entry = self._grid_field(
            metrics, 0, "year_of_harvest", "Год сбора (1900–2100)", self._entry
        )
        self.price_entry = self._grid_field(
            metrics, 1, "price", "Цена, ₽ (до 9999.99)", self._entry
        )
        self.mark_entry = self._grid_field(
            metrics, 2, "mark", "Оценка (0–10)", self._entry
        )

        self.description_box = self._field(
            fields,
            "description",
            "Описание",
            lambda parent: ctk.CTkTextbox(
                parent,
                height=110,
                corner_radius=8,
                border_width=1,
                border_color=COLOR_BORDER,
                fg_color=COLOR_SURFACE,
                text_color=COLOR_TEXT,
                font=(FONT, 12),
                wrap="word",
            ),
        )

        self.type_combo.set(TEA_TYPE_LABELS["other"])
        self.region_combo.set(REGION_LABELS["China"])
        self.availability_switch.select()

        photo_section = self._section(
            "Фото",
            "Одно основное изображение сорта. Можно заменить или удалить.",
        )
        self._build_photo_block(photo_section)

        gallery_section = self._section(
            "Галерея",
            "Inline-галерея TeaImage: добавление и удаление при сохранении.",
        )
        self._build_gallery_block(gallery_section)

        readonly_section = self._section("Служебные поля", "Только для чтения.")
        self.readonly_body = ctk.CTkLabel(
            readonly_section,
            text="",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.readonly_body.pack(fill="x", pady=(0, 8))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=22, pady=(4, 16))
        footer.grid_columnconfigure(1, weight=1)

        self.delete_button = None
        if self.is_edit:
            self.delete_button = ctk.CTkButton(
                footer,
                text="Удалить сорт",
                command=self._delete,
                width=140,
                height=38,
                corner_radius=19,
                fg_color=COLOR_DANGER_SOFT,
                hover_color="#EBCFC7",
                text_color=COLOR_DANGER,
                font=(FONT, 12),
            )
            self.delete_button.grid(row=0, column=0, sticky="w")

        bottom_actions = ctk.CTkFrame(footer, fg_color="transparent")
        bottom_actions.grid(row=0, column=2, sticky="e")
        self._make_action_buttons(bottom_actions)

    def _make_action_buttons(self, parent) -> None:
        idle_text = "Применить" if self.is_edit else "Сохранить"
        save = ctk.CTkButton(
            parent,
            text=idle_text,
            command=self._submit,
            width=130,
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 12, "bold"),
        )
        save.pack(side="right")
        self._save_buttons.append(save)

        cancel = ctk.CTkButton(
            parent,
            text="Отмена",
            command=self._close,
            width=104,
            height=38,
            corner_radius=19,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 12),
        )
        cancel.pack(side="right", padx=(0, 8))
        self._cancel_buttons.append(cancel)

    def _section(self, title: str, hint: str = "") -> ctk.CTkFrame:
        ctk.CTkLabel(
            self.body,
            text=title,
            font=(FONT, 13, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).pack(fill="x", pady=(12, 0))
        if hint:
            ctk.CTkLabel(
                self.body,
                text=hint,
                font=(FONT, 10),
                text_color=COLOR_MUTED,
                anchor="w",
                justify="left",
                wraplength=640,
            ).pack(fill="x", pady=(1, 0))
        frame = ctk.CTkFrame(self.body, fg_color="transparent")
        frame.pack(fill="x", pady=(6, 0))
        return frame

    def _entry(self, parent) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            parent,
            height=34,
            corner_radius=8,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_SURFACE,
            text_color=COLOR_TEXT,
            font=(FONT, 12),
        )

    def _combo(
        self,
        parent,
        values: list[str],
        *,
        editable: bool = False,
    ) -> ctk.CTkComboBox:
        return ctk.CTkComboBox(
            parent,
            values=values,
            state="normal" if editable else "readonly",
            height=34,
            corner_radius=8,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_SURFACE,
            text_color=COLOR_TEXT,
            font=(FONT, 12),
            button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER,
            dropdown_fg_color=COLOR_SURFACE,
            dropdown_text_color=COLOR_TEXT,
            dropdown_hover_color=COLOR_CHIP,
            dropdown_font=(FONT, 12),
        )

    def _field(self, parent, key: str, label: str, factory):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            frame,
            text=label,
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        ).pack(fill="x")

        widget = factory(frame)
        widget.pack(fill="x", pady=(3, 0))

        error = ctk.CTkLabel(
            frame,
            text="",
            font=(FONT, 10),
            text_color=COLOR_DANGER,
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self._errors[key] = error
        return widget

    def _grid_field(
        self,
        parent,
        column: int,
        key: str,
        label: str,
        factory,
    ):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(
            row=0,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 8, 0),
        )
        ctk.CTkLabel(
            cell,
            text=label,
            font=(FONT, 10),
            text_color=COLOR_MUTED,
            anchor="w",
        ).pack(fill="x")
        widget = factory(cell)
        widget.pack(fill="x", pady=(3, 0))
        error = ctk.CTkLabel(
            cell,
            text="",
            font=(FONT, 9),
            text_color=COLOR_DANGER,
            anchor="w",
            justify="left",
            wraplength=200,
        )
        self._errors[key] = error
        return widget

    # ------------------------------------------------------------- фото UI

    def _build_photo_block(self, parent) -> None:
        self.photo_preview_frame = ctk.CTkFrame(
            parent,
            height=180,
            corner_radius=10,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_SURFACE,
        )
        self.photo_preview_frame.pack(fill="x")
        self.photo_preview_frame.pack_propagate(False)
        self.photo_preview = self._new_photo_preview(
            self.photo_preview_frame, "Фото не выбрано"
        )

        controls = ctk.CTkFrame(parent, fg_color="transparent")
        controls.pack(fill="x", pady=(8, 0))

        self.photo_button = ctk.CTkButton(
            controls,
            text="Выбрать…",
            command=self._choose_photo,
            width=112,
            height=32,
            corner_radius=16,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
        )
        self.photo_button.pack(side="left")

        self.photo_clear_button = ctk.CTkButton(
            controls,
            text="Удалить фото",
            command=self._toggle_photo,
            width=124,
            height=32,
            corner_radius=16,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
        )
        self.photo_clear_button.pack(side="left", padx=(8, 0))

        self.photo_open_button = ctk.CTkButton(
            controls,
            text="Открыть",
            command=self._open_photo,
            width=96,
            height=32,
            corner_radius=16,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
        )
        self.photo_open_button.pack(side="left", padx=(8, 0))

        self.photo_status = ctk.CTkLabel(
            parent,
            text="",
            font=(FONT, 10),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self.photo_status.pack(fill="x", pady=(6, 0))

        self._errors["photo"] = ctk.CTkLabel(
            parent,
            text="",
            font=(FONT, 10),
            text_color=COLOR_DANGER,
            anchor="w",
            justify="left",
            wraplength=620,
        )

    def _new_photo_preview(self, parent, text: str) -> ctk.CTkLabel:
        label = ctk.CTkLabel(
            parent,
            text=text,
            font=(FONT, 11),
            text_color=COLOR_MUTED,
        )
        label.place(relx=0.5, rely=0.5, anchor="center")
        return label

    # ---------------------------------------------------------- галерея UI

    def _build_gallery_block(self, parent) -> None:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x")
        header.grid_columnconfigure(0, weight=1)

        self.gallery_count_label = ctk.CTkLabel(
            header,
            text="Изображений нет",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        self.gallery_count_label.grid(row=0, column=0, sticky="w")

        self.gallery_add_button = ctk.CTkButton(
            header,
            text="Добавить изображения…",
            command=self._add_gallery_files,
            width=190,
            height=32,
            corner_radius=16,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11),
        )
        self.gallery_add_button.grid(row=0, column=1, sticky="e")

        self.gallery_grid = ctk.CTkFrame(parent, fg_color="transparent")
        self.gallery_grid.pack(fill="x", pady=(8, 0))
        self.gallery_grid.grid_columnconfigure((0, 1, 2), weight=1)
        self.gallery_hint = ctk.CTkLabel(
            parent,
            text="",
            font=(FONT, 10),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self.gallery_hint.pack(fill="x", pady=(4, 0))
        self._render_gallery()


    # ------------------------------------------------------- заполнение

    def _fill_from_tea(self) -> None:
        """Предзаполняет форму для редактирования."""
        if not self.tea:
            self.readonly_label.configure(text="Новая запись")
            self.readonly_body.configure(
                text="ID, дата создания и дата обновления появятся после "
                "первого сохранения."
            )
            self.place_entry.set("")
            self.photo_preview_frame.configure(height=96)
            self._reset_photo_preview("Фото не выбрано")
            self.photo_status.configure(text="Фото не загружено.")
            self._update_photo_buttons()
            return

        tea = self.tea
        self.name_entry.insert(0, tea.get("name") or "")
        self.place_entry.set(tea.get("place_of_purchase") or "")

        tea_type = tea.get("tea_type")
        self.type_combo.set(TEA_TYPE_LABELS.get(tea_type, TEA_TYPE_LABELS["other"]))

        region = tea.get("place_of_harvest")
        self.region_combo.set(REGION_LABELS.get(region, REGION_LABELS["China"]))

        if tea.get("availability"):
            self.availability_switch.select()
        else:
            self.availability_switch.deselect()

        year = tea.get("year_of_harvest")
        if year not in (None, ""):
            self.year_entry.insert(0, str(year))

        price = tea.get("price")
        if price not in (None, ""):
            self.price_entry.insert(0, str(price).replace(",", "."))

        mark = tea.get("mark")
        if mark not in (None, ""):
            self.mark_entry.insert(0, str(mark).replace(",", "."))

        description = tea.get("description") or ""
        if description:
            self.description_box.insert("1.0", description)

        self.readonly_label.configure(
            text=f"ID {tea.get('id')} · запись из REST API"
        )
        self.readonly_body.configure(
            text=(
                f"ID: {tea.get('id')}\n"
                f"Создано: {format_readonly(tea.get('created_at'))}\n"
                f"Обновлено: {format_readonly(tea.get('updated_at'))}"
            )
        )

        if self._photo_existing_url:
            name = image_name_from_url(self._photo_existing_url)
            self.photo_preview_frame.configure(height=180)
            self.photo_status.configure(text=f"Фото загружено: {name}")
            self._reset_photo_preview("Загрузка фото…")
        else:
            self.photo_preview_frame.configure(height=96)
            self.photo_status.configure(text="Фото не загружено.")
            self._reset_photo_preview("Фото не загружено")
        self._update_photo_buttons()

    # ------------------------------------------------------------- фото

    def _reset_photo_preview(self, text: str) -> None:
        try:
            self.photo_preview.destroy()
        except tk.TclError:
            pass
        self.photo_preview = self._new_photo_preview(
            self.photo_preview_frame, text
        )
        self._photo_preview_image = None

    def _show_photo_image(self, image: ctk.CTkImage) -> None:
        self._photo_preview_image = image
        self.photo_preview_frame.configure(height=180)
        try:
            self.photo_preview.destroy()
        except tk.TclError:
            pass
        self.photo_preview = ctk.CTkLabel(
            self.photo_preview_frame, text="", image=image
        )
        self.photo_preview.place(relx=0.5, rely=0.5, anchor="center")

    def _update_photo_buttons(self) -> None:
        busy = self._busy or self._closed
        has_photo = bool(self._photo_path or self._photo_existing_url)
        self.photo_button.configure(
            text="Заменить фото…" if has_photo else "Добавить фото…",
            state="disabled" if busy else "normal",
        )

        restoring = self._photo_action == "clear" and has_photo
        can_clear = (
            not busy
            and has_photo
            and self._photo_action != "clear"
        )
        self.photo_clear_button.configure(
            text="Вернуть фото" if restoring else "Удалить фото",
            state="normal" if (can_clear or (not busy and restoring)) else "disabled",
        )
        can_open = (
            not busy
            and self._photo_action != "clear"
            and bool(self._photo_path or self._photo_existing_url)
        )
        self.photo_open_button.configure(
            state="normal" if can_open else "disabled"
        )

    def _start_photo_preview(self) -> None:
        url = self._photo_existing_url
        if not url or self._closed:
            return
        threading.Thread(
            target=self._photo_preview_worker,
            args=(url,),
            daemon=True,
            name="photo-preview",
        ).start()

    def _photo_preview_worker(self, url: str) -> None:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except Exception as exc:
            self.app._post(
                lambda message=str(exc): self._photo_preview_failed(message)
            )
            return
        self.app._post(
            lambda content=response.content: self._apply_remote_preview(content)
        )

    def _photo_preview_failed(self, message: str) -> None:
        if self._closed or self._photo_action != "keep":
            return
        LOGGER.info("Превью фото недоступно: %s", message)
        self._reset_photo_preview("Превью недоступно")
        self.photo_status.configure(
            text="Фото загружено, но превью получить не удалось."
        )

    def _apply_remote_preview(self, content: bytes) -> None:
        if self._closed or self._photo_action != "keep":
            return
        try:
            with Image.open(io.BytesIO(content)) as image:
                image = ImageOps.exif_transpose(image)
                image.load()
                preview = build_thumb(image.convert("RGB"), PHOTO_PREVIEW_SIZE)
        except Exception as exc:
            LOGGER.info("Превью фото не открылось: %s", exc)
            self._reset_photo_preview("Превью недоступно")
            return
        self._show_photo_image(preview)

    def _choose_photo(self) -> None:
        if self._busy or self._closed:
            return
        selected = filedialog.askopenfilename(
            parent=self,
            title="Фото сорта",
            filetypes=PHOTO_FILETYPES,
        )
        if not selected:
            return
        path = Path(selected)
        error, _content_type = validate_image_file(path)
        if error:
            self._set_error_label(self._errors["photo"], error)
            return
        try:
            thumb = load_image_thumb(path, PHOTO_PREVIEW_SIZE)
        except Exception as exc:
            self._set_error_label(
                self._errors["photo"],
                f"Не удалось открыть изображение: {exc}",
            )
            return

        self._photo_action = "replace"
        self._photo_path = path
        self._set_error_label(self._errors["photo"], "")
        self._show_photo_image(thumb)
        size_kb = max(1, path.stat().st_size // 1024)
        self.photo_status.configure(
            text=f"Новое фото: {path.name} ({size_kb} КБ) — заменит текущее."
        )
        self._update_photo_buttons()

    def _toggle_photo(self) -> None:
        if self._busy or self._closed:
            return
        if self._photo_action == "clear" and self._photo_existing_url:
            self._restore_photo()
        else:
            self._clear_photo()

    def _restore_photo(self) -> None:
        if not self._photo_existing_url:
            return
        self._photo_action = "keep"
        self._photo_path = None
        self._set_error_label(self._errors["photo"], "")
        self.photo_preview_frame.configure(height=180)
        self._reset_photo_preview("Загрузка фото…")
        self.photo_status.configure(
            text=f"Фото загружено: {image_name_from_url(self._photo_existing_url)}"
        )
        self._update_photo_buttons()
        self._start_photo_preview()

    def _clear_photo(self) -> None:
        if self._busy or self._closed:
            return
        if not (self._photo_path or self._photo_existing_url):
            return
        had_existing = bool(self._photo_existing_url)
        self._photo_path = None
        self._photo_action = "clear" if had_existing else "keep"
        self._set_error_label(self._errors["photo"], "")
        if had_existing:
            self.photo_preview_frame.configure(height=96)
            self._reset_photo_preview("Фото будет удалено после сохранения")
            self.photo_status.configure(
                text="Фото будет удалено после сохранения."
            )
        else:
            self.photo_preview_frame.configure(height=96)
            self._reset_photo_preview("Фото не выбрано")
            self.photo_status.configure(text="Фото не выбрано.")
        self._update_photo_buttons()

    def _open_photo(self) -> None:
        if self._photo_action == "replace" and self._photo_path is not None:
            webbrowser.open(self._photo_path.as_uri())
        elif self._photo_action == "keep" and self._photo_existing_url:
            webbrowser.open(self._photo_existing_url)

    # ----------------------------------------------------------- галерея

    def _start_gallery_load(self) -> None:
        tea_id = tea_int_id(self.tea or {})
        if tea_id is None or self._closed:
            return
        threading.Thread(
            target=self._gallery_worker,
            args=(tea_id,),
            daemon=True,
            name="gallery-loader",
        ).start()

    def _gallery_worker(self, tea_id: int) -> None:
        try:
            items = self.api.list_images(tea_id)
        except ApiError as exc:
            self.app._post(
                lambda message=exc.message: self._gallery_failed(message)
            )
            return
        except Exception as exc:
            LOGGER.exception("Не удалось загрузить галерею")
            self.app._post(
                lambda message=f"Неожиданная ошибка: {exc}": self._gallery_failed(
                    message
                )
            )
            return
        self.app._post(lambda: self._apply_gallery(items))

    def _apply_gallery(self, items: list[dict]) -> None:
        if self._closed:
            return
        self._gallery_existing = [
            item for item in items if isinstance(item, dict)
        ]
        self._gallery_loaded = True
        self._gallery_error = None
        self._render_gallery()

    def _gallery_failed(self, message: str) -> None:
        if self._closed:
            return
        self._gallery_loaded = True
        self._gallery_error = message
        self._render_gallery()

    def _gallery_changes_pending(self) -> bool:
        return bool(self._gallery_deleted or self._gallery_new)

    def _render_gallery(self) -> None:
        if self._closed:
            return
        for child in self.gallery_grid.winfo_children():
            child.destroy()

        total = (
            len(self._gallery_existing)
            - len(self._gallery_deleted)
            + len(self._gallery_new)
        )
        detail = f"Изображений: {total}"
        if self._gallery_deleted:
            detail += f" · к удалению: {len(self._gallery_deleted)}"
        if self._gallery_new:
            detail += f" · новых: {len(self._gallery_new)}"
        self.gallery_count_label.configure(text=detail)

        if not self._gallery_loaded:
            ctk.CTkLabel(
                self.gallery_grid,
                text="Загрузка галереи…",
                font=(FONT, 11),
                text_color=COLOR_MUTED,
            ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
            return

        if self._gallery_error:
            error_box = ctk.CTkFrame(
                self.gallery_grid, fg_color="transparent"
            )
            error_box.grid(row=0, column=0, columnspan=3, sticky="w")
            ctk.CTkLabel(
                error_box,
                text=f"Галерея недоступна: {self._gallery_error}",
                font=(FONT, 10),
                text_color=COLOR_DANGER,
                anchor="w",
                justify="left",
                wraplength=560,
            ).pack(side="left")
            ctk.CTkButton(
                error_box,
                text="Повторить",
                command=self._retry_gallery,
                width=92,
                height=26,
                corner_radius=13,
                fg_color=COLOR_CHIP,
                hover_color=COLOR_CHIP_HOVER,
                text_color=COLOR_TEXT,
                font=(FONT, 10),
            ).pack(side="left", padx=(10, 0))
            if not self._gallery_existing and not self._gallery_new:
                return

        entries: list[tuple[str, object]] = []
        for image in self._gallery_existing:
            if image.get("id") in self._gallery_deleted:
                entries.append(("deleted", image))
            else:
                entries.append(("remote", image))
        for path in self._gallery_new:
            entries.append(("local", path))

        if not entries:
            ctk.CTkLabel(
                self.gallery_grid,
                text="Изображений нет. Добавьте файлы — они сохранятся "
                "вместе с сортом.",
                font=(FONT, 11),
                text_color=COLOR_MUTED,
                anchor="w",
            ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
            self.gallery_hint.configure(text="", text_color=COLOR_MUTED)
            return

        for index, (kind, payload) in enumerate(entries):
            card = self._gallery_card(kind, payload)
            card.grid(
                row=index // 3,
                column=index % 3,
                sticky="nsew",
                padx=(0 if index % 3 == 0 else 8, 0),
                pady=(0 if index < 3 else 8, 0),
            )

    def _retry_gallery(self) -> None:
        self._gallery_loaded = False
        self._gallery_error = None
        self._render_gallery()
        self._start_gallery_load()

    def _gallery_card(self, kind: str, payload) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            self.gallery_grid,
            fg_color=COLOR_SURFACE,
            corner_radius=10,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        card.grid_columnconfigure(0, weight=1)

        if kind == "local":
            path = Path(payload)
            key = f"local:{path}"
            url = None
            caption = path.name
            status = "Новое изображение"
            action_text = "Убрать"
            action = lambda p=path: self._remove_new_gallery(p)
        else:
            image = payload
            image_id = image.get("id")
            key = f"remote:{image_id}"
            url = media_url(image.get("image"))
            caption = image_name_from_url(url, f"image_{image_id}.jpg")
            if kind == "deleted":
                status = "Будет удалено"
                action_text = "Вернуть"
                action = lambda value=image_id: self._mark_gallery_deleted(
                    value, False
                )
            else:
                status = ""
                action_text = "Удалить"
                action = lambda value=image_id: self._mark_gallery_deleted(
                    value, True
                )

        thumb = self._thumb_cache.get(key)
        if thumb is None and kind == "local":
            try:
                thumb = load_image_thumb(Path(payload), GALLERY_THUMB_SIZE)
            except Exception:
                thumb = None
            if thumb is not None:
                self._thumb_cache[key] = thumb

        if thumb is not None:
            image_label = ctk.CTkLabel(card, text="", image=thumb)
        else:
            placeholder = "Нет превью" if key in self._thumb_failed else "…"
            image_label = ctk.CTkLabel(
                card,
                text=placeholder,
                font=(FONT, 10),
                text_color=COLOR_MUTED,
                height=GALLERY_THUMB_SIZE[1],
            )
        image_label.grid(row=0, column=0, padx=6, pady=(6, 2))

        ctk.CTkLabel(
            card,
            text=caption,
            font=(FONT, 10),
            text_color=COLOR_TEXT if kind != "deleted" else COLOR_DANGER,
            anchor="w",
            wraplength=190,
        ).grid(row=1, column=0, sticky="w", padx=8)

        if status:
            ctk.CTkLabel(
                card,
                text=status,
                font=(FONT, 9),
                text_color=COLOR_DANGER if kind == "deleted" else COLOR_ACCENT,
                anchor="w",
            ).grid(row=2, column=0, sticky="w", padx=8)

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=6, pady=6)

        if url:
            ctk.CTkButton(
                buttons,
                text="Открыть",
                command=lambda link=url: webbrowser.open(link),
                width=74,
                height=26,
                corner_radius=13,
                fg_color=COLOR_CHIP,
                hover_color=COLOR_CHIP_HOVER,
                text_color=COLOR_TEXT,
                font=(FONT, 10),
            ).pack(side="left")
        else:
            ctk.CTkButton(
                buttons,
                text="Открыть",
                command=lambda path=Path(payload): webbrowser.open(path.as_uri()),
                width=74,
                height=26,
                corner_radius=13,
                fg_color=COLOR_CHIP,
                hover_color=COLOR_CHIP_HOVER,
                text_color=COLOR_TEXT,
                font=(FONT, 10),
            ).pack(side="left")

        ctk.CTkButton(
            buttons,
            text=action_text,
            command=action,
            width=82,
            height=26,
            corner_radius=13,
            fg_color=COLOR_DANGER_SOFT if kind != "deleted" else COLOR_CHIP,
            hover_color="#EBCFC7" if kind != "deleted" else COLOR_CHIP_HOVER,
            text_color=COLOR_DANGER if kind != "deleted" else COLOR_TEXT,
            font=(FONT, 10),
        ).pack(side="right")

        if kind != "local" and url and thumb is None and key not in self._thumb_failed:
            self._request_thumb(key, url)
        return card

    def _request_thumb(self, key: str, url: str) -> None:
        if key in self._thumb_loading or key in self._thumb_cache:
            return
        self._thumb_loading.add(key)
        threading.Thread(
            target=self._thumb_worker,
            args=(key, url),
            daemon=True,
            name="gallery-thumb",
        ).start()

    def _thumb_worker(self, key: str, url: str) -> None:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as image:
                image = ImageOps.exif_transpose(image)
                image.load()
                preview = image.convert("RGB")
                preview.thumbnail(GALLERY_THUMB_SIZE, Image.Resampling.LANCZOS)
        except Exception:
            self.app._post(lambda: self._thumb_failed_ui(key))
            return
        # CTkImage создаём только в UI-потоке: Tkinter не потокобезопасен.
        self.app._post(lambda: self._thumb_succeeded_ui(key, preview))

    def _thumb_succeeded_ui(self, key: str, preview: Image.Image) -> None:
        self._thumb_loading.discard(key)
        if self._closed:
            return
        self._thumb_cache[key] = build_thumb(preview, GALLERY_THUMB_SIZE)
        self._render_gallery()

    def _thumb_failed_ui(self, key: str) -> None:
        self._thumb_loading.discard(key)
        self._thumb_failed.add(key)
        if self._closed:
            return
        self._render_gallery()

    def _add_gallery_files(self) -> None:
        if self._busy or self._closed:
            return
        selected = filedialog.askopenfilenames(
            parent=self,
            title="Изображения галереи",
            filetypes=PHOTO_FILETYPES,
        )
        if not selected:
            return
        errors: list[str] = []
        for raw in selected:
            path = Path(raw)
            error, _content_type = validate_image_file(path)
            if error:
                errors.append(f"{path.name}: {error}")
                continue
            if path in self._gallery_new:
                continue
            self._gallery_new.append(path)
            try:
                self._thumb_cache[f"local:{path}"] = load_image_thumb(
                    path, GALLERY_THUMB_SIZE
                )
            except Exception:
                pass
        if errors:
            self.gallery_hint.configure(
                text=" ".join(errors), text_color=COLOR_DANGER
            )
        else:
            self.gallery_hint.configure(text="", text_color=COLOR_MUTED)
        self._render_gallery()

    def _remove_new_gallery(self, path: Path) -> None:
        if self._busy:
            return
        if path in self._gallery_new:
            self._gallery_new.remove(path)
        self._thumb_cache.pop(f"local:{path}", None)
        self._render_gallery()

    def _mark_gallery_deleted(self, image_id, delete: bool) -> None:
        if self._busy:
            return
        try:
            image_id = int(image_id)
        except (TypeError, ValueError):
            return
        if delete:
            self._gallery_deleted.add(image_id)
        else:
            self._gallery_deleted.discard(image_id)
        self._render_gallery()

    # -------------------------------------------------------- валидация

    @staticmethod
    def _parse_decimal(raw: str) -> Decimal | None:
        try:
            number = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            return None
        return number if number.is_finite() else None

    def _collect_values(self) -> dict | None:
        """Проверяет форму и возвращает желаемое состояние полей."""
        errors: dict[str, str] = {}

        name = self.name_entry.get().strip()
        if not name:
            errors["name"] = "Укажите название."
        elif len(name) > 200:
            errors["name"] = "Не больше 200 символов."

        place = self.place_entry.get().strip()
        if not place:
            errors["place_of_purchase"] = "Укажите место покупки."
        elif len(place) > 200:
            errors["place_of_purchase"] = "Не больше 200 символов."

        tea_type = TYPE_CODES.get(self.type_combo.get())
        if tea_type is None:
            errors["tea_type"] = "Выберите тип чая."

        region = REGION_CODES.get(self.region_combo.get())
        if region is None:
            errors["place_of_harvest"] = "Выберите регион."

        year_value: int | None = None
        year_raw = self.year_entry.get().strip()
        if year_raw:
            try:
                year_value = int(year_raw)
            except ValueError:
                errors["year_of_harvest"] = "Год должен быть целым числом."
            else:
                if not 1900 <= year_value <= 2100:
                    errors["year_of_harvest"] = "Год должен быть от 1900 до 2100."
                    year_value = None

        price_value: str | None = None
        price_raw = self.price_entry.get().strip().replace(",", ".")
        if price_raw:
            number = self._parse_decimal(price_raw)
            if number is None:
                errors["price"] = "Цена должна быть числом."
            elif number < 0:
                errors["price"] = "Цена не может быть отрицательной."
            elif -number.as_tuple().exponent > 2:
                errors["price"] = "Не больше 2 знаков после запятой."
            elif number.adjusted() > 3:
                errors["price"] = "Слишком большая цена (до 9999.99)."
            else:
                price_value = format(number, "f")

        mark_value: str | None = None
        mark_raw = self.mark_entry.get().strip().replace(",", ".")
        if mark_raw:
            number = self._parse_decimal(mark_raw)
            if number is None:
                errors["mark"] = "Оценка должна быть числом."
            elif number < 0 or number > 10:
                errors["mark"] = "Оценка должна быть от 0 до 10."
            elif -number.as_tuple().exponent > 1:
                errors["mark"] = "Не больше одного знака после запятой."
            else:
                mark_value = format(number, "f")

        description = self.description_box.get("1.0", "end").strip()

        if self._photo_action == "replace":
            if self._photo_path is None:
                errors["photo"] = "Файл фото не выбран."
            else:
                error, _content_type = validate_image_file(self._photo_path)
                if error:
                    errors["photo"] = error

        for path in self._gallery_new:
            error, _content_type = validate_image_file(path)
            if error:
                errors.setdefault(
                    "photo",
                    f"Галерея, {path.name}: {error}",
                )

        if errors:
            self._show_errors(errors, "Проверьте выделенные поля.")
            return None

        return {
            "name": name,
            "place_of_purchase": place,
            "tea_type": tea_type,
            "place_of_harvest": region,
            "availability": bool(self.availability_switch.get()),
            "year_of_harvest": year_value,
            "price": price_value,
            "mark": mark_value,
            "description": description,
        }

    @staticmethod
    def _payload_for_create(values: dict) -> dict:
        """Создание: отсутствие ключа и null значат разное."""
        return {
            key: value
            for key, value in values.items()
            if value is not None and value != ""
        }

    def _payload_for_update(self, values: dict) -> dict:
        """PATCH: только изменённые поля; очистка уходит как null/""."""
        original = self.tea or {}
        changes: dict = {}

        for key, value in values.items():
            old = original.get(key)
            if key in ("price", "mark"):
                # "8.5" и "8.50" — одно и то же значение.
                if _to_decimal(value) != _to_decimal(old):
                    changes[key] = value
            elif key == "year_of_harvest":
                if value != old:
                    changes[key] = value
            elif key == "availability":
                if bool(value) != bool(old):
                    changes[key] = value
            else:
                if (value or "") != (old or ""):
                    changes[key] = value

        return changes

    # ------------------------------------------------------------ отправка

    def _submit(self) -> None:
        if self._busy or self._closed:
            return
        if getattr(self.app, "_busy", False):
            self.banner.configure(text="Дождитесь окончания операции.")
            return

        values = self._collect_values()
        if values is None:
            return

        is_edit = self.is_edit
        photo_path = (
            self._photo_path if self._photo_action == "replace" else None
        )

        if is_edit:
            payload = self._payload_for_update(values)
            if self._photo_action == "clear" and self._photo_existing_url:
                payload["photo"] = None
            if (
                not payload
                and photo_path is None
                and not self._gallery_changes_pending()
            ):
                self.banner.configure(text="Изменений нет.")
                return
        else:
            payload = self._payload_for_create(values)

        self.banner.configure(text="")
        self._set_busy(True)
        threading.Thread(
            target=self._worker,
            args=(
                payload,
                photo_path,
                is_edit,
                sorted(self._gallery_deleted),
                list(self._gallery_new),
            ),
            daemon=True,
            name="tea-update" if is_edit else "tea-create",
        ).start()

    def _worker(
        self,
        payload: dict,
        photo_path: Path | None,
        is_edit: bool,
        deleted_images: list[int],
        new_images: list[Path],
    ) -> None:
        tea_id = tea_int_id(self.tea or {})
        try:
            updated = self._save_tea(
                payload, photo_path, is_edit, tea_id
            )
        except ApiError as exc:
            self.app._post(lambda error=exc: self._on_error(error))
            return
        except OSError as exc:
            error = ApiError(
                f"Не удалось прочитать файл: {exc}",
                field_errors={"photo": [str(exc)]},
            )
            self.app._post(lambda error=error: self._on_error(error))
            return
        except Exception as exc:
            LOGGER.exception("Не удалось сохранить сорт")
            message = f"Неожиданная ошибка: {exc}"
            self.app._post(
                lambda text=message: self._on_error(ApiError(text))
            )
            return

        actual_id = tea_int_id(updated) or tea_id
        warnings: list[str] = []
        if actual_id is not None:
            for image_id in deleted_images:
                try:
                    self.api.delete_image(image_id)
                except ApiError as exc:
                    warnings.append(
                        f"изображение #{image_id} не удалено: {exc.message}"
                    )
                except Exception as exc:
                    LOGGER.exception("Не удалось удалить изображение")
                    warnings.append(
                        f"изображение #{image_id} не удалено: {exc}"
                    )
            for path in new_images:
                try:
                    self.api.create_image(actual_id, path)
                except ApiError as exc:
                    warnings.append(
                        f"{path.name} не загружено: {exc.message}"
                    )
                except Exception as exc:
                    LOGGER.exception("Не удалось загрузить изображение")
                    warnings.append(
                        f"{path.name} не загружено: {exc}"
                    )

            try:
                updated = self.api.get_tea(actual_id)
            except Exception as exc:
                LOGGER.warning("Не удалось перечитать сорт: %s", exc)

        self.app._post(
            lambda tea=updated, notes=warnings: self._on_success(tea, notes)
        )

    def _save_tea(
        self,
        payload: dict,
        photo_path: Path | None,
        is_edit: bool,
        tea_id: int | None,
    ) -> dict:
        if not is_edit:
            if photo_path is None:
                return self.api.create_tea(payload)
            content_type = (
                mimetypes.guess_type(photo_path.name)[0]
                or "application/octet-stream"
            )
            with photo_path.open("rb") as handle:
                files = {
                    "photo": (photo_path.name, handle, content_type)
                }
                return self.api.create_tea(payload, files=files)

        if tea_id is None:
            raise ApiError("У записи нет числового ID.")
        if photo_path is None:
            if not payload:
                return self.api.get_tea(tea_id)
            return self.api.update_tea(tea_id, payload)

        content_type = (
            mimetypes.guess_type(photo_path.name)[0]
            or "application/octet-stream"
        )
        with photo_path.open("rb") as handle:
            files = {"photo": (photo_path.name, handle, content_type)}
            return self.api.update_tea(tea_id, payload, files=files)

    def _on_success(self, tea: dict, warnings: list[str]) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._close(force=True)
        self.on_saved(tea, warnings, self.is_edit)

    def _on_error(self, exc: ApiError) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._set_busy(False)
        general = exc.message
        if exc.status == 403:
            general += " Проверьте учётные данные или режим «только просмотр»."
        elif exc.status == 404:
            general = (
                "Запись не найдена: возможно, её удалили. "
                "Закройте форму и обновите список (F5)."
            )
            for button in self._save_buttons:
                button.configure(state="disabled")
        self._show_errors(exc.field_errors, general)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        idle_text = "Применить" if self.is_edit else "Сохранить"
        for button in self._save_buttons:
            button.configure(
                state=state,
                text="Сохранение…" if busy else idle_text,
            )
        for button in self._cancel_buttons:
            button.configure(state=state)
        if self.delete_button is not None:
            self.delete_button.configure(state=state)
        self.photo_button.configure(state=state)
        self.gallery_add_button.configure(state=state)
        self._update_photo_buttons()

    # -------------------------------------------------------------- удаление

    def _delete(self) -> None:
        if self._busy or self._closed or not self.is_edit:
            return
        tea_id = tea_int_id(self.tea or {})
        if tea_id is None:
            return
        ConfirmDialog(
            self,
            title="Удаление сорта",
            message=(
                "Сорт будет удалён без возможности восстановления. "
                "Изображения галереи удалятся каскадно."
            ),
            details=tea_title(self.tea),
            confirm_text="Удалить",
            danger=True,
            on_confirm=lambda: self._perform_delete(tea_id),
        )

    def _perform_delete(self, tea_id: int) -> None:
        if self._busy or self._closed:
            return
        self._set_busy(True)
        threading.Thread(
            target=self._delete_worker,
            args=(tea_id,),
            daemon=True,
            name="tea-delete",
        ).start()

    def _delete_worker(self, tea_id: int) -> None:
        try:
            self.api.delete_tea(tea_id)
        except ApiError as exc:
            self.app._post(lambda error=exc: self._on_error(error))
            return
        except Exception as exc:
            LOGGER.exception("Не удалось удалить сорт")
            message = f"Неожиданная ошибка: {exc}"
            self.app._post(
                lambda text=message: self._on_error(ApiError(text))
            )
            return
        self.app._post(lambda: self._on_delete_success(tea_id))

    def _on_delete_success(self, tea_id: int) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._close(force=True)
        self.on_deleted(tea_id)

    # --------------------------------------------------------------- ошибки

    def _set_error_label(self, label: ctk.CTkLabel, text: str) -> None:
        label.configure(text=text)
        try:
            packed = bool(label.winfo_manager())
        except tk.TclError:
            return
        if text and not packed:
            label.pack(fill="x")
        elif not text and packed:
            label.pack_forget()

    def _clear_errors(self) -> None:
        for label in self._errors.values():
            self._set_error_label(label, "")
        self.banner.configure(text="")

    def _show_errors(self, field_errors, general: str = "") -> None:
        self._clear_errors()
        extra: list[str] = []

        for key, value in field_errors.items():
            messages = value if isinstance(value, (list, tuple)) else [value]
            text = " ".join(str(message) for message in messages).strip()
            if not text:
                continue
            label = self._errors.get(key)
            if label is not None:
                self._set_error_label(label, text)
            elif key == "non_field_errors":
                extra.append(text)
            else:
                extra.append(f"{key}: {text}")

        banner_text = " ".join(
            part for part in (general, *extra) if part
        ).strip()
        self.banner.configure(text=banner_text)

    # --------------------------------------------------------------- окно

    def _close(self, force: bool = False) -> None:
        # Во время запроса окно не закрываем: ответ сервера должен
        # прийти в ещё существующие виджеты.
        if self._busy and not force:
            return
        if self._closed:
            return
        self._closed = True

        try:
            self.grab_release()
        except tk.TclError:
            pass
        if getattr(self.app, "_form_dialog", None) is self:
            self.app._form_dialog = None
            self.app._update_actions()
        try:
            self.destroy()
        except tk.TclError:
            pass


def main() -> None:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

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
