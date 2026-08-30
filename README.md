# CloakDB — Анонимизатор SQL-дампов с ИИ-трансформерами

CloakDB — инструмент для маскировки персональных данных (PII) в MySQL/MariaDB дампах. Интегрируется как обёртка поверх существующих инструментов (Greenmask, PostgreSQL native) через пользовательские трансформеры на Python и декларативные YAML-конфиги.

---

## ⚡ Быстрый старт

### Интерактивный мастер
```bash
cd /path/to/my-sql-sanitizer

# Настройте API ключ LLM
mv .env.example .env
vim .env

# Запустите мастер (выбор БД → проверка конфига → запуск)
python3 -m cloaker
```

Мастер проходит через 4 этапа:
1. **Выбор базы** — список доступных `.sql` файлов в `examples/`
2. **Сканирование** — сбор ВСЕХ уникальных значений каждого поля (без ограничений!)
3. **Проверка конфигурации** — автоклассификация полей + маппинг к трансформерам
4. **Запуск обработки** — анонимизация с индикатором прогресса

### Пакетный режим (для CI/CD)
```bash
python3 -m cloaker --batch examples/chinook_test.sql -o output/sanitized.sql
```

### Docker
```bash
docker compose up --build              # Интерактивный мастер
docker compose run --rm cloakdb --batch /input/dump.sql  # Без мастера
```

---

## 🏗️ Архитектура: три фазы обработки

Весь процесс выполняется в одном процессе Python, без внешних скриптов и промежуточных файлов.

```
┌──────────────┐     ┌──────────────────┐     ┌────────────────────┐
│ ФАЗА 1       │     │ ФАЗА 2           │     │ ФАЗА 3             │
│ СБОР ДАННЫХ  │────▶│ ЗАГРУЗКА         │────▶│ СТРИМИНГ +         │
│ (Без лимитов)│     │ МАППИНГОВ        │────▶│ ЗАМЕНА             │
│              │     │                  │     │ O(1) lookup        │
└──────────────┘     └──────────────────┘     └────────────────────┘
```

### Фаза 1: Сбор ВСЕХ уникальных значений

Инструмент читает SQL-дамп один раз, извлекает каждое значение и группирует по `Таблица.Поле`. **Ограничений нет**: если в колонке 10 000 уникальных имён артистов, будут собраны все значения. RAM расходуется минимально — текстовые строки в SQL-дампах короткие, даже 10 тысяч значений занимают ~200 КБ.

### Фаза 2: Загрузка маппингов через LLM

Для каждого поля делается вызов к LLM API: собираем уникальные значения → отправляем запросом (с чанкингом при необходимости) → получаем JSON `{оригинал: замена}`. Результат сохраняется в глобальную карту `GlobalMappingRegistry`.

**Важно:** для полей «шафл» (`genre`, `crm_status`, `date_shuffle`) и для `skip` (PK/FK, метрики) LLM не вызывается вообще — обработка локальная.

### Фаза 3: Потоковая замена

Файл проходит повторно. Для каждой ячейки проверяется маппинг в словаре. Сложность O(1). Никаких сетевых вызовов.

### Кросс-табличная консистентность

`GlobalMappingRegistry` — единое хранилище замен для всего процесса. Одинаковые исходные значения всегда получают одинаковую замену:

| Таблица | Поле | Значение до | Значение после |
|---------|------|------------|----------------|
| Customer | FirstName | John | Elena |
| Invoice | ContactName | John | Elena |
| Employee | LastName | Johnson | Ivanova |

Это достигается тем, что каждый трансформер при `_load_mapping()` сохраняет пары в общий реестр, а при поиске замены идёт сначала туда.

---

## 🔧 Как связать поле с трансформером?

Маппинг работает в два уровня:

### Уровень 1: Явные правила (config.yaml)

В `config.yaml` в секции `transforms:` вы прописываете точно какие поля каким трансформером обрабатываются:

