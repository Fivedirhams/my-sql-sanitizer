# CloakDB — Анонимизатор SQL-дампов с ИИ-трансформерами

CloakDB — инструмент для маскировки персональных данных (PII) в MySQL/MariaDB дампах. Работает как обёртка поверх существующих систем (Greenmask, PostgreSQL native), расширяется через пользовательские Python-трансформеры и декларативные YAML-конфиги.

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

### Docker — полная инструкция
См. раздел `🐳 Docker — полная инструкция` в конце этого файла.

**Быстрый старт:**
```bash
cp .env.example .env && vim .env       # Настройте API ключ
docker compose up                      # Интерактивный мастер
docker compose run --rm cloakdb --batch /input/dump.sql   # Batch mode
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

### Фаза 2: Загрузка маппингов (LLM + детерминированные)

- **LLM-поля** (`name`, `address`, `company`, `composer`, `title`, `postal_code`): собираем уникальные значения → отправляем запросом с чанкингом → получаем JSON `{оригинал: замена}` → сохраняем в `GlobalMappingRegistry`
- **Детерминированные поля** (`email`, `phone`, `genre`, `crm_status`, `date_shuffle`, `id_guardian`, `skip`): маппинги вычисляются мгновенно на клиенте через SHA256 или циклический swap — **ни одного сетевого вызова**

**Экономия LLM-вызовов:** email и phone теперь обрабатываются локально. Для Chinook (~15K строк) это экономит ~10 API-запросов за один дамп.

### Фаза 3: Потоковая замена

Файл проходит повторно. Для каждой ячейки проверяется маппинг в словаре. Сложность O(1). Никаких сетевых вызовов.

---

## 🔧 Как связаны поля с трансформерами?

Маппинг работает в два уровня:

### Уровень 1: Явные правила (config.yaml)

В `config.yaml` в секции `transforms:` вы прописываете точно какие поля каким трансформером обрабатываются:

```yaml
transforms:
  Table.Column: type
  Customer.Email: email          # Почты → SHA256 hash (ZERO LLM)
  Customer.Phone: phone          # Телефоны → format-preserving (ZERO LLM)
  Artist.Name: name              # Имена музыкантов → LLM реалистичные
  deals.Stage: crm_status        # Статусы сделок → циклическая замена
  Genre.Name: genre              # Жанры → циклическая замена
  companies.Inn: id_guardian     # ИНН → детерминированный генератор
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
| Бизнес-коды: `inn`, `kpp`, `ogrn`, `passport` | `id_guardian` (checksums) | `companies.Inn` |
| Название содержит `title` | `title` (LLM) | `JobTitle` |
| Числовые поля (`price`, `total`, `quantity`) | `skip` | `Invoice.Total` |
| Колонки `_id` или `Id` | `skip` | `CustomerId` |

Автодетект хорош для быстрого старта. Для продакшена рекомендуется прописать явные правила в `config.yaml`.

---

## 🔄 Алгоритмы трансформеров (полный справочник)

Все 13 типов трансформеров:

| Тип файла | Идентификатор в конфиге | Логика | Требуется LLM? |
|-----------|------------------------|--------|----------------|
| `genre_transformer.py` | `genre` | Циклический swap значений | ❌ Нет |
| `crm_status_transformer.py` | `crm_status` | Циклический swap статусов | ❌ Нет |
| `date_transformer.py` | `date_shuffle_month`, `date_shuffle_year` | Математический шифр дат | ❌ Нет |
| `id_guardian_transformer.py` | `id_guardian` | Генерация ИНН/OGRN/KPP с checksums | ❌ Нет |
| `email_transformer.py` | `email` | Детерминированный hash → реалистичное имя, домен сохраняется | ❌ Нет |
| `phone_transformer.py` | `phone` | Format-preserving замена цифр (последние 3-4), формат скобок/дефисов сохраняется | ❌ Нет |
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

