"""
Запуск: .venv\\Scripts\\python.exe desktop_admin.py
"""
import io
import logging
import math
import os
import queue
import threading
import time
import tkinter as tk
from collections import OrderedDict
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
FETCH_PAGE_SIZE = 500
ENV_PATH = Path(__file__).resolve().parent / ".env"

FONT = "Segoe UI"
COLOR_BG = "#F6F0E3"
COLOR_SURFACE = "#FDFAF2"
COLOR_TEXT = "#2A3122"
COLOR_MUTED = "#847D6B"
COLOR_BORDER = "#E7DDC9"
COLOR_ACCENT = "#5F7534"
COLOR_ACCENT_HOVER = "#4C5F29"
COLOR_SOFT = "#EFF0DF"
COLOR_DANGER = "#A23B2A"
COLOR_WARN = "#B9822E"
COLOR_MARK = "#A7771F"
COLOR_CHIP = "#EDE6D6"
COLOR_CHIP_HOVER = "#E1D7C2"
COLOR_UNAVAILABLE = "#948D7B"
COLOR_ERROR_SOFT = "#F8E7E2"
COLOR_SCROLL = "#D9CDB7"
COLOR_SCROLL_HOVER = "#C7B99F"

LOGO_PATH = (
    Path(__file__).resolve().parent
    / "logos-design"
    / "sologo-aitools-bgremover-trimmed.png"
)
LOGO_SIZE = 42

CARD_MIN_WIDTH = 210
CARD_GAP = 14
PAGE_SIZE = 12

# Ширина карточки квантуется шагом: при ресайзе окна кэш картинок
# (ключ = точные пиксели) переиспользуется вместо пересчёта каждого фото.
CARD_SIZE_STEP = 16
CARD_PHOTO_RADIUS = 12
DETAIL_IMAGE_WIDTH = 520
DETAIL_IMAGE_HEIGHT = 220
DETAIL_PHOTO_RADIUS = 16

RESIZE_DEBOUNCE_MS = 150
SEARCH_DEBOUNCE_MS = 250
WHEEL_DEBOUNCE_MS = 120
LOAD_RENDER_DEBOUNCE_MS = 150
RENDER_BUDGET_MS = 50
UI_DRAIN_MS = 16

RAW_PHOTO_SIZE = (640, 640)
RAW_PHOTO_CACHE_LIMIT = 48
PREPARED_CACHE_LIMIT = 160
IMAGE_CACHE_LIMIT = 120

LOGGER = logging.getLogger("desktop_admin")
_MISSING = object()

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


class LoadError(RuntimeError):
    """Ошибка загрузки с сообщением, готовым для статус-строки."""


def _http_error_message(status: int) -> str:
    if status == 401:
        return "Ошибка авторизации (401)"
    if status == 403:
        return "Доступ запрещён (403)"
    if status == 404:
        return "Не найден (404)"
    return f"HTTP {status}"


def fetch_teas_stream(
    session: requests.Session,
    page_size: int = FETCH_PAGE_SIZE,
    timeout: float = REQUEST_TIMEOUT,
):
    """Отдаёт пары (items, total) по мере поступления страниц /api/teas/."""
    url = f"{API_TEAS_URL}?page_size={page_size}"
    loaded = 0

    while url:
        try:
            response = session.get(url, timeout=timeout)
        except requests.exceptions.Timeout as exc:
            raise LoadError(f"Сервер не ответил за {timeout} с") from exc
        except requests.exceptions.ConnectionError as exc:
            raise LoadError(f"Сервер недоступен: {BASE_URL}") from exc
        except requests.exceptions.RequestException as exc:
            raise LoadError(f"Ошибка запроса: {exc}") from exc

        if response.status_code != 200:
            raise LoadError(_http_error_message(response.status_code))

        try:
            data = response.json()
        except ValueError as exc:
            raise LoadError("Ошибка JSON") from exc

        if isinstance(data, list):
            yield data, len(data)
            return
        if not isinstance(data, dict):
            raise LoadError("Неожиданный формат ответа")

        results = data.get("results")
        if not isinstance(results, list):
            results = []
        total = data.get("count")
        if not isinstance(total, int):
            total = loaded + len(results)
        loaded += len(results)
        yield results, total

        next_url = data.get("next")
        url = urljoin(BASE_URL + "/", next_url) if next_url else None


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