```yaml
transforms:
  Table.Column: type
  Customer.Email: email          # Почты → LLM-генерация
  Artist.Name: name              # Имена музыкантов → LLM реалистичные
  deals.Stage: crm_status        # Статусы сделок → циклическая замена
  Genre.Name: genre              # Жанры → циклическая замена
  companies.Inn: skip            # ИНН → не трогать
```

Формат: `ИмяТаблицы.ИмяКолонки` → идентификатор трансформера. Если правило не найдено — срабатывает автодетект.

### Уровень 2: Автодетект (`_auto_select_transformer`)

Если явного правила нет, алгоритм автоматически определяет тип по названию колонки и содержанию сэмплов:

| Что проверяет | Трансформер | Пример |
|---------------|-------------|--------|
| Содержит `@` | `email` | `Customer.Email` |
| Цифры ≥7 | `phone` | `Employee.Phone` |
| Название содержит `name`, `firstname` | `name` | `Artist.Name` |
| Название содержит `city`, `country` | `genre` (цикл) | `Customer.City` |
| Название содержит `status`, `stage` | `crm_status` (цикл) | `contacts.Status` |
| Название содержит `address` | `address` (LLM) | `Customer.Address` |
| Числовые поля (`price`, `total`, `quantity`) | `skip` | `Invoice.Total` |
| Колонки `_id` или `Id` | `skip` | `CustomerId` |
| Бизнес-коды (`inn`, `kpp`, `ogrn`) | `crm_status` | `companies.Inn` |

Автодетект хорош для быстрого старта. Для продакшена рекомендуется прописать явные правила в `config.yaml`.

---

## 🔄 Алгоритмы трансформеров (полный справочник)

Все 12 типов трансформеров в таблице:

| Тип файла | Идентификатор в конфиге | Логика | Требуется LLM? |
|-----------|------------------------|--------|----------------|
| `genre_transformer.py` | `genre` | Циклический swap значений | ❌ Нет |
| `crm_status_transformer.py` | `crm_status` | Циклический swap статусов | ❌ Нет |
| `date_transformer.py` | `date_shuffle_month`, `date_shuffle_year` | Математический шифр дат | ❌ Нет |
| `email_transformer.py` | `email` | LLM генерация почт | ✅ Да (локальный fallback) |
| `phone_transformer.py` | `phone` | LLM генерация номеров | ✅ Да (локальный fallback) |
| `name_transformer.py` | `name` | LLM реалистичные имена | ✅ Да |
| `company_transformer.py` | `company` | LLM названия компаний | ✅ Да |
| `address_transformer.py` | `address` | LLM адреса | ✅ Да |
| `composer_transformer.py` | `composer` | LLM композиторы | ✅ Да |
| `title_transformer.py` | `title` | LLM должности | ✅ Да |
| `postal_code_transformer.py` | `postal_code` | LLM почтовые коды | ✅ Да |
| *(нет)* | `skip` | Пропустить без изменений | ❌ Нет |

---

## 1️⃣ Локальные трансформеры (БЕЗ LLM, мгновенно, бесплатно)

Эти трансформеры работают полностью автономно — **ни одного вызова к API**. Они просто перемешивают или циклически меняют найденные значения. Распределение сохраняется полностью.

### `GenreTransformer` (genre) — циклический swap

Переставляет значения по кругу: 1→2, 2→3, последняя→первая. Гарантирует сохранение процентного распределения.

**Когда использовать:** любые списки с короткими строками где важна статистика но не конкретное значение.

| Поле | Таблица | Зачем |
|------|---------|-------|
| `City` | Customer, Employee | Город клиента — достаточно заменить |
| `Country` | Customer | Страна — безопасно менять |
| `State` | Customer | Штат/регион |
| `Name` | Genre, MediaType, Playlist | Жанры музыки — плейлисты |
| `PostalCode` | BillingAddress | Почтовый индекс — сохранить формат XXXXX |

### `CRMStatusTransformer` (crm_status) — циклический swap статусов

Те же принципы что и genre, но для бизнес-полей со специфичными значениями. Сохраняет формат (case, пробелы).