Переставляет значения по кругу: первое→второе, второе→третье, последнее→первое. Гарантирует сохранение процентного распределения.

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
| `Inn/Ogrn/Kpp` | Companies | юр. реквизиты → теперь `id_guardian` |

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

### `IDGuardianTransformer` — детерминированный генератор российских бизнес-идентификаторов

Новый трансформер для INN, KPP, OGRN, паспортов, контрактов. Генерирует РЕАЛЬНО ВАЛИДНЫЕ номера с правильными контрольными суммами. LLM тут категорически не подходит — он модульную арифметику не считает.

**Принцип работы:** берёт длину оригинала → определяет тип → генерирует случайные цифры с правильной формулой контрольной суммы.

#### Формулы контрольных сумм:

| Документ | Длина | Формула |
|----------|-------|---------|
| ИНН юрлица | 10 цифр | Первая девятка × [3,7,2,4,10,3,5,9,6] mod 11, остаток % 10 → десятая цифра |
| ИНН ИП | 12 цифр | Две контрольные с разными весами |
| ОГРН | 13 цифр | Первые 9 цифр × [2,4,10,3,5,9,4,6,8] mod 11 |
| КПП | 9 цифр | Произвольная комбинация допустимых символов |
| Паспорт РФ | XXXX XXXXXX | Серия (4 цифры, первая 4–9), номер (6 цифр) |

| Где применять | Примеры полей |
|---------------|---------------|
| ИНН компании | `companies.Inn` |
| КПП | `companies.Kpp` |
| ОГРН | `companies.Ogrn` |
| Номер паспорта | `contacts.PassportNumber` |
| Договоры | `contracts.Number` |

**Пример работы:**
```
Input:  companies.Inn = "7707083893" (ИНН ЮЛ)
Output: "5925389347" (валидный ИНН с правильной контрольной)
```

### `EmailTransformer` (email) — детерминированный, ZERO LLM

Hash-алгоритм (SHA256) генерирует реалистичную локальную часть (`juan@gmail.com` → `juzija@gmail.com`), доменная часть (`@gmail.com`) сохраняется без изменений.

**Алгоритм:**
1. Извлекаем домен после `@`
2. Хэшируем полный email → смещение `offset`
3. Генерируем 2-3 слога из словаря гласных/согласных
4. Вставляем первые символы оригинального имени для узнаваемости

**Примеры:**
```
juan@gmail.com     → juzija@gmail.com
luis@yahoo.com     → lurobe@yahoo.com
maria@hotmail.com  → mariba@hotmail.com
unknown@test.com   → untida@test.com
```

### `PhoneTransformer` (phone) — format-preserving, ZERO LLM

Заменяет последние 3-4 цифры номера с сохранением полного формата: скобки, дефисы, пробелы, код страны. SHA256 seed + multi-round XOR+rotation.

**Примеры:**
```
+1 (425) 555-0174    → +1 (423) 346-9221
+7 (495) 123-45-67   → +7 (494) 308-14-29
(514) 721-4711       → (514) 270-1680
8-800-555-35-35      → 8-803-614-18-24
```

---

## 2️⃣ LLM-трансформеры (один вызов на колонку, потом O(1))

Каждый из этих трансформеров вызывает LLM API один раз для загрузки маппинга всех уникальных значений колонки. После этого замена происходит по словарю (O(1)).

### `NameTransformer` (name)

Реалистичные имена другого человека того же языка/скрипта. Требует LLM.

**Где применять:** `Customer.FirstName`, `Employee.FirstName`, `Artist.Name`

**Формат запроса к LLM:**
```
SYSTEM PROMPT:
"You are a professional data anonymization expert. Generate realistic anonymized replacements for each name provided. All output must be valid JSON only, no explanation text."

USER PROMPT:
Field: {field_key}
Description: {description}
Original values ({count} total):
"{name1}", "{name2}", "{name3}"...

Replace each of these names with a realistic, culturally appropriate alternative name.
Return a JSON object where keys are the original names and values are the new names.
Example: {"John Smith": "James Anderson", "Jane Doe": "Sarah Williams"}

Parameters:
- max_tokens: 4096
- temperature: 0.3
- response_format: {"type": "json_object"}
- chunk_size: максимум 15 имён на вызов
```

