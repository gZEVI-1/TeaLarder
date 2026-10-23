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

**Tea Larder** — небольшой пет-проект для учёта чайной коллекции. Django-сервер хранит сорта чая и отдаёт их через REST API, Django Admin используется для редактирования, а десктопные клиенты — для просмотра каталога.

В каталоге есть название, тип, регион и год сбора, место покупки, цена, оценка, описание, основное фото и галерея изображений.

## Возможности

- CRUD через REST API и Django Admin.
- 16 типов чая и 27 регионов произрастания.
- Основное фото и галерея `TeaImage`.
- Пагинация списков, поиск и фильтры в админке.
- Выборки `available`, `unavailable`, `new`, `available_old`.
- Массовые действия в Django Admin.
- Импорт каталога из Excel в JSON/fixture.
- Десктопная таблица `desktop_table.py`.
- Карточный клиент `desktop_admin.py`.

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
| `GET` | `/api/teas/available/` | В наличии |
| `GET` | `/api/teas/unavailable/` | Не в наличии |
| `GET` | `/api/teas/new/` | Новые сорта |
| `GET` | `/api/teas/available_old/` | Давно в коллекции |

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
├── desktop_table.py      # таблица каталога
├── manage.py
└── pyproject.toml
```

## Лицензия

MIT — подробности в [docs/LICENSE](docs/LICENSE).