**Когда использовать:** любые enum/stage/terms columns.

| Поле | Таблица | Зачем |
|------|---------|-------|
| `Status` | Contacts | new/won/lost/on_hold → цикл |
| `Stage` | Deals | prospect/negotiation/closed |
| `Terms` | Payments | Net 30, Cash on Delivery |
| `Method` | Invoices | cash/credit/bank_transfer |
| `Inn/Ogrn/Kpp` | Companies | юр. реквизиты (обычно `skip`) |

### `DateShuffleTransformer` (date_shuffle_month / date_shuffle_year)

Математическое перемещение даты. Не ломает возрастную статистику:
- `month`: тот же год+месяц, другой день (1–28)
- `year`: тот же год, случайный месяц/день
- default: ±30 дней от оригинала

| Поле | Таблица | scope по умолчанию |
|------|---------|---------------------|
| `HireDate` | Employee | month (сохраняем период найма) |
| `BirthDate` | Employee | year (сохраняем возраст) |
| `InvoiceDate` | Invoice | days (±30 дней — не важно для аналитики) |

---

## 2️⃣ LLM-трансформеры (один вызов на колонку, потом O(1))

Каждый из этих трансформеров вызывает LLM API один раз для загрузки маппинга всех уникальных значений колонки. После этого замена происходит по словарю (O(1)). Если LLM недоступен — используется локальный fallback.

### `EmailTransformer` (email)

Генерирует реалистичные замены, сохраняя домен (`@example.com`). Домены берутся из пула оригинальных значений, чтобы сохранить долю каждого домена.

| Где применять | Примеры полей |
|---------------|---------------|
| Клиенты, сотрудники, контакты | `Customer.Email`, `Employee.Email`, `contacts.PersonalEmail` |

### `PhoneTransformer` (phone)

Сохраняет country code (+7, +1...), формат группировки скобок/дефисов. Меняет только номер. LLM даёт реалистичный вариант, fallback — hash-based generation.

| Где применять | Примеры полей |
|---------------|---------------|
| Контакты клиентов и сотрудников | `Customer.Phone`, `Employee.Phone`, `contacts.BusinessPhone` |
| Факсы | `Customer.Fax`, `Employee.Fax` |

### `NameTransformer` (name)

Реалистичные имена другого человека того же языка/скрипта. Требует LLM.

| Где применять | Примеры полей |
|---------------|---------------|
| Имена людей | `Customer.FirstName`, `Employee.FirstName` |
| Фамилии | `Customer.LastName`, `Employee.LastName` |
| Исполнители | `Artist.Name` |

### `CompanyTransformer` (company)

Названия организаций масштаба региона оригинала.

| Где применять | Примеры полей |
|---------------|---------------|
| Работодатели клиентов | `Customer.Company` |
| Юр. названия | `companies.CompanyName` |

### `AddressTransformer` (address)

Новые улицы/дома/город, сохраняющие структуру адреса.

| Где применять | Примеры полей |
|---------------|---------------|
| Адреса доставки | `Customer.Address`, `Employee.Address` |
| billing адрес | `Invoice.BillingAddress` |
| юр. адрес | `companies.LegalAddress` |

### `ComposerTransformer` (composer)

Композиторы (аналогично NameTransformer, но с музыкальным контекстом).

### `TitleTransformer` (title)

Должности/роли (Sales Representative, CFO, Director).

### `PostalCodeTransformer` (postal_code)

Почтовые коды с сохранением формата (XXX-XX или XXXXX).

---

## ⚙️ Конфигурация

### config.yaml — полная структура