### `CompanyTransformer` (company)

Названия организаций масштаба региона оригинала.

**Где применять:** `Customer.Company`, `companies.CompanyName`

**Формат запроса к LLM:**
```
USER PROMPT:
Field: {field_key}
Replace each company name with a realistic alternative company name.
Return JSON: {"original_company": "new_company", ...}
Companies: "{company1}", "{company2}", "{company3}"...
```

### `AddressTransformer` (address)

Новые улицы/дома/город, сохраняющие структуру адреса.

**Где применять:** `Customer.Address`, `Employee.Address`, `Invoice.BillingAddress`, `companies.LegalAddress`

**Формат запроса к LLM:**
```
USER PROMPT:
Field: {field_key}
Replace each address with a realistic alternative address.
Preserve the general structure but change street names, cities, etc.
Return JSON: {"original": "new", ...}
Addresses:
- "{address1}"
- "{address2}"
- "{address3}"...
```

### `ComposerTransformer` (composer)

Композиторы (аналогично NameTransformer, но с музыкальным контекстом).

### `TitleTransformer` (title)

Должности/роли (Sales Representative, CFO, Director).

**Формат запроса к LLM:**
```
USER PROMPT:
Field: {field_key}
Replace each job title with another realistic job title at a similar level.
Return JSON: {"original_title": "new_title", ...}
Titles: "{title1}", "{title2}", "{title3}"...
```

### `PostalCodeTransformer` (postal_code)

Почтовые коды с сохранением формата (XXX-XX или XXXXX).

**Формат запроса к LLM:**
```
USER PROMPT:
Field: {field_key}
Replace each postal code with another valid postal code of the same format.
Return JSON: {"original": "new", ...}
Codes:
- "{code1}"
- "{code2}"
- "{code3}"...
```

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
  Customer.Email: email               # Почты → SHA256 (ZERO LLM)
  Customer.Phone: phone               # Телефоны → format-preserving (ZERO LLM)
  Customer.State: genre               # Штаты → циклический swap
  Genre.Name: genre                   # Жанры → циклический swap (без LLM!)
  MediaType.Name: genre               # Типы медиа → циклический swap
  Album.Title: title                  # Названия альбомов → LLM
  Track.Name: name                    # Названия треков → LLM
  Invoice.InvoiceDate: date_shuffle   # Даты счетов → shuffle дат
  Employee.BirthDate: date_shuffle    # Даты рождения → shuffle дат
  Employee.HireDate: date_shuffle     # Дата найма → shuffle дат
  companies.Inn: id_guardian          # ИНН → детерминированный генератор (checksums!)
  companies.Kpp: id_guardian          # КПП
  companies.Ogrn: id_guardian         # ОГРН
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

### Почему чанкинг?

API устанавливает `max_tokens: 4096` — это жёсткий лимит на ОБЪЁМ ОТВЕТА. Даже если модель поддерживает миллионный контекст, ответить JSON с 100 парами ключ-значение физически невозможно за 4096 токенов.

**Стратегия чанкинга:** делим 100 значений на 7 чанков по 15 значений → 7 запросов к API → результат слепляется в единый словарь.

---

## 💾 Кросс-табличная консистентность

`GlobalMappingRegistry` — единое хранилище замен для всего процесса. Гарантирует что одно и то же исходное значение всегда получает одну и ту же замену, независимо от того, в какой таблице оно встретилось.

### Как это работает технически

Внутри класса `GlobalMappingRegistry` хранится один общий словарь:

```python
class GlobalMappingRegistry:
    _mapping: Dict[str, str] = {}      # {оригинал: замена}
    
    def get_replacement(original):     # ← ПРОВЕРЯЕТСЯ В САМОМ НАЧАЛЕ
        return self._mapping.get(original)
    
    def set_mapping(original, repl):   # ← ЗАПИСЫВАЕТСЯ КУДА-ТО ЕДИНСТВЕННОМУ
        self._mapping[original] = repl
```