@lru_cache(maxsize=4)
def logo_image(size: int) -> Image.Image | None:
    """Левая (светлая) версия лого из logos-design, обрезанная до значка."""
    try:
        with Image.open(LOGO_PATH) as source:
            image = source.convert("RGBA")
    except (OSError, ValueError):
        LOGGER.warning("Лого не найдено: %s", LOGO_PATH)
        return None

    tile = image.crop((0, 0, image.width // 2, image.height))
    bbox = tile.getchannel("A").getbbox()
    if bbox:
        tile = tile.crop(bbox)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


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
    image = Image.new("RGB", (canvas_width, canvas_height), "#EAE4D6")
    draw = ImageDraw.Draw(image)

    top = (246, 242, 232)
    bottom = (219, 211, 194)
    # Градиент строим в 1 px шириной и растягиваем: в разы дешевле, чем
    # canvas_height отдельных draw.line в главном потоке.
    gradient = Image.new("RGB", (1, canvas_height))
    gradient_pixels = gradient.load()
    for y in range(canvas_height):
        ratio = y / max(1, canvas_height - 1)
        gradient_pixels[0, y] = tuple(
            int(start + (end - start) * ratio)
            for start, end in zip(top, bottom)
        )
    image.paste(
        gradient.resize((canvas_width, canvas_height), Image.Resampling.BILINEAR),
        (0, 0),
    )

    center_x = canvas_width / 2
    center_y = canvas_height / 2
    glow = min(canvas_width, canvas_height) * 0.42
    draw.ellipse(
        (center_x - glow, center_y - glow, center_x + glow, center_y + glow),
        fill=(239, 235, 224),
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
    draw.polygon(upper + lower[::-1], fill=(166, 175, 110))

    vein_width = max(2, scale * 2)
    draw.line(
        (rotate(-leaf_length * 0.46, 0), rotate(leaf_length * 0.46, 0)),
        fill=(137, 147, 82),
        width=vein_width,
    )
    draw.line(
        (rotate(-leaf_length * 0.44, 0), rotate(-leaf_length * 0.62, 0)),
        fill=(137, 147, 82),
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

        # Модель коллекции: список для счётчиков + индексы для поиска и порядка.
        self._teas: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._order: list[str] = []
        self._sort_key: dict[str, tuple] = {}
        self._haystack: dict[str, str] = {}
        self._is_new: dict[str, bool] = {}
        self._fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
        self._active_query = ""

        self._card_widgets: dict[str, ctk.CTkFrame] = {}
        self._card_parts: dict[str, dict] = {}
        self._last_visible: set[str] = set()
        self._state_frame: ctk.CTkFrame | None = None
        self._filter_key = "all"
        self._busy = False
        self._load_token = 0
        self._last_error: str | None = None
        self._columns = 0
        self._card_width = 0
        self._relayout_job = None
        self._search_job = None
        self._render_job = None
        self._render_token = 0
        self._rendered_once = False
        self._placed_count = 0
        self._placing = False
        self._wheel_job = None
        self._loading_more = False
        self._shown = PAGE_SIZE
        self._total_filtered = 0
        self._footer: ctk.CTkFrame | None = None
        self._footer_button: ctk.CTkButton | None = None
        self._details_window: ctk.CTkToplevel | None = None
        self._detail_widgets: dict = {}
        self._search_var = tk.StringVar()

        self._photos: OrderedDict[str, Image.Image | None] = OrderedDict()
        self._raw_locks: dict[str, threading.Lock] = {}
        self._photo_pending: dict[tuple[str, int, int], list] = {}
        self._prepared: OrderedDict[tuple[str, int, int], Image.Image | None] = OrderedDict()
        self._image_cache: OrderedDict[tuple, ctk.CTkImage] = OrderedDict()
        self._photo_queue: queue.Queue[tuple | None] = queue.Queue()
        self._photo_lock = threading.Lock()
        self._photo_epoch = 0
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._drain_job = None
        self._closing = False

        for index in range(4):
            threading.Thread(
                target=self._photo_worker,
                daemon=True,
                name=f"tea-photo-{index}",
            ).start()

        self._build_ui()
        self.after(UI_DRAIN_MS, self._drain_ui_queue)
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

        logo_picture = logo_image(LOGO_SIZE)
        if logo_picture is not None:
            self._logo_asset = logo_picture
            self._logo_ctk = ctk.CTkImage(
                light_image=logo_picture,
                size=(LOGO_SIZE, LOGO_SIZE),
            )
            logo = ctk.CTkLabel(
                brand,
                text="",
                image=self._logo_ctk,
                fg_color="transparent",
            )
        else:
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
            textvariable=self._search_var,
        )
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=14)
        # trace ловит и набор, и вставку мышью — без вечного опроса поля.
        self._search_var.trace_add("write", self._on_search_changed)

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
            scrollbar_button_color=COLOR_SCROLL,
            scrollbar_button_hover_color=COLOR_SCROLL_HOVER,
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
        self._set_status("Загрузка коллекции…", COLOR_WARN)
        threading.Thread(
            target=self._load_worker,
            args=(token,),
            daemon=True,
            name="tea-loader",
        ).start()

    def _load_worker(self, token: int) -> None:
        try:
            first = True
            for batch, total in fetch_teas_stream(self.session):
                self._post(
                    lambda items=batch, count=total, is_first=first: self._apply_batch(
                        token, items, count, is_first
                    )
                )
                first = False
            self._post(lambda: self._finish_load(token))
        except LoadError as exc:
            self._post(lambda message=str(exc): self._fail_load(token, message))
        except Exception as exc:  # воркер не должен умирать молча
            self._post(lambda message=f"Неожиданная ошибка: {exc}": self._fail_load(token, message))
        finally:
            self._post(lambda: self._set_busy(False))

    def _apply_batch(
        self,
        token: int,
        batch: list[dict],
        total: int,
        first: bool,
    ) -> None:
        if token != self._load_token or self._closing:
            return

        if first:
            # Атомарная замена: старые данные живут до первой страницы новой
            # загрузки, поэтому неудачный F5 не оставляет пустой экран.
            self._teas.clear()
            self._by_id.clear()
            self._order.clear()
            self._haystack.clear()
            self._sort_key.clear()
            self._is_new.clear()
            self._fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
            self._rendered_once = False
            self._last_error = None
            self._discard_cards()

        added = 0
        for tea in batch:
            key = self._tea_key(tea)
            if key in self._by_id:  # защита от повторов между страницами
                continue
            self._index_tea(tea)
            added += 1

        if added:
            self._rebuild_order()

        if first:
            self._schedule_render(0)
        elif added:
            self._schedule_render(LOAD_RENDER_DEBOUNCE_MS)

        self._set_status(
            f"Загрузка: {len(self._teas)} из {max(total, len(self._teas))}…",
            COLOR_WARN,
        )

    def _finish_load(self, token: int) -> None:
        if token != self._load_token or self._closing:
            return
        self._set_busy(False)
        self._render_cards(reset=not self._rendered_once)
        if not self._teas:
            return
        self._set_status(
            f"Сервер доступен · {len(self._teas)} {plural_tea(len(self._teas))}",
            COLOR_ACCENT,
        )

    def _fail_load(self, token: int, message: str) -> None:
        if token != self._load_token or self._closing:
            return
        self._set_busy(False)
        self._last_error = message
        self._set_status(f"Ошибка загрузки: {message}", COLOR_DANGER)
        if not self._teas:
            self._show_state(
                "Не удалось загрузить коллекцию",
                f"{message}. Проверьте, что сервер запущен, и повторите попытку.",
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
        soft = COLOR_ERROR_SOFT if error else COLOR_SOFT
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
        self._photo_epoch += 1
        self._clear_state()
        for card in self._card_widgets.values():
            try:
                card.destroy()
            except tk.TclError:
                pass
        self._card_widgets.clear()
        self._card_parts.clear()
        # Колбэки указывают на уничтоженные лейблы — держать их незачем.
        self._photo_pending.clear()
        self._card_width = 0
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
        raw_width = max(CARD_MIN_WIDTH, (width - CARD_GAP * (columns - 1)) // columns)
        # Квантование шагом: кэш картинок ключуется точными пикселями, без шага
        # каждый пиксель ресайза обесценивал бы все подготовленные фото.
        card_width = max(CARD_MIN_WIDTH, (raw_width // CARD_SIZE_STEP) * CARD_SIZE_STEP)
        return columns, card_width

    def _restyle_cards(self, card_width: int) -> None:
        """Подгоняет готовые карточки под новую ширину, не пересоздавая виджеты."""
        if not self._card_parts:
            return
        image_width = max(140, card_width - 16)
        image_height = max(110, min(180, int(image_width * 0.62)))
        wrap = max(150, card_width - 30)
        name_limit = max(28, (wrap // 8) * 2 + 4)

        for parts in self._card_parts.values():
            tea = parts["tea"]
            try:
                parts["photo_frame"].configure(width=image_width, height=image_height)
                parts["name_label"].configure(
                    text=shorten(tea.get("name"), name_limit),
                    wraplength=wrap,
                )
                parts["purchase_label"].configure(
                    text="Магазин: " + shorten(tea.get("place_of_purchase"), 30),
                    wraplength=wrap,
                )
            except tk.TclError:
                continue

            url = media_url(tea.get("photo"))
            if not url:
                continue
            size_key = (url, image_width, image_height)
            self._request_photo(
                url,
                image_width,
                image_height,
                CARD_PHOTO_RADIUS,
                lambda picture, label=parts["photo_label"], key=size_key, w=image_width, h=image_height: (
                    self._apply_card_photo(key, picture, label, w, h)
                ),
            )

    def _on_window_configure(self, event) -> None:
        if event.widget is not self:
            return
        if self._relayout_job is not None:
            self.after_cancel(self._relayout_job)
        self._relayout_job = self.after(RESIZE_DEBOUNCE_MS, self._relayout)

    def _relayout(self) -> None:
        self._relayout_job = None
        if not self._teas or self._closing:
            return
        columns, card_width = self._grid_metrics()
        if columns == self._columns and abs(card_width - self._card_width) < CARD_SIZE_STEP:
            return
        self._render_cards(reset=False)

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
        self._render_cards(reset=True)

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
        self._render_cards(reset=True)

    def _index_tea(self, tea: dict) -> None:
        """Индексирует чай один раз: поиск и порядок перестают быть работой на клик."""
        key = self._tea_key(tea)
        self._by_id[key] = tea
        self._haystack[key] = self._make_haystack(tea)
        self._sort_key[key] = (
            not bool(tea.get("availability")),
            (tea.get("name") or "").casefold(),
        )
        created = parse_datetime(tea.get("created_at"))
        self._is_new[key] = created is not None and created >= self._fresh_after
        self._teas.append(tea)

    @staticmethod
    def _make_haystack(tea: dict) -> str:
        parts = (
            tea.get("name"),
            tea.get("description"),
            tea.get("place_of_purchase"),
            TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type")),
            REGION_LABELS.get(tea.get("place_of_harvest"), tea.get("place_of_harvest")),
            tea.get("year_of_harvest"),
            tea.get("price"),
        )
        return " ".join(str(part) for part in parts if part).casefold()

    def _rebuild_order(self) -> None:
        self._order = sorted(self._by_id, key=self._sort_key.__getitem__)

    def _filtered_keys(self) -> list[str]:
        """Отбор по готовому порядку — без сортировки и без пересборки haystack."""
        query = self._active_query
        mode = self._filter_key
        result: list[str] = []
        for key in self._order:
            tea = self._by_id.get(key)
            if tea is None:
                continue
            if mode == "available" and not tea.get("availability"):
                continue
            if mode == "unavailable" and tea.get("availability"):
                continue
            if mode == "new" and not self._is_new.get(key):
                continue
            if query and query not in self._haystack.get(key, ""):
                continue
            result.append(key)
        return result

    @staticmethod
    def _tea_key(tea: dict) -> str:
        value = tea.get("id")
        if value is not None:
            return str(value)
        return str(id(tea))

    def _schedule_render(self, delay: int = LOAD_RENDER_DEBOUNCE_MS) -> None:
        if self._closing:
            return
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_job = self.after(delay, self._apply_scheduled_render)

    def _apply_scheduled_render(self) -> None:
        self._render_job = None
        if self._closing:
            return
        self._render_cards(reset=not self._rendered_once)

    def _render_cards(self, reset: bool = False) -> None:
        if self._closing:
            return

        previous_columns = self._columns
        previous_width = self._card_width

        columns, card_width = self._grid_metrics()
        if columns != self._columns:
            self._apply_grid(columns)
        if abs(card_width - self._card_width) >= CARD_SIZE_STEP:
            self._restyle_cards(card_width)
        self._columns = columns
        self._card_width = card_width

        keys = self._filtered_keys()
        self._total_filtered = len(keys)
        if reset:
            self._shown = PAGE_SIZE
        self._shown = min(len(keys), max(PAGE_SIZE, self._shown))

        if not keys:
            self._render_empty(reset)
            return

        self._clear_state()
        self._render_token += 1
        self._rendered_once = True

        visible = keys[: self._shown]
        # «Показать ещё» дорисовывает только новые карточки: остальные уже
        # стоят на местах, и повторный grid на каждую порцию — лишняя работа.
        start = 0
        if not reset and previous_columns == columns and previous_width == card_width:
            start = min(self._placed_count, len(visible))
        self._place_visible(
            visible,
            self._render_token,
            start,
            columns,
            card_width,
            reset,
        )

    def _render_empty(self, reset: bool) -> None:
        self._rendered_once = True
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
        if self._footer is not None:
            try:
                self._footer.grid_remove()
            except tk.TclError:
                pass
        if reset:
            self._reset_scroll()

    def _place_visible(
        self,
        keys: list[str],
        token: int,
        index: int,
        columns: int,
        card_width: int,
        reset: bool,
    ) -> None:
        """Раскладывает карточки кусками по ~12 мс, чтобы окно не подвисало."""
        if token != self._render_token or self._closing:
            self._placing = False
            return

        self._placing = True
        deadline = time.perf_counter() + RENDER_BUDGET_MS / 1000
        total = len(keys)

        while index < total and time.perf_counter() < deadline:
            key = keys[index]
            card = self._card_widgets.get(key)
            if card is None:
                tea = self._by_id.get(key)
                if tea is None:
                    index += 1
                    continue
                card = self._build_card(tea, card_width)
                self._card_widgets[key] = card
            card.grid(
                row=index // columns,
                column=index % columns,
                sticky="nsew",
                padx=CARD_GAP // 2,
                pady=CARD_GAP // 2,
            )
            index += 1
            self._placed_count = index

        if index < total:
            # after_idle, а не after(1): таймер Windows имеет гранулярность
            # ~15 мс, и на каждую карточку получалась лишняя пауза с полным
            # пересчётом геометрии уже разложенных карточек.
            self.after_idle(
                lambda: self._place_visible(keys, token, index, columns, card_width, reset)
            )
            return

        self._placing = False
        self._finish_render_pass(keys, columns, reset)

    def _finish_render_pass(self, keys: list[str], columns: int, reset: bool) -> None:
        visible = set(keys)
        for key, card in self._card_widgets.items():
            if key in visible:
                continue
            try:
                card.grid_remove()
            except tk.TclError:
                pass
        self._prune_cards(visible)
        self._place_footer(max(0, self._total_filtered - len(keys)), columns)
        if reset:
            self._reset_scroll()

    def _prune_cards(self, visible: set[str]) -> None:
        """Держит в памяти карточки только текущего и предыдущего экрана."""
        keep = visible | self._last_visible
        for key in [item for item in self._card_widgets if item not in keep]:
            card = self._card_widgets.pop(key, None)
            self._card_parts.pop(key, None)
            if card is None:
                continue
            try:
                card.destroy()
            except tk.TclError:
                pass
        self._last_visible = set(visible)

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
        if self._closing or self._loading_more:
            return
        if self._shown >= self._total_filtered:
            return
        self._loading_more = True
        try:
            self._shown = min(self._total_filtered, self._shown + PAGE_SIZE)
            self._render_cards(reset=False)
        finally:
            self._loading_more = False

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
        # Один жест = одна догрузка: без after_cancel очередь задач колеса
        # превращалась в каскад полных перерисовок.
        if self._wheel_job is not None:
            self.after_cancel(self._wheel_job)
        self._wheel_job = self.after(WHEEL_DEBOUNCE_MS, self._maybe_load_more)

    def _maybe_load_more(self) -> None:
        self._wheel_job = None
        if self._closing or self._placing or self._loading_more:
            return
        if self._shown >= self._total_filtered:
            return
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
            fg_color=COLOR_ACCENT if availability else COLOR_UNAVAILABLE,
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

        self._card_parts[self._tea_key(tea)] = {
            "tea": tea,
            "photo_frame": photo_frame,
            "photo_label": photo_label,
            "name_label": name_label,
            "purchase_label": purchase_label,
        }

        url = media_url(tea.get("photo"))
        if url:
            key = (url, image_width, image_height)
            self._request_photo(
                url,
                image_width,
                image_height,
                CARD_PHOTO_RADIUS,
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
            self._image_cache.move_to_end(key)
            return cached
        base = placeholder_photo(width, height).convert("RGBA")
        if radius > 0:
            base.putalpha(rounded_mask(width, height, radius))
        return self._image_cache_put(key, base, width, height)

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
            self._image_cache.move_to_end(key)
            return cached
        return self._image_cache_put(key, image, width, height)

    def _image_cache_put(
        self,
        key: tuple,
        image: Image.Image,
        width: int,
        height: int,
    ) -> ctk.CTkImage:
        scaled = ctk.CTkImage(light_image=image, size=(width, height))
        self._image_cache[key] = scaled
        self._image_cache.move_to_end(key)
        while len(self._image_cache) > IMAGE_CACHE_LIMIT:
            self._image_cache.popitem(last=False)
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
        pending = self._photo_pending.get(key)
        if pending is not None:
            pending.append(callback)
            return

        self._photo_pending[key] = [callback]
        self._photo_queue.put((self._photo_epoch, key, url, width, height, radius))

    def _photo_session(self) -> requests.Session:
        """Своя сессия на поток: requests.Session не потокобезопасен."""
        session = build_session()
        session.auth = self.session.auth
        session.headers.update(self.session.headers)
        return session

    def _photo_worker(self) -> None:
        session = self._photo_session()
        while True:
            task = self._photo_queue.get()
            if task is None:
                return
            epoch, key, url, width, height, radius = task
            if epoch != self._photo_epoch:
                # Пока задача стояла в очереди, карточки пересобрали — работа
                # всё равно ушла бы в уничтоженные лейблы.
                continue
            image = self._prepare_photo(session, url, width, height, radius)
            if epoch != self._photo_epoch:
                continue
            self._post(lambda size_key=key, picture=image: self._finish_photo(size_key, picture))

    def _finish_photo(
        self,
        key: tuple[str, int, int],
        image: Image.Image | None,
    ) -> None:
        self._prepared[key] = image
        self._prepared.move_to_end(key)
        while len(self._prepared) > PREPARED_CACHE_LIMIT:
            self._prepared.popitem(last=False)
        for callback in self._photo_pending.pop(key, []):
            try:
                callback(image)
            except tk.TclError:
                pass
            except Exception:
                LOGGER.exception("Не удалось применить фото")

    def _prepare_photo(
        self,
        session: requests.Session,
        url: str,
        width: int,
        height: int,
        radius: int,
    ) -> Image.Image | None:
        image = self._get_raw_photo(session, url)
        if image is None:
            return None
        try:
            return cover_image(image, width, height, radius)
        except Exception:
            return None

    def _get_raw_photo(self, session: requests.Session, url: str) -> Image.Image | None:
        with self._photo_lock:
            cached = self._photos.get(url, _MISSING)
            if cached is not _MISSING:
                self._photos.move_to_end(url)
                return cached
            lock = self._raw_locks.setdefault(url, threading.Lock())

        # Один URL качается один раз, даже если его просят карточка и окно деталей.
        with lock:
            with self._photo_lock:
                cached = self._photos.get(url, _MISSING)
                if cached is not _MISSING:
                    return cached
            image = self._fetch_image(session, url)
            with self._photo_lock:
                self._photos[url] = image
                self._photos.move_to_end(url)
                while len(self._photos) > RAW_PHOTO_CACHE_LIMIT:
                    self._photos.popitem(last=False)
                self._raw_locks.pop(url, None)
            return image

    def _fetch_image(self, session: requests.Session, url: str) -> Image.Image | None:
        try:
            response = session.get(url, timeout=8)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content))
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(RAW_PHOTO_SIZE, Image.Resampling.LANCZOS)
            return image
        except Exception:
            return None

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
            self._drain_job = self.after(UI_DRAIN_MS, self._drain_ui_queue)
        except tk.TclError:
            self._drain_job = None

    def _open_details(self, tea: dict) -> None:
        if self._closing:
            return
        window = self._details_window
        if window is not None:
            try:
                if not window.winfo_exists():
                    window = None
            except tk.TclError:
                window = None
        if window is None:
            window = self._build_details_window()
        self._fill_details(tea)
        try:
            window.deiconify()
            window.lift()
            window.focus_force()
        except tk.TclError:
            pass

    def _build_details_window(self) -> ctk.CTkToplevel:
        """Строит окно карточки один раз; дальше только перезаполняем поля."""
        window = ctk.CTkToplevel(self)
        self._details_window = window
        window.title("Карточка чая")
        window.geometry("600x720")
        window.minsize(560, 560)
        window.configure(fg_color=COLOR_BG)
        window.transient(self)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(window, fg_color=COLOR_SURFACE, corner_radius=0, height=56)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_propagate(False)
        top.grid_columnconfigure(1, weight=1)

        detail_logo_picture = logo_image(28)
        if detail_logo_picture is not None:
            self._detail_logo_ctk = ctk.CTkImage(
                light_image=detail_logo_picture,
                size=(28, 28),
            )
            ctk.CTkLabel(
                top,
                text="",
                image=self._detail_logo_ctk,
                fg_color="transparent",
            ).grid(row=0, column=0, padx=(20, 10), pady=14, sticky="w")

        ctk.CTkLabel(
            top,
            text="Карточка чая",
            font=(FONT, 15, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=0, column=1, padx=(0, 12), pady=14, sticky="w")

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
        close_button.grid(row=0, column=2, padx=20, pady=12, sticky="e")

        photo_frame = ctk.CTkFrame(
            window,
            fg_color=COLOR_SOFT,
            corner_radius=16,
            width=DETAIL_IMAGE_WIDTH,
            height=DETAIL_IMAGE_HEIGHT,
        )
        photo_frame.grid(row=1, column=0, pady=(14, 12))
        photo_frame.grid_propagate(False)

        photo_label = ctk.CTkLabel(
            photo_frame,
            text="",
            fg_color="transparent",
            image=self._placeholder_image(
                DETAIL_IMAGE_WIDTH,
                DETAIL_IMAGE_HEIGHT,
                DETAIL_PHOTO_RADIUS,
            ),
        )
        photo_label.place(relx=0.5, rely=0.5, anchor="center")

        badge = ctk.CTkLabel(
            photo_frame,
            text="",
            width=100,
            height=24,
            corner_radius=9,
            fg_color=COLOR_ACCENT,
            text_color="#FFFFFF",
            font=(FONT, 10, "bold"),
        )
        badge.place(x=12, y=12)

        scroll = ctk.CTkScrollableFrame(
            window,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=COLOR_SCROLL,
            scrollbar_button_hover_color=COLOR_SCROLL_HOVER,
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

        name_label = ctk.CTkLabel(
            panel,
            text="",
            font=(FONT, 21, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=500,
        )
        name_label.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))

        subtitle_label = ctk.CTkLabel(
            panel,
            text="",
            font=(FONT, 12),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        subtitle_label.grid(row=1, column=0, sticky="w", padx=20)

        price_row = ctk.CTkFrame(panel, fg_color="transparent")
        price_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(12, 0))
        price_row.grid_columnconfigure(1, weight=1)

        price_label = ctk.CTkLabel(
            price_row,
            text="",
            font=(FONT, 24, "bold"),
            text_color=COLOR_ACCENT,
            anchor="w",
        )
        price_label.grid(row=0, column=0, sticky="w")

        mark_label = ctk.CTkLabel(
            price_row,
            text="",
            font=(FONT, 12, "bold"),
            text_color=COLOR_MARK,
            anchor="e",
        )
        mark_label.grid(row=0, column=1, sticky="e")

        separator = ctk.CTkFrame(panel, height=1, fg_color=COLOR_BORDER)
        separator.grid(row=3, column=0, sticky="ew", padx=20, pady=14)

        info = ctk.CTkFrame(panel, fg_color="transparent")
        info.grid(row=4, column=0, sticky="ew", padx=20)
        info.grid_columnconfigure(1, weight=1)

        value_labels = []
        for row_index, label in enumerate(
            ("Тип", "Регион", "Год урожая", "Место покупки", "Добавлен", "Обновлён")
        ):
            ctk.CTkLabel(
                info,
                text=label,
                font=(FONT, 11),
                text_color=COLOR_MUTED,
                anchor="w",
            ).grid(row=row_index, column=0, sticky="nw", pady=4, padx=(0, 18))
            value_label = ctk.CTkLabel(
                info,
                text="",
                font=(FONT, 12),
                text_color=COLOR_TEXT,
                anchor="w",
                justify="left",
                wraplength=340,
            )
            value_label.grid(row=row_index, column=1, sticky="ew", pady=4)
            value_labels.append(value_label)

        ctk.CTkLabel(
            panel,
            text="Описание",
            font=(FONT, 12, "bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=5, column=0, sticky="w", padx=20, pady=(16, 4))

        description_label = ctk.CTkLabel(
            panel,
            text="",
            font=(FONT, 12),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=500,
        )
        description_label.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 20))

        self._detail_widgets = {
            "name": name_label,
            "subtitle": subtitle_label,
            "price": price_label,
            "mark": mark_label,
            "badge": badge,
            "photo": photo_label,
            "values": value_labels,
            "description": description_label,
        }

        window.bind("<Escape>", lambda _event: self._close_details(window))
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_details(window))
        window.withdraw()
        return window

    def _fill_details(self, tea: dict) -> None:
        widgets = self._detail_widgets
        if not widgets:
            return

        availability = bool(tea.get("availability"))
        name = tea.get("name") or "Без названия"
        type_name = TEA_TYPE_LABELS.get(tea.get("tea_type"), tea.get("tea_type") or "Другое")
        region = REGION_LABELS.get(tea.get("place_of_harvest"), tea.get("place_of_harvest"))
        description = tea.get("description")

        subtitle_parts = [type_name]
        if region:
            subtitle_parts.append(region)
        if tea.get("year_of_harvest"):
            subtitle_parts.append(str(tea["year_of_harvest"]))

        badge_text = "В наличии" if availability else "Нет в наличии"
        accent = COLOR_ACCENT if availability else COLOR_MUTED

        try:
            window = self._details_window
            if window is not None:
                window.title(name)
            widgets["name"].configure(
                text=name,
                text_color=COLOR_TEXT if availability else COLOR_MUTED,
            )
            widgets["subtitle"].configure(text=" · ".join(subtitle_parts))
            widgets["price"].configure(text=format_price(tea.get("price")), text_color=accent)
            widgets["mark"].configure(text=format_mark(tea.get("mark")))
            widgets["badge"].configure(
                text=badge_text,
                width=max(60, int(len(badge_text) * 7) + 16),
                fg_color=COLOR_ACCENT if availability else COLOR_UNAVAILABLE,
            )
            values = (
                type_name,
                region or "—",
                str(tea.get("year_of_harvest") or "—"),
                tea.get("place_of_purchase") or "—",
                format_date(tea.get("created_at")),
                format_date(tea.get("updated_at")),
            )
            for label, value in zip(widgets["values"], values):
                label.configure(text=str(value))
            widgets["description"].configure(
                text=description or "Описание не добавлено.",
                text_color=COLOR_TEXT if description else COLOR_MUTED,
            )
        except tk.TclError:
            return

        photo_label = widgets["photo"]
        url = media_url(tea.get("photo"))
        if not url:
            self._apply_detail_placeholder(photo_label)
            return
        size_key = (url, DETAIL_IMAGE_WIDTH, DETAIL_IMAGE_HEIGHT)
        self._request_photo(
            url,
            DETAIL_IMAGE_WIDTH,
            DETAIL_IMAGE_HEIGHT,
            DETAIL_PHOTO_RADIUS,
            lambda picture, label=photo_label, key=size_key: (
                self._apply_detail_photo(key, picture, label)
            ),
        )

    def _apply_detail_placeholder(self, label: ctk.CTkLabel) -> None:
        try:
            label.configure(
                image=self._placeholder_image(
                    DETAIL_IMAGE_WIDTH,
                    DETAIL_IMAGE_HEIGHT,
                    DETAIL_PHOTO_RADIUS,
                ),
                text="",
            )
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
        label.configure(
            image=self._ctk_image(key, image, DETAIL_IMAGE_WIDTH, DETAIL_IMAGE_HEIGHT),
            text="",
        )

    def _close_details(self, window: ctk.CTkToplevel) -> None:
        """Окно не уничтожаем: повторное открытие бесплатно, и глобальный
        перехват колеса от CTkScrollableFrame не плодится на каждый чай."""
        try:
            window.withdraw()
        except tk.TclError:
            pass

    def on_close(self) -> None:
        self._closing = True
        for job in (
            self._relayout_job,
            self._search_job,
            self._render_job,
            self._wheel_job,
            self._drain_job,
        ):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        for _ in range(4):
            self._photo_queue.put(None)
        try:
            self.destroy()
        except tk.TclError:
            pass


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
