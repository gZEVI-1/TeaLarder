<p align="center">
  <img src="logos-design/logo-dark.png" alt="Tea Larder" width="180">
</p>

<h1 align="center">Tea Larder</h1>

<p align="center">
  Личный каталог чая с REST API и десктопными клиентами
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://www.djangoproject.com/"><img src="https://img.shields.io/badge/Django-6.1-092E20?style=flat-square&logo=django&logoColor=white" alt="Django 6.1"></a>
  <a href="https://www.django-rest-framework.org/"><img src="https://img.shields.io/badge/DRF-3.18-A30000?style=flat-square" alt="Django REST Framework"></a>
  <a href="docs/LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f?style=flat-square" alt="MIT"></a>
</p>

---

## О проекте

**Tea Larder** — небольшой пет-проект для учёта чайной коллекции. Django-сервер хранит сорта чая и отдаёт их через REST API, Django Admin используется для редактирования, а десктопная таблица повторяет админку: фильтры, сортировку, массовые действия, форму с фото и inline-галереей; карточный клиент — для просмотра каталога.

В каталоге есть название, тип, регион и год сбора, место покупки, цена, оценка, описание, основное фото и галерея изображений.

## Возможности

- CRUD через REST API и Django Admin.
- 16 типов чая и 27 регионов произрастания.
- Основное фото и галерея `TeaImage`.
- Пагинация списков, поиск и фильтры в админке.
- Выборки `available`, `unavailable`, `new`, `available_old`.
- Массовые действия в Django Admin и десктопной таблице.
- Импорт каталога из Excel в JSON/fixture.
- Десктопная таблица `desktop_table.py` повторяет админку: все колонки `list_display`, сортировка по заголовкам, фильтры `list_filter` с количеством записей, поиск по `search_fields`, 12 массовых действий, создание/редактирование/удаление, быстрое переключение наличия, замена и очистка фото, inline-галерея. Массовое удаление подтверждается контрольным словом; файлы фото и галереи удаляются вместе с записью.
- Карточный клиент `desktop_admin.py`: витрина для просмотра каталога.

## Варианты поставки (этап 7, запланировано)

Сейчас проект запускается из исходников. Целевая схема — две поставки из одной кодовой базы:

1. **Local Single** — одно приложение, полностью локально (БД, серверный слой и GUI в одном пакете). Пользователь видит только карточки; в верхнем углу — «Администратор», после пароля включаются карточки и таблица, CRUD, фото и галерея. Пароль хранится только как хеш, права проверяются на уровне API.
2. **Серверный релиз** — разделение на сервер (Django + БД + медиа + API), опциональную серверную админку (Django Admin), локальную админку и локальный клиент. Все части — с аккаунтами и ролями (`viewer` / `admin` / `superuser`). Сервер ставится через Docker.

Различия версий выносятся в профили сборки (`local_single`, `server`, `server_admin`, `server_client`), а не в копии проекта. Подробности, матрица компонентов, роли, Docker и критерии приёмки — в [docs/new/Releases.md](docs/new/Releases.md).

## Стек

`Python` · `Django` · `Django REST Framework` · `SQLite` · `Pillow` · `CustomTkinter` · `requests` · `python-dotenv`

## Запуск

```bash
git clone https://github.com/gZEVI-1/TeaLarder.git
cd TeaLarder
python -m venv .venv
```

```powershell
# Windows
.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

```bash
pip install -e .
```

Создайте `.env` в корне проекта:

```dotenv
SECRET_KEY=django-insecure-change-me
DEBUG=True

TEA_API_USER=admin
TEA_API_PASSWORD=your-password
```

Подготовьте базу и загрузите примеры:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py loaddata Catalog/fixtures/teas.json
```

Запустите сервер:

```bash
python manage.py runserver
```

Тесты API:

```bash
python manage.py test Catalog
```

- Приложение: <http://127.0.0.1:8000/api/>
- Админка: <http://127.0.0.1:8000/admin/>

Десктопные клиенты:

```bash
python desktop_table.py
python desktop_admin.py
```

## API

| Метод | URL | Описание |
|---|---|---|
| `GET` | `/api/teas/` | Список сортов |
| `POST` | `/api/teas/` | Создать сорт |
| `GET` | `/api/teas/{id}/` | Сорт по ID |
| `PUT` / `PATCH` | `/api/teas/{id}/` | Изменить сорт |
| `DELETE` | `/api/teas/{id}/` | Удалить сорт |
| `GET` / `POST` | `/api/tea-images/` | Галерея: список (`?tea={id}`) и создание |
| `GET` / `PATCH` / `PUT` / `DELETE` | `/api/tea-images/{id}/` | Изображение галереи |
| `GET` | `/api/teas/available/` | В наличии |
| `GET` | `/api/teas/unavailable/` | Не в наличии |
| `GET` | `/api/teas/new/` | Новые сорта |
| `GET` | `/api/teas/available_old/` | Давно в коллекции |

В ответе сорта есть вложенный список `images` (галерея `TeaImage`).
`created_at` доступен для `PATCH` — это используют массовые действия
десктопной админки; `photo` можно заменить через `multipart/form-data`
или очистить, передав `{"photo": null}`.

Чтение доступно без авторизации, запись — через HTTP Basic:

```bash
curl -u admin:your-password \
  -X POST http://127.0.0.1:8000/api/teas/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Да Хун Пао",
    "place_of_purchase": "Чайный дом",
    "tea_type": "oolongNF",
    "price": "12.50",
    "mark": "9.0",
    "year_of_harvest": 2025,
    "place_of_harvest": "Fujian"
  }'
```

Пагинация: `?page=2&page_size=50` (по умолчанию 20, максимум 500).

## Структура

```text
TeaLarder/
├── Catalog/              # модели, API, админка, миграции, fixture
├── Parser/               # импорт Excel → JSON
├── docs/                 # команды Django и лицензия
├── logos-design/         # логотипы
├── media/                # изображения (не в Git)
├── tea_larder/           # настройки и маршруты Django
├── desktop_admin.py      # карточный клиент
├── desktop_table.py      # таблица каталога: паритет с Django Admin
├── manage.py
└── pyproject.toml
```

## Лицензия

MIT — подробности в [docs/LICENSE](docs/LICENSE).