**Алгоритм при запуске санитизации:**

| Шаг | Что происходит |
|-----|----------------|
| 1. Обрабатываем `Customer.FirstName='John'` | `EmailTransformer._load_mapping()` → LLM генерирует `{'John': 'Elena'}` → вызывает `reg.set_mapping('John', 'Elena')` |
| 2. Обрабатываем `Invoice.ContactName='John'` | Тот же `'John'` → смотрит `reg.get_replacement('John')` → видит `'Elena'` → использует сразу, без нового LLM вызова |

### Важный нюанс

Один и тот же ключ `'John'` хранится в ОДНОМ ГЛОБАЛЬНОМ СЛОВАРЕ. Это значит что если имя `'John'` встречается в столбце `FirstName` И в столбце `LastName`, оно получит одинаковую замену → например → `'Elena'`.

Это **корректная** стратегия анонимизации: если 'John' — это реальный человек, и мы заменяем его псевдонимом, этот псевдоним должен оставаться CONSISTENT across all references. Не должно быть так что John в Customer → Elena, а тот же John в Employee → Michael. Это разрушило бы целостность данных.

Если вам нужно независимое шифрование разных колонок для одного значения — используйте явно `shuffle`-трансформеры которые работают изолированно для каждой колонки.

### Персистентность

При завершении работы `GlobalMappingRegistry` сохраняется в `global_mapping.json`. При повторном запуске с тем же дампом маппинг загружается из файла — ни одного вызова к API, мгновенная работа.

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
- Реквизиты: INN (9 цифр), KPP (9 цифр), OGRN (13 цифр) → `id_guardian` (с проверкой checksum)
- Бизнес-статусы: `Status`, `Stage`, `Terms`, `Method` → циклическая замена (`crm_status`)
- Город, отрасль: `City`, `Industry` → шифруются через `genre` (цикл)

---

## 🛡️ Гарантии целостности данных

### Первичные и внешние ключи (PK/FK)
Колонки, оканчивающиеся на `_id` или `Id` (например `CustomerId`, `InvoiceId`), **никогда не обрабатываются**. Они проходят транзитом без изменений. Гарантируется:
- Последовательности автоинкрементов целы
- Связи между таблицами intact
- Результат загружается обратно в MySQL без ошибок

### Бизнес-идентификаторы
Новый `IDGuardianTransformer` гарантирует что после замены:
- Контрольная сумма ИНН остаётся валидной
- Длина числа не меняется
- Структура (разделители серии/номера) сохраняется

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
│       ├── __init__.py                   ← Registry TRANSFORMER_MAP (13 типов)
│       ├── genre_transformer.py          ← Циклический swap (без LLM)
│       ├── crm_status_transformer.py     ← Статусы (без LLM)
│       ├── date_transformer.py           ← Шаффл дат (без LLM)
│       ├── id_guardian_transformer.py    ← ИНН/OGRN/паспорта с checksum (NEW)
│       ├── name_transformer.py           ← Имена (LLM)
│       ├── email_transformer.py          ← Почты (SHA256, ZERO LLM)
│       ├── phone_transformer.py          ← Телефоны (format-preserving, ZERO LLM)
│       ├── address_transformer.py        ← Адреса (LLM)
│       ├── company_transformer.py        ← Компании (LLM)
│       ├── composer_transformer.py       ← Композиторы (LLM)
│       ├── title_transformer.py          ← Должности (LLM)
│       └── postal_code_transformer.py    ← Почтовые коды (LLM)
├── config.yaml                           ← Маппинг полей к трансформерам
├── examples/                             ← Тестовые SQL дампы
│   ├── chinook_test.sql                  ← Музыкальный магазин (11 табл.)
│   └── crm_sample.sql                    ← CRM российская (6 табл.)
├── start.sh                              ← Обёртка CLI
├── docker-compose.yml                    ← Оркестрация
├── Dockerfile.cloakdb                    ← Production образ
├── .env.example                          ← Шаблон переменных окружения
├── requirements.txt                      ← Python зависимости
└── README.md                             ← Этот файл
```

---

## 🐳 Docker — полная инструкция

### Предварительные требования
- Docker ≥ 20.10 и Docker Compose ≥ 2.0
- API ключ Kodik Router (получите на https://api.kodikrouter.ru)

### Шаг 1: Настройка окружения

```bash
# Скопируйте шаблон .env
cp .env.example .env

