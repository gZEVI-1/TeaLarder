"""
Запуск: .venv\\Scripts\\python.exe desktop_admin.py
"""
import io
import math
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin

import customtkinter as ctk
import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageOps

APP_TITLE = "Чайная Лавка"
BASE_URL = "http://127.0.0.1:8000"
API_TEAS_URL = f"{BASE_URL}/api/teas/"
REQUEST_TIMEOUT = 10.0
PROBE_TIMEOUT = 3.0
ENV_PATH = Path(__file__).resolve().parent / ".env"

FONT = "Segoe UI"
COLOR_BG = "#ECF0ED"
COLOR_SURFACE = "#FFFFFF"
COLOR_TEXT = "#1C2B22"
COLOR_MUTED = "#7B8580"
COLOR_BORDER = "#E4E9E6"
COLOR_ACCENT = "#2E7D5B"
COLOR_ACCENT_HOVER = "#25664A"
COLOR_SOFT = "#EAF3EE"
COLOR_DANGER = "#C44536"
COLOR_WARN = "#C98A1B"
COLOR_MARK = "#A8741A"
COLOR_CHIP = "#E8EFEA"
COLOR_CHIP_HOVER = "#DCE7E0"

CARD_MIN_WIDTH = 210
CARD_GAP = 14
PAGE_SIZE = 12
RESIZE_DEBOUNCE_MS = 450

FILTER_OPTIONS = (
    ("Все", "all"),
    ("В наличии", "available"),
    ("Нет в наличии", "unavailable"),
    ("Новинки", "new"),
)

TEA_TYPE_LABELS = {
    "white": "Белый",
    "green": "Зеленый",
    "matcha": "Матча",
    "yellow": "Желтый",
    "oolongNF": "Улун северо-фуцзяньский",
    "oolongSF": "Улун южно-фуцзяньский",
    "oolongGD": "Улун гуаньдунский",
    "oolongTW": "Улун тайваньский",
    "oolongGaba": "Улун габа",
    "red": "Красный",
    "black": "Хэй Ча",
    "puerSHU": "Шу Пуэр",
    "puerSHEN": "Шен Пуэр",
    "herbal": "Травяной",
    "flavored": "Ароматизированный",
    "other": "Другое",
}

REGION_LABELS = {
    "China": "Китай",
    "Anhui": "Аньхой",
    "Fujian": "Фуцзянь",
    "Guangdong": "Гуандун",
    "Guangxi": "Гуанси",
    "Hainan": "Хайнань",
    "Hebei": "Хэбэй",
    "Henan": "Хэнань",
    "Hubei": "Хубэй",
    "Hunan": "Хунань",
    "Jiangsu": "Цзянсу",
    "Jiangxi": "Цзянси",
    "Qinghai": "Цинхай",
    "Shandong": "Шаньдун",
    "Shanxi": "Шаньси",
    "Sichuan": "Сычуань",
    "Taiwan": "Тайвань",
    "Yunnan": "Юньнань",
    "Zhejiang": "Чжэцзян",
    "Sri Lanka": "Шри-Ланка",
    "Nepal": "Непал",
    "Japan": "Япония",
    "India": "Индия",
    "Assam": "Ассам",
    "Kenya": "Кения",
    "Russia": "Россия",
    "Other": "Другое",
}


def load_credentials() -> tuple[str, str]:
    """Читает TEA_API_USER / TEA_API_PASSWORD из .env или окружения."""
    load_dotenv(ENV_PATH)
    username = os.getenv("TEA_API_USER", "").strip()
    password = os.getenv("TEA_API_PASSWORD", "").strip()
    if not username or not password:
        raise ValueError(
            "Не найдены TEA_API_USER / TEA_API_PASSWORD в .env или окружении."
        )
    return username, password


def build_session(username: str = "", password: str = "") -> requests.Session:
    """Сессия с JSON-Accept и, если переданы, Basic-учётными данными."""
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "TeaAdminDesktop/0.1",
    })
    if username and password:
        session.auth = (username, password)
    return session


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    status: int | None
    message: str
    count: int | None = None


def probe_api(session: requests.Session, timeout: float = PROBE_TIMEOUT) -> ProbeResult:
    """GET /api/teas/ с диагностикой: сервер, учётные данные, формат ответа."""
    try:
        response = session.get(API_TEAS_URL, timeout=timeout)
        status = response.status_code
        if status == 200:
            try:
                data = response.json()
            except ValueError:
                return ProbeResult(ok=False, status=status, message="Ошибка JSON")
            if isinstance(data, dict) and "results" in data:
                return ProbeResult(
                    ok=True,
                    status=status,
                    message="Сервер доступен, учётные данные приняты",
                    count=data.get("count"),
                )
            if isinstance(data, list):
                return ProbeResult(
                    ok=True,
                    status=status,
                    message="Сервер доступен, учётные данные приняты",
                    count=len(data),
                )
            return ProbeResult(ok=False, status=status, message="Неожиданный формат ответа")
        if status == 401:
            return ProbeResult(ok=False, status=status, message="Ошибка авторизации (401)")
        if status == 403:
            return ProbeResult(ok=False, status=status, message="Доступ запрещён (403)")
        if status == 404:
            return ProbeResult(ok=False, status=status, message="Не найден (404)")
        return ProbeResult(ok=False, status=status, message=f"HTTP {status}")
    except requests.exceptions.ConnectionError:
        return ProbeResult(False, None, f"Сервер недоступен: {BASE_URL}")
    except requests.exceptions.Timeout:
        return ProbeResult(False, None, f"Сервер не ответил за {timeout} с")
    except requests.exceptions.RequestException as exc:
        return ProbeResult(False, None, f"Ошибка запроса: {exc}")
    except Exception as exc:
        return ProbeResult(False, None, f"Неожиданная ошибка: {exc}")