```yaml
# === Маппинг полей к трансформерам ===
# Если поле здесь НЕ указано → сработает автодетект (_auto_select_transformer)
transforms:
  Artist.Name: name                   # Имена музыкантов → LLM
  Customer.FirstName: name            # Имена клиентов → LLM
  Customer.LastName: name             # Фамилии → LLM
  Customer.City: genre                # Города → циклический swap (без LLM!)
  Customer.Country: genre             # Страны → циклический swap (без LLM!)
  Customer.Email: email               # Почты → LLM
  Customer.Phone: phone               # Телефоны → LLM
  Customer.State: genre               # Штаты → циклический swap
  Genre.Name: genre                   # Жанры → циклический swap (без LLM!)
  MediaType.Name: genre               # Типы медиа → циклический swap
  Album.Title: title                  # Названия альбомов → LLM (или genre)
  Track.Name: name                    # Названия треков → LLM (или genre)
  Invoice.InvoiceDate: date_shuffle   # Даты счетов → shuffle дат
  Employee.BirthDate: date_shuffle    # Даты рождения → shuffle дат
  Employee.HireDate: date_shuffle     # Дата найма → shuffle дат
  contracts.Number: crm_status        # Номера контрактов → цикл
  contacts.Status: crm_status         # Статусы контактов → цикл
  deals.Stage: crm_status             # Этапы сделок → цикл

# === Параметры обработки ===
processing:
  batch_size: 20                      # Значений на один LLM-чанк
  chunk_max_chars: 8000               # Макс. размер промпта (байт)
  timeout_base: 30                    # Базовый таймаут LLM-запроса (сек)
  timeout_max: 55                     # Максимальный таймаут (сек)

# === Вывод ===
output:
  storage: file
  overwrite: true
  preserve_formatting: true
```

> **Важно:** `skip` означает «не трогать вообще». PK/FK (`*_id`, `Id`) пропускаются автоматически.

### Переменные окружения (.env)

| Переменная | Значение по умолчанию | Описание |
|------------|----------------------|----------|
| `LLM_API_KEY` | *(обязательно)* | API ключ вашего провайдера |
| `LLM_ENDPOINT` | `https://api.kodikrouter.ru/v1` | URL Chat Completions API |
| `LLM_MODEL` | `qwen/qwen3.7-flash` | Модель для генерации |
| `MAX_TOKENS` | `4096` | Лимит токенов ответа LLM |
| `BATCH_SIZE` | `20` | Значений на один LLM-чанк |

### Поток работы с примера

```
Вы запускаете: python3 -m cloaker --batch chinook_test.sql
  │
  ├─> Phase 1: собрано 64 поля, ~1800 уникальных значений
  │
  ├─> Автодетект определяет тип каждого поля:
  │   ├─ Customer.FirstName → name      → нужен LLM (~$0.001)
  │   ├─ Customer.City      → genre     → НУЛЛЕВАЯ стоимость (shuffle)
  │   ├─ Customer.Email     → email     → нужен LLM (~$0.002)
  │   ├─ Genre.Name         → genre     → НУЛЛЕВАЯ стоимость (shuffle)
  │   └─ Customer.CustomerId → skip     → вообще не трогаем
  │
  ├─> Phase 2: делаем N вызовов LLM, где N = поля с LLM-трансформерами
  │   (обычно 15-40 из 64 колонок; остальные шаффлятся бесплатно)
  │
  └─> Phase 3: O(1) замена всех строк по готовым словарям
```

---

## 📊 Примеры баз

### Chinook (музыкальный магазин)
Стандартный тестовый дамп. Проверка основных типов полей: имена, жанры, города, телефоны, почты, даты.

```
examples/chinook_test.sql   — 11 таблиц, ~15 000 строк, 64 поля
```

### Business CRM (российская система управления клиентами)
Бизнес-ориентированная база с русскоязычными данными, ИНН/KPP/OGRN, российскими телефонами и почтами.

```
examples/crm_sample.sql     — 6 таблиц: компании, контакты, сделки, платежи
```

Поддерживаемые типы для CRM:
- Реквизиты: INN (9 цифр), KPP (9 цифр), OGRN (13 цифр) — обычно `skip`
- Бизнес-статусы: `Status`, `Stage`, `Terms`, `Method` — циклическая замена
- Город, отрасль: `City`, `Industry` — шифруются через `genre` (цикл)

---

## 🛡️ Гарантии целостности данных