# Отредактируйте с вашим API ключом
vim .env
```

Содержимое `.env`:
```ini
LLM_API_KEY=sk-your-key-here
LLM_ENDPOINT=https://api.kodikrouter.ru/v1
LLM_MODEL=qwen/qwen3.7-flash
MAX_TOKENS=4096
BATCH_SIZE=20
```

### Шаг 2: Сборка образа

**Вариант A — через docker-compose (рекомендуется):**
```bash
docker compose build
```

**Вариант B — ручная сборка:**
```bash
# Ключ подставляется через --build-arg
LLM_KEY=$(cat .env | grep 'LLM_API_KEY=' | cut -d= -f2)

docker buildx build \
  --build-arg LLM_API_KEY="$LLM_KEY" \
  --build-arg LLM_ENDPOINT=https://api.kodikrouter.ru/v1 \
  --build-arg LLM_MODEL=qwen/qwen3.7-flash \
  --build-arg MAX_TOKENS=4096 \
  -t cloakdb:latest .
```

### Шаг 3: Запуск

#### Интерактивный мастер (выбор БД, проверка конфига)
```bash
docker compose up
```
Мастер пройдёт по 4 этапам:
1. Выбор SQL дампа из `examples/`
2. Сбор ВСЕХ уникальных значений каждого поля
3. Проверка автоклассификации + маппинг к трансформерам
4. Запуск обработки с индикатором прогресса

Результат сохранится в `output/`

#### Пакетный режим (для CI/CD)
```bash
# Сопутствующий файл mount'ен как /input/dump.sql
docker compose run --rm cloakdb --batch /input/dump.sql -o /output/result.sql
```

#### Запуск без мастера (минимальный контейнер)
```bash
docker run --rm \
  -v $(pwd)/examples:/data \
  -v $(pwd)/output:/output \
  -e LLM_API_KEY=$(cat .env | grep 'LLM_API_KEY=' | cut -d= -f2) \
  cloakdb:latest --batch /data/chinook_test.sql -o /output/sanitized.sql
```

### Структура volume mount'ов внутри контейнера
| Host path        | Container path | Назначение |
|------------------|---------------|------------|
| `./config.yaml`  | `/app/config.yaml` | Маппинг полей |
| `./examples/`    | `/app/examples/` | Исходные дампы |
| `./output/`      | `/app/output/` | Результаты + профили |
| `./.env`         | ENV vars | Переменные окружения |

### Очистка
```bash
docker compose down
docker image rm cloakdb:latest   # Если нужно удалить образ
rm -rf output/profiles/ output/global_mapping.json
```

---

## 🧪 Проверка качества результата

### Автоматические проверки
После выполнения можно проверить:
1. **Отсутствие дублей PK** — все `*_id` сохранены без изменений
2. **Нулевые хеш-плейсхолдеры** — никаких `"hash_abc123"` или фиктивных строк
3. **Контрольные суммы ИНН** — если применялся `id_guardian`, все генерированные номера должны пройти валидацию
4. **Кросс-табличная консистентность** — одинаковые значения в разных таблицах заменились одинаково

### Ручная проверка
```bash
# Сравните количество уникальных значений до и после
mysql -e "SELECT COUNT(DISTINCT FirstName) FROM Customer" sanitized_db

# Проверьте что новые значения действительно разные
grep -c "FirstName" result.sql
```

---

**License:** MIT