def fetch_teas(
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[list[dict], str | None]:
    """Загружает все страницы /api/teas/."""
    items: list[dict] = []
    url = API_TEAS_URL
    for _ in range(100):
        try:
            response = session.get(url, timeout=timeout)
        except requests.exceptions.Timeout:
            return [], f"Сервер не ответил за {timeout} с"
        except requests.exceptions.RequestException as exc:
            return [], f"Ошибка запроса: {exc}"

        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"

        try:
            data = response.json()
        except ValueError:
            return [], "Ошибка JSON"

        if isinstance(data, list):
            items.extend(data)
            break
        if not isinstance(data, dict):
            return [], "Неожиданный формат ответа"

        results = data.get("results")
        if isinstance(results, list):
            items.extend(results)
        next_url = data.get("next")
        if not next_url:
            break
        url = urljoin(BASE_URL + "/", next_url)

    return items, None


def media_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return urljoin(BASE_URL + "/", value)


def plural_tea(count: int) -> str:
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return "чаёв"
    tail %= 10
    if tail == 1:
        return "чай"
    if 2 <= tail <= 4:
        return "чая"
    return "чаёв"


def shorten(value: str | None, limit: int) -> str:
    text = " ".join((value or "").split())
    if not text:
        return "—"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_price(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " ₽"


def format_mark(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "★ —"
    return "★ " + f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_date(value: str | None) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%d.%m.%Y") if parsed else "—"


@lru_cache(maxsize=64)
def rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    scale_factor = 4
    mask = Image.new("L", (width * scale_factor, height * scale_factor), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, width * scale_factor - 1, height * scale_factor - 1),
        radius=radius * scale_factor,
        fill=255,
    )
    return mask.resize((width, height), Image.Resampling.LANCZOS)


def cover_image(
    image: Image.Image,
    width: int,
    height: int,
    radius: int = 0,
) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    scale = max(width / image.width, height / image.height)
    resized_width = max(width, int(image.width * scale + 0.5))
    resized_height = max(height, int(image.height * scale + 0.5))
    resized = image.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    left = (resized_width - width) // 2
    top = (resized_height - height) // 2
    result = resized.crop((left, top, left + width, top + height)).convert("RGBA")

    if radius > 0:
        result.putalpha(rounded_mask(width, height, radius))

    return result


@lru_cache(maxsize=32)
def placeholder_photo(width: int, height: int) -> Image.Image:
    """Серое стоковое изображение для чаёв без фотографии."""
    width = max(1, int(width))
    height = max(1, int(height))
    scale = 2
    canvas_width = width * scale
    canvas_height = height * scale
    image = Image.new("RGB", (canvas_width, canvas_height), "#E6EAE8")
    draw = ImageDraw.Draw(image)

    top = (244, 246, 245)
    bottom = (212, 219, 215)
    for y in range(canvas_height):
        ratio = y / max(1, canvas_height - 1)
        color = tuple(
            int(start + (end - start) * ratio)
            for start, end in zip(top, bottom)
        )
        draw.line((0, y, canvas_width, y), fill=color)

    center_x = canvas_width / 2
    center_y = canvas_height / 2
    glow = min(canvas_width, canvas_height) * 0.42
    draw.ellipse(
        (center_x - glow, center_y - glow, center_x + glow, center_y + glow),
        fill=(236, 239, 237),
    )

    leaf_length = min(canvas_width, canvas_height) * 0.52
    leaf_width = leaf_length * 0.46
    angle = math.radians(-24)
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)

    def rotate(x: float, y: float) -> tuple[float, float]:
        return (
            center_x + x * cos_angle - y * sin_angle,
            center_y + x * sin_angle + y * cos_angle,
        )

    upper: list[tuple[float, float]] = []
    lower: list[tuple[float, float]] = []
    steps = 60
    for step in range(steps + 1):
        ratio = step / steps
        x = (ratio - 0.5) * leaf_length
        half = (leaf_width / 2) * math.sin(math.pi * ratio)
        upper.append(rotate(x, -half))
        lower.append(rotate(x, half))
    draw.polygon(upper + lower[::-1], fill=(166, 176, 170))

    vein_width = max(2, scale * 2)
    draw.line(
        (rotate(-leaf_length * 0.46, 0), rotate(leaf_length * 0.46, 0)),
        fill=(146, 157, 150),
        width=vein_width,
    )
    draw.line(
        (rotate(-leaf_length * 0.44, 0), rotate(-leaf_length * 0.62, 0)),
        fill=(146, 157, 150),
        width=vein_width,
    )

    image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image.convert("RGBA")


class TeaAdminApp(ctk.CTk):
    def __init__(self, session: requests.Session) -> None:
        super().__init__()
        self.session = session
        self.title(APP_TITLE)
        self.geometry("780x420")
        self.minsize(680, 380)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._teas: list[dict] = []
        self._card_widgets: dict[str, ctk.CTkFrame] = {}
        self._state_frame: ctk.CTkFrame | None = None
        self._filter_key = "all"
        self._busy = False
        self._load_token = 0
        self._loaded = False
        self._last_error: str | None = None
        self._columns = 0
        self._card_width = 0
        self._relayout_job = None
        self._search_job = None
        self._last_query = ""
        self._shown = PAGE_SIZE
        self._total_filtered = 0
        self._footer: ctk.CTkFrame | None = None
        self._footer_button: ctk.CTkButton | None = None
        self._details_window: ctk.CTkToplevel | None = None
        self._photos: dict[str, Image.Image | None] = {}
        self._photo_pending: dict[tuple[str, int, int], list] = {}
        self._prepared: dict[tuple[str, int, int], Image.Image | None] = {}
        self._image_cache: dict[tuple, ctk.CTkImage] = {}
        self._photo_queue: queue.Queue[tuple[str, int, int, int] | None] = queue.Queue()
        self._photo_lock = threading.Lock()
        self._closing = False

        for index in range(4):
            threading.Thread(
                target=self._photo_worker,
                daemon=True,
                name=f"tea-photo-{index}",
            ).start()

        self._build_ui()
        self.after(150, self.refresh_status)

    def _build_ui(self) -> None:
        self.configure(fg_color=COLOR_BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color=COLOR_SURFACE, corner_radius=0, height=64)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=(20, 12), pady=12, sticky="w")

        logo = ctk.CTkLabel(
            brand,
            text="Ч",
            width=38,
            height=38,
            corner_radius=12,
            fg_color=COLOR_ACCENT,
            text_color="#FFFFFF",
            font=(FONT, 18, "bold"),
        )
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 10))

        ctk.CTkLabel(
            brand,
            text="Чайная лавка",
            font=(FONT, 16, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(
            brand,
            text="коллекция и учёт",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        ).grid(row=1, column=1, sticky="w")

        self.search_entry = ctk.CTkEntry(
            header,
            placeholder_text="Поиск: название, тип, регион",
            height=36,
            corner_radius=18,
            border_width=1,
            border_color=COLOR_BORDER,
            fg_color=COLOR_BG,
            text_color=COLOR_TEXT,
            placeholder_text_color=COLOR_MUTED,
            font=(FONT, 12),
        )
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=14)
        self.search_entry.bind("<KeyRelease>", self._on_search_changed, add="+")
        self.search_entry.bind("<Return>", self._on_search_changed, add="+")
        self.after(400, self._watch_search)

        self.check_button = ctk.CTkButton(
            header,
            text="Проверить снова",
            command=self.refresh_status,
            width=142,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=(FONT, 12, "bold"),
        )
        self.check_button.grid(row=0, column=2, padx=(0, 20), pady=14)

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=20, pady=(12, 8))
        toolbar.grid_columnconfigure(len(FILTER_OPTIONS), weight=1)

        self.filter_buttons: dict[str, ctk.CTkButton] = {}
        for index, (label, key) in enumerate(FILTER_OPTIONS):
            button = ctk.CTkButton(
                toolbar,
                text=label,
                command=lambda value=key: self._set_filter(value),
                width=max(56, int(len(label) * 7.2) + 22),
                height=30,
                corner_radius=15,
                border_width=0,
                fg_color=COLOR_ACCENT if key == "all" else COLOR_CHIP,
                hover_color=COLOR_ACCENT_HOVER if key == "all" else COLOR_CHIP_HOVER,
                text_color="#FFFFFF" if key == "all" else COLOR_TEXT,
                font=(FONT, 11, "bold" if key == "all" else "normal"),
            )
            button.grid(row=0, column=index, padx=(0, 6), sticky="w")
            self.filter_buttons[key] = button

        status_box = ctk.CTkFrame(toolbar, fg_color="transparent")
        status_box.grid(row=0, column=len(FILTER_OPTIONS), sticky="e")
        status_box.grid_columnconfigure(1, weight=1)

        self.status_dot = ctk.CTkLabel(
            status_box,
            text="●",
            width=12,
            font=(FONT, 10),
            text_color=COLOR_WARN,
        )
        self.status_dot.grid(row=0, column=0, padx=(0, 5))

        self.status_label = ctk.CTkLabel(
            status_box,
            text="Подключение…",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="e",
            justify="right",
            wraplength=240,
        )
        self.status_label.grid(row=0, column=1, sticky="e")

        self.scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#CFD8D2",
            scrollbar_button_hover_color="#B9C6BE",
        )
        self.scroll.grid(row=2, column=0, sticky="nsew", padx=(14, 8), pady=(0, 12))
        self.scroll.grid_columnconfigure(0, weight=1)

        self.bind("<Configure>", self._on_window_configure, add="+")
        self.bind("<F5>", lambda _event: self.refresh_status(), add="+")
        self.bind("<Control-f>", lambda _event: self.search_entry.focus_set(), add="+")
        self.bind_all("<MouseWheel>", self._on_mouse_wheel, add="+")
        self._show_state(
            "Подключаемся к серверу…",
            "Проверяем API и загружаем коллекцию чая.",
        )

    def refresh_status(self) -> None:
        if self._busy:
            return
        self._load_token += 1
        token = self._load_token
        self._set_busy(True)
        self._set_status("Проверяем подключение…", COLOR_WARN)
        threading.Thread(
            target=self._load_worker,
            args=(token,),
            daemon=True,
            name="tea-loader",
        ).start()

    def _load_worker(self, token: int) -> None:
        result = probe_api(self.session)
        items: list[dict] | None = None
        error: str | None = None
        if result.ok:
            items, error = fetch_teas(self.session)
        self._post(lambda: self._finish_load(token, result, items, error))

    def _finish_load(
        self,
        token: int,
        result: ProbeResult,
        items: list[dict] | None,
        error: str | None,
    ) -> None:
        if token != self._load_token or self._closing:
            return
        self._set_busy(False)
        if not result.ok:
            self._render(result)
            return
        if error:
            self._last_error = error
            self._set_status(f"Ошибка загрузки: {error}", COLOR_DANGER)
            if not self._teas:
                self._show_state(
                    "Не удалось загрузить коллекцию",
                    f"{error}. Проверьте, что сервер запущен, и повторите попытку.",
                    error=True,
                )
            return

        self._teas = items or []
        self._loaded = True
        self._last_error = None
        self._discard_cards()
        self._render_cards(reset=True)
        self._set_status(
            f"Сервер доступен · {len(self._teas)} {plural_tea(len(self._teas))}",
            COLOR_ACCENT,
        )

    def _render(self, result: ProbeResult) -> None:
        if result.ok:
            text = "Сервер доступен"
            if result.count is not None:
                text = f"Сервер доступен · {result.count} {plural_tea(result.count)}"
            self._set_status(text, COLOR_ACCENT)
            return

        self._last_error = result.message
        self._set_status(result.message, COLOR_DANGER)
        if not self._teas:
            self._show_state(
                "Нет связи с сервером",
                f"{result.message}. Запустите python manage.py runserver и нажмите «Проверить снова».",
                error=True,
            )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.check_button.configure(
            state="disabled" if busy else "normal",
            text="Проверка..." if busy else "Проверить снова",
        )

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.configure(text=text, text_color=color)
        self.status_dot.configure(text_color=color)

    def _show_state(self, title: str, subtitle: str, error: bool = False) -> None:
        self._hide_cards()
        self._clear_state()
        if self._columns <= 0:
            self._apply_grid(1)
        frame = ctk.CTkFrame(self.scroll, fg_color="transparent")
        frame.grid(
            row=0,
            column=0,
            columnspan=max(1, self._columns),
            sticky="nsew",
            padx=20,
            pady=(60, 40),
        )
        frame.grid_columnconfigure(0, weight=1)

        accent = COLOR_DANGER if error else COLOR_ACCENT
        soft = "#FBEAE7" if error else COLOR_SOFT
        icon = ctk.CTkLabel(
            frame,
            text="!" if error else "Ч",
            width=44,
            height=44,
            corner_radius=14,
            fg_color=soft,
            text_color=accent,
            font=(FONT, 20, "bold"),
        )
        icon.grid(row=0, column=0, pady=(0, 12))

        ctk.CTkLabel(
            frame,
            text=title,
            font=(FONT, 16, "bold"),
            text_color=COLOR_TEXT,
        ).grid(row=1, column=0)

        ctk.CTkLabel(
            frame,
            text=subtitle,
            font=(FONT, 12),
            text_color=COLOR_MUTED,
            justify="center",
            wraplength=460,
        ).grid(row=2, column=0, pady=(6, 0))
        self._state_frame = frame

    def _clear_state(self) -> None:
        if self._state_frame is None:
            return
        try:
            self._state_frame.destroy()
        except tk.TclError:
            pass
        self._state_frame = None

    def _hide_cards(self) -> None:
        for card in self._card_widgets.values():
            try:
                card.grid_remove()
            except tk.TclError:
                pass
        if self._footer is not None:
            try:
                self._footer.grid_remove()
            except tk.TclError:
                pass

    def _discard_cards(self) -> None:
        self._clear_state()
        for card in self._card_widgets.values():
            try:
                card.destroy()
            except tk.TclError:
                pass
        self._card_widgets.clear()
        if self._footer is not None:
            try:
                self._footer.destroy()
            except tk.TclError:
                pass
        self._footer = None
        self._footer_button = None

    def _apply_grid(self, columns: int) -> None:
        for column in range(8):
            self.scroll.grid_columnconfigure(
                column,
                weight=1 if column < columns else 0,
                uniform="cards" if column < columns else "",
            )
        self._columns = columns

    def _grid_metrics(self) -> tuple[int, int]:
        width = self.scroll.winfo_width()
        if width < 200:
            width = max(600, self.winfo_width() - 60)
        width = max(300, width - 20)
        columns = max(1, min(6, (width + CARD_GAP) // (CARD_MIN_WIDTH + CARD_GAP)))
        card_width = max(CARD_MIN_WIDTH, (width - CARD_GAP * (columns - 1)) // columns)
        return columns, card_width

    def _on_window_configure(self, event) -> None:
        if event.widget is not self:
            return
        if self._relayout_job is not None:
            self.after_cancel(self._relayout_job)
        self._relayout_job = self.after(RESIZE_DEBOUNCE_MS, self._relayout)

    def _relayout(self) -> None:
        self._relayout_job = None
        if not self._teas:
            return
        columns, card_width = self._grid_metrics()
        if columns == self._columns and abs(card_width - self._card_width) < 8:
            return
        if columns != self._columns:
            self._apply_grid(columns)
        self._render_cards(reset=False)

    def _watch_search(self) -> None:
        text = self.search_entry.get()
        if text != self._last_query:
            self._last_query = text
            self._on_search_changed()
        self.after(400, self._watch_search)

    def _on_search_changed(self, *_args) -> None:
        self._last_query = self.search_entry.get()
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(180, self._apply_search)

    def _apply_search(self) -> None:
        self._search_job = None
        self._render_cards(reset=True)

    def _set_filter(self, key: str) -> None:
        self._filter_key = key
        for value, button in self.filter_buttons.items():
            selected = value == key
            button.configure(
                fg_color=COLOR_ACCENT if selected else COLOR_CHIP,
                hover_color=COLOR_ACCENT_HOVER if selected else COLOR_CHIP_HOVER,
                text_color="#FFFFFF" if selected else COLOR_TEXT,
                font=(FONT, 11, "bold" if selected else "normal"),
            )
        self._render_cards(reset=True)

    def _filtered_teas(self) -> list[dict]:
        query = self.search_entry.get().strip().casefold()
        now = datetime.now(timezone.utc)
        fresh_after = now - timedelta(days=30)
        result = []

        for tea in self._teas:
            availability = bool(tea.get("availability"))
            if self._filter_key == "available" and not availability:
                continue
            if self._filter_key == "unavailable" and availability:
                continue
            if self._filter_key == "new":
                created = parse_datetime(tea.get("created_at"))
                if created is None or created < fresh_after:
                    continue

            if query:
                haystack = " ".join(
                    str(part)
                    for part in (
                        tea.get("name"),
                        tea.get("description"),
                        tea.get("place_of_purchase"),
                        TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type")),
                        REGION_LABELS.get(tea.get("place_of_harvest"), tea.get("place_of_harvest")),
                        tea.get("year_of_harvest"),
                        tea.get("price"),
                    )
                    if part
                ).casefold()
                if query not in haystack:
                    continue

            result.append(tea)

        result.sort(
            key=lambda tea: (
                not bool(tea.get("availability")),
                (tea.get("name") or "").casefold(),
            )
        )
        return result

    @staticmethod
    def _tea_key(tea: dict) -> str:
        value = tea.get("id")
        if value is not None:
            return str(value)
        return str(id(tea))

    def _render_cards(self, reset: bool = False) -> None:
        columns, card_width = self._grid_metrics()
        if columns != self._columns:
            self._apply_grid(columns)
        if abs(card_width - self._card_width) >= 8:
            self._discard_cards()
        self._card_width = card_width

        teas = self._filtered_teas()
        self._total_filtered = len(teas)
        if reset:
            self._shown = PAGE_SIZE

        if not teas:
            if self._teas:
                title = "Ничего не найдено"
                subtitle = "Измените запрос или выберите другой фильтр."
            elif self._last_error:
                title = "Нет данных"
                subtitle = self._last_error
            else:
                title = "Коллекция пуста"
                subtitle = "Добавьте чаи через админку Django или API, затем обновите список."
            self._show_state(title, subtitle, error=bool(self._last_error) and not self._teas)
            if reset:
                self._reset_scroll()
            return

        self._clear_state()
        self._shown = min(len(teas), max(PAGE_SIZE, self._shown))
        visible = teas[: self._shown]
        visible_keys: set[str] = set()

        for index, tea in enumerate(visible):
            key = self._tea_key(tea)
            visible_keys.add(key)
            card = self._card_widgets.get(key)
            if card is None:
                card = self._build_card(tea, card_width)
                self._card_widgets[key] = card
            card.grid(
                row=index // columns,
                column=index % columns,
                sticky="nsew",
                padx=CARD_GAP // 2,
                pady=CARD_GAP // 2,
            )

        for key, card in self._card_widgets.items():
            if key in visible_keys:
                continue
            try:
                card.grid_remove()
            except tk.TclError:
                pass

        self._place_footer(len(teas) - self._shown, columns)

        if reset:
            self._reset_scroll()

    def _place_footer(self, hidden: int, columns: int) -> None:
        if hidden <= 0:
            if self._footer is not None:
                try:
                    self._footer.grid_remove()
                except tk.TclError:
                    pass
            return

        if self._footer is None:
            footer = ctk.CTkFrame(self.scroll, fg_color="transparent")
            footer.grid_columnconfigure(0, weight=1)
            self._footer_button = ctk.CTkButton(
                footer,
                text="",
                command=self._load_more,
                width=210,
                height=38,
                corner_radius=19,
                fg_color=COLOR_SURFACE,
                hover_color=COLOR_CHIP,
                text_color=COLOR_ACCENT,
                border_width=1,
                border_color=COLOR_BORDER,
                font=(FONT, 12, "bold"),
            )
            self._footer_button.grid(row=0, column=0)
            self._footer = footer

        if self._footer_button is not None:
            self._footer_button.configure(text=f"Показать ещё ({hidden})")
        self._footer.grid(
            row=(self._shown + columns - 1) // columns,
            column=0,
            columnspan=columns,
            pady=16,
        )

    def _load_more(self) -> None:
        teas = self._filtered_teas()
        self._total_filtered = len(teas)
        if self._shown >= len(teas):
            return
        self._shown = min(len(teas), self._shown + PAGE_SIZE)
        self._render_cards(reset=False)

    def _on_mouse_wheel(self, event) -> None:
        try:
            if not self.winfo_exists() or self._shown >= self._total_filtered:
                return
        except tk.TclError:
            return
        canvas = getattr(self.scroll, "_parent_canvas", None)
        widget = event.widget
        inside = False
        while widget is not None:
            if widget is self.scroll or widget is canvas:
                inside = True
                break
            try:
                widget = widget.master
            except AttributeError:
                break
        if not inside:
            return
        self.after(80, self._maybe_load_more)

    def _maybe_load_more(self) -> None:
        canvas = getattr(self.scroll, "_parent_canvas", None)
        if canvas is None:
            return
        try:
            if canvas.yview()[1] >= 0.99:
                self._load_more()
        except tk.TclError:
            pass

    def _reset_scroll(self) -> None:
        canvas = getattr(self.scroll, "_parent_canvas", None)
        if canvas is not None:
            canvas.yview_moveto(0.0)

    def _build_card(self, tea: dict, card_width: int) -> ctk.CTkFrame:
        availability = bool(tea.get("availability"))
        image_width = max(140, card_width - 16)
        image_height = max(110, min(180, int(image_width * 0.62)))
        wrap = max(150, card_width - 30)
        name_limit = max(28, (wrap // 8) * 2 + 4)

        card = ctk.CTkFrame(
            self.scroll,
            fg_color=COLOR_SURFACE,
            corner_radius=16,
            border_width=1,
            border_color=COLOR_BORDER,
        )

        photo_frame = ctk.CTkFrame(
            card,
            fg_color=COLOR_SOFT,
            corner_radius=12,
            width=image_width,
            height=image_height,
        )
        photo_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        photo_frame.grid_propagate(False)

        photo_label = ctk.CTkLabel(
            photo_frame,
            text="",
            fg_color="transparent",
            image=self._placeholder_image(image_width, image_height, 12),
        )
        photo_label.place(relx=0.5, rely=0.5, anchor="center")

        badge_text = "В наличии" if availability else "Нет в наличии"
        badge = ctk.CTkLabel(
            photo_frame,
            text=badge_text,
            width=max(54, int(len(badge_text) * 7) + 16),
            height=22,
            corner_radius=8,
            fg_color=COLOR_ACCENT if availability else "#8A918D",
            text_color="#FFFFFF",
            font=(FONT, 10, "bold"),
        )
        badge.place(x=8, y=8)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=12, pady=(10, 12))
        body.grid_columnconfigure(0, weight=1)

        type_label = ctk.CTkLabel(
            body,
            text=TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type") or "Другое").upper(),
            font=(FONT, 10, "bold"),
            text_color=COLOR_ACCENT if availability else COLOR_MUTED,
            anchor="w",
        )
        type_label.grid(row=0, column=0, sticky="w")

        name_label = ctk.CTkLabel(
            body,
            text=shorten(tea.get("name"), name_limit),
            font=(FONT, 14, "bold"),
            text_color=COLOR_TEXT if availability else COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=wrap,
            height=42,
        )
        name_label.grid(row=1, column=0, sticky="ew", pady=(3, 2))

        region = REGION_LABELS.get(tea.get("place_of_harvest"), tea.get("place_of_harvest"))
        year = tea.get("year_of_harvest")
        meta_parts = [str(part) for part in (region, year) if part]
        meta_label = ctk.CTkLabel(
            body,
            text=" · ".join(meta_parts) or "Регион не указан",
            font=(FONT, 11),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        meta_label.grid(row=2, column=0, sticky="w")

        bottom = ctk.CTkFrame(body, fg_color="transparent")
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bottom.grid_columnconfigure(0, weight=1)

        price_label = ctk.CTkLabel(
            bottom,
            text=format_price(tea.get("price")),
            font=(FONT, 16, "bold"),
            text_color=COLOR_ACCENT if availability else COLOR_MUTED,
            anchor="w",
        )
        price_label.grid(row=0, column=0, sticky="w")

        mark_label = ctk.CTkLabel(
            bottom,
            text=format_mark(tea.get("mark")),
            font=(FONT, 11, "bold"),
            text_color=COLOR_MARK,
            anchor="e",
        )
        mark_label.grid(row=0, column=1, sticky="e")

        purchase_label = ctk.CTkLabel(
            body,
            text="Магазин: " + shorten(tea.get("place_of_purchase"), 30),
            font=(FONT, 10),
            text_color=COLOR_MUTED,
            anchor="w",
            wraplength=wrap,
        )
        purchase_label.grid(row=4, column=0, sticky="w", pady=(6, 0))

        for widget in (
            card,
            photo_frame,
            photo_label,
            badge,
            body,
            type_label,
            name_label,
            meta_label,
            bottom,
            price_label,
            mark_label,
            purchase_label,
        ):
            widget.bind(
                "<Button-1>",
                lambda _event, item=tea: self._open_details(item),
                add="+",
            )

        url = media_url(tea.get("photo"))
        if url:
            radius = 12 if image_height < 200 else 16
            key = (url, image_width, image_height)
            self._request_photo(
                url,
                image_width,
                image_height,
                radius,
                lambda picture, label=photo_label, size_key=key, w=image_width, h=image_height: (
                    self._apply_card_photo(size_key, picture, label, w, h)
                ),
            )

        return card

    def _placeholder_image(
        self,
        width: int,
        height: int,
        radius: int,
    ) -> ctk.CTkImage:
        key = ("__placeholder__", width, height, radius)
        cached = self._image_cache.get(key)
        if cached is not None:
            return cached
        base = placeholder_photo(width, height).convert("RGBA")
        if radius > 0:
            base.putalpha(rounded_mask(width, height, radius))
        if len(self._image_cache) >= 600:
            self._image_cache.clear()
        scaled = ctk.CTkImage(light_image=base, size=(width, height))
        self._image_cache[key] = scaled
        return scaled

    def _apply_card_photo(
        self,
        key: tuple[str, int, int],
        image: Image.Image | None,
        label: ctk.CTkLabel,
        width: int,
        height: int,
    ) -> None:
        if image is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        label.configure(image=self._ctk_image(key, image, width, height), text="")

    def _ctk_image(
        self,
        key: tuple[str, int, int],
        image: Image.Image,
        width: int,
        height: int,
    ) -> ctk.CTkImage:
        cached = self._image_cache.get(key)
        if cached is not None:
            return cached
        if len(self._image_cache) >= 600:
            self._image_cache.clear()
        scaled = ctk.CTkImage(light_image=image, size=(width, height))
        self._image_cache[key] = scaled
        return scaled

    def _request_photo(
        self,
        url: str,
        width: int,
        height: int,
        radius: int,
        callback,
    ) -> None:
        key = (url, width, height)
        if key in self._prepared:
            callback(self._prepared[key])
            return
        if key in self._photo_pending:
            self._photo_pending[key].append(callback)
            return

        self._photo_pending[key] = [callback]
        self._photo_queue.put((url, width, height, radius))

    def _photo_worker(self) -> None:
        while True:
            task = self._photo_queue.get()
            if task is None:
                return
            url, width, height, radius = task
            image = self._prepare_photo(url, width, height, radius)
            if self._closing:
                continue
            key = (url, width, height)
            self._post(lambda size_key=key, picture=image: self._finish_photo(size_key, picture))

    def _finish_photo(
        self,
        key: tuple[str, int, int],
        image: Image.Image | None,
    ) -> None:
        self._prepared[key] = image
        for callback in self._photo_pending.pop(key, []):
            try:
                callback(image)
            except Exception:
                pass

    def _prepare_photo(
        self,
        url: str,
        width: int,
        height: int,
        radius: int,
    ) -> Image.Image | None:
        image = self._get_raw_photo(url)
        if image is None:
            return None
        try:
            return cover_image(image, width, height, radius)
        except Exception:
            return None

    def _get_raw_photo(self, url: str) -> Image.Image | None:
        with self._photo_lock:
            if url in self._photos:
                return self._photos[url]
        image = self._fetch_image(url)
        with self._photo_lock:
            self._photos[url] = image
        return image

    def _fetch_image(self, url: str) -> Image.Image | None:
        try:
            response = self.session.get(url, timeout=8)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content))
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((900, 900), Image.Resampling.LANCZOS)
            return image
        except Exception:
            return None

    def _post(self, callback) -> None:
        try:
            self.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def _open_details(self, tea: dict) -> None:
        if self._details_window is not None:
            try:
                if self._details_window.winfo_exists():
                    self._details_window.lift()
                    self._details_window.focus_force()
                    return
            except tk.TclError:
                pass
            self._details_window = None

        availability = bool(tea.get("availability"))
        window = ctk.CTkToplevel(self)
        self._details_window = window
        window.title(tea.get("name") or "Карточка чая")
        window.geometry("600x720")
        window.minsize(560, 560)
        window.configure(fg_color=COLOR_BG)
        window.transient(self)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(window, fg_color=COLOR_SURFACE, corner_radius=0, height=56)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_propagate(False)
        top.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top,
            text="Карточка чая",
            font=(FONT, 15, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=0, column=0, padx=20, pady=14, sticky="w")

        close_button = ctk.CTkButton(
            top,
            text="Закрыть",
            command=lambda: self._close_details(window),
            width=88,
            height=30,
            corner_radius=15,
            fg_color=COLOR_CHIP,
            hover_color=COLOR_CHIP_HOVER,
            text_color=COLOR_TEXT,
            font=(FONT, 11, "bold"),
        )
        close_button.grid(row=0, column=1, padx=20, pady=12, sticky="e")

        image_width, image_height = 520, 220
        photo_frame = ctk.CTkFrame(
            window,
            fg_color=COLOR_SOFT,
            corner_radius=16,
            width=image_width,
            height=image_height,
        )
        photo_frame.grid(row=1, column=0, pady=(14, 12))
        photo_frame.grid_propagate(False)

        photo_label = ctk.CTkLabel(
            photo_frame,
            text="",
            fg_color="transparent",
            image=self._placeholder_image(image_width, image_height, 16),
        )
        photo_label.place(relx=0.5, rely=0.5, anchor="center")

        badge_text = "В наличии" if availability else "Нет в наличии"
        ctk.CTkLabel(
            photo_frame,
            text=badge_text,
            width=max(60, int(len(badge_text) * 7) + 16),
            height=24,
            corner_radius=9,
            fg_color=COLOR_ACCENT if availability else "#8A918D",
            text_color="#FFFFFF",
            font=(FONT, 10, "bold"),
        ).place(x=12, y=12)

        scroll = ctk.CTkScrollableFrame(
            window,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#CFD8D2",
            scrollbar_button_hover_color="#B9C6BE",
        )
        scroll.grid(row=2, column=0, sticky="nsew", padx=(10, 4), pady=(0, 10))
        scroll.grid_columnconfigure(0, weight=1)

        panel = ctk.CTkFrame(
            scroll,
            fg_color=COLOR_SURFACE,
            corner_radius=16,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        panel.grid(row=0, column=0, sticky="ew", padx=10, pady=(0, 10))
        panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            panel,
            text=tea.get("name") or "Без названия",
            font=(FONT, 21, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=500,
        ).grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))

        type_name = TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type") or "Другое")
        region = REGION_LABELS.get(tea.get("place_of_harvest"), tea.get("place_of_harvest"))
        subtitle_parts = [type_name]
        if region:
            subtitle_parts.append(region)
        if tea.get("year_of_harvest"):
            subtitle_parts.append(str(tea["year_of_harvest"]))
        ctk.CTkLabel(
            panel,
            text=" · ".join(subtitle_parts),
            font=(FONT, 12),
            text_color=COLOR_MUTED,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=20)

        price_row = ctk.CTkFrame(panel, fg_color="transparent")
        price_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(12, 0))
        price_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            price_row,
            text=format_price(tea.get("price")),
            font=(FONT, 24, "bold"),
            text_color=COLOR_ACCENT if availability else COLOR_MUTED,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            price_row,
            text=format_mark(tea.get("mark")),
            font=(FONT, 12, "bold"),
            text_color=COLOR_MARK,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        separator = ctk.CTkFrame(panel, height=1, fg_color=COLOR_BORDER)
        separator.grid(row=3, column=0, sticky="ew", padx=20, pady=14)

        info = ctk.CTkFrame(panel, fg_color="transparent")
        info.grid(row=4, column=0, sticky="ew", padx=20)
        info.grid_columnconfigure(1, weight=1)

        rows = (
            ("Тип", type_name),
            ("Регион", region or "—"),
            ("Год урожая", str(tea.get("year_of_harvest") or "—")),
            ("Место покупки", tea.get("place_of_purchase") or "—"),
            ("Добавлен", format_date(tea.get("created_at"))),
            ("Обновлён", format_date(tea.get("updated_at"))),
        )
        for row_index, (label, value) in enumerate(rows):
            ctk.CTkLabel(
                info,
                text=label,
                font=(FONT, 11),
                text_color=COLOR_MUTED,
                anchor="w",
            ).grid(row=row_index, column=0, sticky="nw", pady=4, padx=(0, 18))
            ctk.CTkLabel(
                info,
                text=str(value),
                font=(FONT, 12),
                text_color=COLOR_TEXT,
                anchor="w",
                justify="left",
                wraplength=340,
            ).grid(row=row_index, column=1, sticky="ew", pady=4)

        ctk.CTkLabel(
            panel,
            text="Описание",
            font=(FONT, 12, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=5, column=0, sticky="w", padx=20, pady=(16, 4))

        ctk.CTkLabel(
            panel,
            text=tea.get("description") or "Описание не добавлено.",
            font=(FONT, 12),
            text_color=COLOR_TEXT if tea.get("description") else COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=500,
        ).grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 20))

        url = media_url(tea.get("photo"))
        if url:
            key = (url, image_width, image_height)
            self._request_photo(
                url,
                image_width,
                image_height,
                16,
                lambda picture, label=photo_label, size_key=key: (
                    self._apply_detail_photo(size_key, picture, label)
                ),
            )

        window.bind("<Escape>", lambda _event: self._close_details(window))
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_details(window))
        window.after(80, lambda: self._focus_details(window))

    def _focus_details(self, window: ctk.CTkToplevel) -> None:
        try:
            if window.winfo_exists():
                window.grab_set()
                window.focus_force()
        except tk.TclError:
            pass

    def _apply_detail_photo(
        self,
        key: tuple[str, int, int],
        image: Image.Image | None,
        label: ctk.CTkLabel,
    ) -> None:
        if image is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        label.configure(image=self._ctk_image(key, image, 520, 220), text="")

    def _close_details(self, window: ctk.CTkToplevel) -> None:
        if self._details_window is window:
            self._details_window = None
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            window.destroy()
        except tk.TclError:
            pass

    def on_close(self) -> None:
        self._closing = True
        for job in (self._relayout_job, self._search_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        for _ in range(4):
            self._photo_queue.put(None)
        self.destroy()


def main() -> None:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")

    try:
        username, password = load_credentials()
    except (ValueError, OSError) as exc:
        print(f"[i] {exc}")
        print("[i] Запуск без учётных данных: доступен только просмотр.")
        session = build_session()
    else:
        session = build_session(username, password)

    app = TeaAdminApp(session)
    app.mainloop()


if __name__ == "__main__":
    main()