### Первичные и внешние ключи (PK/FK)
Колонки, оканчивающиеся на `_id` или `Id` (например `CustomerId`, `InvoiceId`), **никогда не обрабатываются**. Они проходят транзитом без изменений. Гарантируется:
- Последовательности автоинкрементов целы
- Связи между таблицами intact
- Результат загружается обратно в MySQL без ошибок

### Кросс-табличная консистентность
Одинаковые исходные значения заменяются одинаково через `GlobalMappingRegistry`. Имя `'John'` в таблице `Customer` получит ту же замену, что и `'John'` в `Invoice` или `Employee`.

### Распределение сохраняется
Локальные трансформеры (`genre`, `crm_status`, `date_shuffle`) сохраняют распределение значений: если город `Москва` занимал 40% строк, после обработки он тоже будет встречаться в 40%.

---

## 📦 Установка

### Требования
- Python 3.11+
- Доступ к LLM API (совместимый с OpenAI Chat Completions)
- SQL-дамп в формате MySQL/MariaDB

```bash
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env           # Вставьте свой API ключ

# Интерактивно
python3 -m cloaker

# Или напрямую
python3 -m cloaker --batch examples/chinook_test.sql -o result.sql
```

---

## 🐳 Docker

### Сборка образа
Обraz собирается с поддержкой API ключа через `--build-arg`:

```bash
docker buildx build \
  --build-arg LLM_API_KEY=$(cat .env | grep 'LLM_API_KEY=' | cut -d= -f2) \
  -t cloakdb:latest .
```

### docker-compose (рекомендуется)
Автоматически монтирует конфиги, создаёт изолированное окружение.

```bash
docker compose up --build              # Мастер с выбором БД
docker compose run --rm cloakdb --batch /input/dump.sql   # Batch mode
```

---

## 📁 Структура проекта

```
my-sql-sanitizer/
├── cloaker/                              ← Главный модуль
│   ├── __main__.py                       ← CLI (мастер + пакетный режим)
│   ├── main.py                           ← SQLProcessor (фаза 1/2/3)
│   ├── config.py                         ← Парсер YAML + env
│   ├── cache.py                          ← GlobalMappingRegistry (кросс-таблица)
│   ├── llm_client.py                     ← Клиент LLM (чанкинг, retry)
│   ├── base_transformer.py               ← Базовый класс трансформеров
│   └── transformers/                     ← Реализации
│       ├── __init__.py                   ← Registry TRANSFORMER_MAP (12 типов)
│       ├── genre_transformer.py          ← Циклический swap (без LLM)
│       ├── crm_status_transformer.py     ← Статусы (без LLM)
│       ├── date_transformer.py           ← Шаффл дат (без LLM)
│       ├── name_transformer.py           ← Имена (LLM)
│       ├── email_transformer.py          ← Почты (LLM)
│       ├── phone_transformer.py          ← Телефоны (LLM)
│       ├── address_transformer.py        ← Адреса (LLM)
│       ├── company_transformer.py        ← Компании (LLM)
│       ├── composer_transformer.py       ← Композиторы (LLM)
│       ├── title_transformer.py          ← Должности (LLM)
│       ├── postal_code_transformer.py    ← Почтовые коды (LLM)
├── config.yaml                           ← Маппинг полей к трансформерам
├── examples/                             ← Тестовые SQL дампы
│   ├── chinook_test.sql                  ← Музыкальный магазин (11 табл.)
│   └── crm_sample.sql                    ← CRM российская (6 табл.)
├── prompt_templates/                     ← Шаблоны для LLM
│   ├── name.txt
│   ├── email.txt
│   ├── phone.txt
│   └── address.txt
├── start.sh                              ← Обёртка CLI
├── docker-compose.yml                    ← Оркестрация
├── Dockerfile.cloakdb                    ← Production образ
├── .env.example                          ← Шаблон переменных окружения
├── requirements.txt                      ← Python зависимости
└── README.md                             ← Этот файл
```

---

**License:** MIT
