# CloakDB — Анонимизатор SQL-дампов с LLM-трансформерами

CloakDB — самостоятельный инструмент для маскировки персональных данных (PII) в MySQL/MariaDB дампах. Использует LLM (любой OpenAI-совместимый API) для генерации реалистичных замен и детерминированные алгоритмы дляphone/email/даты. Расширяется через Python-трансформеры и YAML-конфиги.

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

Мастер проходит через 5 этапов:
1. **Выбор базы** — список `.sql` файлов в `examples/`
2. **Сканирование** — сбор ВСЕХ уникальных значений каждого поля
3. **Классификация полей** — автодетект типов по первым 50 значениям (`SAMPLES_PER_FIELD`)
4. **Загрузка маппингов** — LLM вызывается чанками (по 20 значений), результаты мержатся в единый словарь
5. **Обработка** — потоковая замена по словарю O(1)

*Зачем все значения? Для LLM маппинга — нужно знать ВСЕ уникальные значения, чтобы сгенерировать замену для каждого. 50 нужны только для автодетекта типа поля.*

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

- **LLM-поля** (`name`, `address`, `company`, `composer`, `title`): собираем уникальные значения → отправляем запросом с чанкингом → получаем JSON `{оригинал: замена}` → сохраняем в `GlobalMappingRegistry`
- **Детерминированные поля** (`email`, `phone`, `postal_code`, `genre`, `date_shuffle`, `skip`): маппинги вычисляются мгновенно на клиенте через SHA256 или циклический swap — **ни одного сетевого вызова**

**Экономия LLM-вызовов:** email, phone и postal_code теперь обрабатываются локально. Для Chinook (~15K строк) это экономит ~12 API-запросов за один дамп.

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
  Genre.Name: genre              # Жанры → циклическая замена
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
| Название содержит `address` | `address` (LLM) | `Customer.Address` |
| Название содержит `title` | `title` (LLM) | `JobTitle` |
| Числовые поля (`price`, `total`, `quantity`) | `skip` | `Invoice.Total` |
| Колонки `_id` или `Id` | `skip` | `CustomerId` |

Автодетект хорош для быстрого старта. Для продакшена рекомендуется прописать явные правила в `config.yaml`.

---

## 🔄 Алгоритмы трансформеров (полный справочник)

Все 12 типов трансформеров:

| Тип файла | Идентификатор в конфиге | Логика | Требуется LLM? |
|-----------|------------------------|--------|----------------|
| `genre_transformer.py` | `genre` | Циклический swap значений | ❌ Нет |
| `date_transformer.py` | `date_shuffle_month`, `date_shuffle_year` | Математический шифр дат | ❌ Нет |
| `email_transformer.py` | `email` | Детерминированный hash → реалистичное имя, домен сохраняется | ❌ Нет |
| `phone_transformer.py` | `phone` | Format-preserving замена цифр (последние 3-4), формат скобок/дефисов сохраняется | ❌ Нет |
| `name_transformer.py` | `name` | LLM реалистичные имена | ✅ Да |
| `company_transformer.py` | `company` | LLM названия компаний | ✅ Да |
| `address_transformer.py` | `address` | LLM адреса | ✅ Да |
| `composer_transformer.py` | `composer` | LLM композиторы | ✅ Да |
| `title_transformer.py` | `title` | LLM должности | ✅ Да |
| `postal_code_transformer.py` | `postal_code` | SHA256 hash, формат сохраняется (пробелы/дефисы) | ❌ Нет |
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

### `PostalCodeTransformer` (postal_code) — детерминированный, ZERO LLM

Hash-алгоритм (SHA256) генерирует замену, сохраняя формат почтового индекса: длину, разделители (пробелы, дефисы, скобки), регистр букв.

**Поддерживаемые форматы:**
- US ZIP:        98004 → 41597,  98004-1234 → 27990-7785
- Российский:     123456 → 000912
- UK:            SW1A 1AA → HJ0W 0RK
- Canadian:       K1A 0B1 → Y4S 1R9

**Алгоритм:** заменяет каждый алфанумерический символ на hash-derived альтернативу, сохраняя позицию разделителей.

| Где применять | Примеры полей |
|---------------|---------------|
| Почтовые индексы | `Customer.PostalCode`, `BillingAddress.PostalCode` |
| Индексы адресов | `Employee.PostalCode`, `Invoice.BillingPostalCode` |

---

## 2️⃣ LLM-трансформеры (чанки по 20 значений, потом O(1))

LLM вызывается чанками (по 20 значений). Пример: 270 уникальных значений → 14 API-запросов. Результаты мержатся в единый маппинг-словарь. Затем замена происходит по словарю O(1).

### `NameTransformer` (name)

Реалистичные имена другого человека того же языка/скрипта. Требует LLM.

**Где применять:** `Customer.FirstName`, `Employee.FirstName`, `Artist.Name`

**System Prompt:**
```
You are a data anonymization expert. 
Preserve the same ALPHABET (Latin stays Latin, Cyrillic stays Cyrillic). 
Keep name length similar (±30%). 
Match the case pattern (first letters capitalized). 
Output valid JSON only.
```

**User Prompt:**
```
Field: {field_key}
Description: {description}

Replace each name with a realistic alternative.
- Keep same alphabet (Cyrillic→Cyrillic, Latin→Latin)
- Keep similar length (±30% OK)
- Preserve case pattern
- Return JSON: {"original": "replacement", ...}
Example: {"John Smith": "James Anderson", "Иван Петров": "Алексей Сидоров"}
```

### `CompanyTransformer` (company)

Названия организаций.

**System Prompt:**
```
You are a data anonymization expert. 
Preserve alphabet and case pattern. Keep length similar. Output valid JSON only.
```

**User Prompt:**
```
Field: {field_key}
Replace each company name with a realistic alternative.
- Keep similar length (±30%)
- Preserve case pattern
- Return JSON: {"original": "replacement", ...}
Companies: "{company1}", "{company2}", ...
```

### `AddressTransformer` (address)

Новые улицы/дома/город, сохраняющие структуру.

**System Prompt:**
```
You are a data anonymization expert. 
Preserve address structure: same number of parts, same separators, same case pattern. 
Output valid JSON only.
```

**User Prompt:**
```
Field: {field_key}
Replace each address with a realistic alternative.
- Keep same structure (street, city, state, postal code)
- Preserve separators and case
- Return JSON: {"original": "replacement", ...}
Addresses:
- "{address1}"
- "{address2}"
```

### `ComposerTransformer` (composer)

Композиторы (аналогично NameTransformer, но с музыкальным контекстом).

**System Prompt:**
```
You are a data anonymization expert. 
Preserve the same alphabet and case pattern. 
Keep length similar. Output valid JSON only.
```

**User Prompt:**
```
Field: {field_key}
Replace each composer name with a realistic alternative composer.
- Keep similar length (±30%)
- Preserve case pattern
- Return JSON: {"original": "replacement", ...}
Composers: "{composer1}", "{composer2}", ...
```

### `TitleTransformer` (title)

Должности/роли.

**System Prompt:**
```
You are a data anonymization expert. 
Preserve alphabet and case pattern. Keep length similar. Output valid JSON only.
```

**User Prompt:**
```
Field: {field_key}
Replace each job title with another realistic title at similar level.
- Keep similar length (±30%)
- Preserve case pattern
- Return JSON: {"original": "replacement", ...}
Titles: "{title1}", "{title2}", ...
```

---

## ⚙️ Конфигурация

### config.yaml — полная структура

```yaml
# =============================================================================
# CloakDB Configuration — SQL Dump Sanitizer
# =============================================================================
# Format: Table.Column → transformer_type
# Available types: name, email, phone, address, title, company, genre,
#                  date_shuffle_year, date_shuffle_month, composer, postal_code
# Special: skip — column passes through unchanged (PK/FK, metrics)
# =============================================================================

transforms:
  # ── Album ──
  Album.AlbumId: skip
  Album.ArtistId: skip
  Album.Title: title

  # ── Artist ──
  Artist.ArtistId: skip
  Artist.Name: name

  # ── Customer ──
  Customer.Address: address
  Customer.City: genre
  Customer.Company: company
  Customer.Country: genre
  Customer.CustomerId: skip
  Customer.Email: email
  Customer.Fax: phone
  Customer.FirstName: name
  Customer.LastName: name
  Customer.Phone: phone
  Customer.PostalCode: postal_code
  Customer.State: genre
  Customer.SupportRepId: skip

  # ── Employee ──
  Employee.Address: address
  Employee.BirthDate: date_shuffle_year
  Employee.City: genre
  Employee.Country: genre
  Employee.Email: email
  Employee.EmployeeId: skip
  Employee.Fax: phone
  Employee.FirstName: name
  Employee.HireDate: date_shuffle_year
  Employee.LastName: name
  Employee.Phone: phone
  Employee.PostalCode: postal_code
  Employee.ReportsTo: skip
  Employee.State: genre
  Employee.Title: title

  # ── Genre ──
  Genre.GenreId: skip
  Genre.Name: genre

  # ── Invoice ──
  Invoice.BillingAddress: address
  Invoice.BillingCity: genre
  Invoice.BillingCountry: genre
  Invoice.BillingPostalCode: postal_code
  Invoice.BillingState: genre
  Invoice.CustomerId: skip
  Invoice.InvoiceDate: date_shuffle_year
  Invoice.InvoiceId: skip
  Invoice.Total: skip

  # ── InvoiceLine ──
  InvoiceLine.InvoiceId: skip
  InvoiceLine.InvoiceLineId: skip
  InvoiceLine.Quantity: skip
  InvoiceLine.TrackId: skip
  InvoiceLine.UnitPrice: skip

  # ── MediaType ──
  MediaType.MediaTypeId: skip
  MediaType.Name: genre

  # ── Playlist ──
  Playlist.Name: name
  Playlist.PlaylistId: skip

  # ── PlaylistTrack ──
  PlaylistTrack.PlaylistId: skip
  PlaylistTrack.TrackId: skip

  # ── Track ──
  Track.AlbumId: skip
  Track.Bytes: skip
  Track.Composer: composer
  Track.GenreId: skip
  Track.MediaTypeId: skip
  Track.Milliseconds: skip
  Track.Name: genre
  Track.TrackId: skip
  Track.UnitPrice: skip

# Параметры обработки (глубина выборки, пути вывода, таймауты) задаются в .env:
#   SAMPLES_PER_FIELD=50, PROFILES_DIR=output/profiles,
#   LLM_TIMEOUT_BASE=45, LLM_TIMEOUT_MAX=180
# Размер LLM-чанка (≤20 значений) зашит в cloaker/llm_client.py — он подобран
# под ограничение гейтвея по времени ответа (см. «Почему чанкинг?»).
# Парсер config.yaml читает ТОЛЬКО секцию transforms: — ниже справочно.
processing: {}
output: {}
```

> **Важно:** `skip` означает «не трогать вообще». PK/FK (`*_id`, `Id`) пропускаются автоматически.

### Переменные окружения (.env)

| Переменная | Значение по умолчанию | Описание |
|------------|----------------------|----------|
| `LLM_PROVIDER` | `ofox` | Провайдер: `ofox` \| `kodik` \| `openai` \| свой (задаёт пресеты endpoint/модели) |
| `LLM_API_KEY` | *(обязательно)* | Токен доступа (алиас: `LLM_API_TOKEN`) |
| `LLM_ENDPOINT` | пресет провайдера | URL Chat Completions API (пусто → пресет) |
| `LLM_MODEL` | пресет провайдера | Модель генерации (пусто → пресет) |
| `LLM_MAX_COMPLETION_TOKENS` | `32768` | Лимит токенов ответа LLM |
| `LLM_TIMEOUT_BASE` | `45` | Нижняя граница таймаута одного запроса, сек |
| `LLM_TIMEOUT_MAX` | `180` | Потолок таймаута самого крупного чанка, сек |
| `SAMPLES_PER_FIELD` | `50` | Уникальных значений на поле при профилировании (Phase 1) |
| `PROFILES_DIR` | `output/profiles` | Куда складывать JSON-профили полей |

### Почему чанкинг?

Ответ LLM ограничен `max_tokens` (настраивается через `LLM_MAX_COMPLETION_TOKENS`, по умолчанию 32768) — это лимит на ОБЪЁМ ОТВЕТА. Модель не может вернуть бесконечный JSON за один вызов.

Но главное ограничение — не токены, а **время**. Замерено на ofox: гейтвей обрывает
соединение примерно на 60 секундах (`curl rc=52` — empty reply), независимо от
клиентского таймаута. Увеличивать `LLM_TIMEOUT_MAX` бесполезно — соединение убьют
на сервере. Поэтому:

1. **Чанк ≤20 значений** — успевает сгенерироваться за ~25-30с (замер: 20 значений
   ≈ 26с успешно, 40+ ≈ обрыв).
2. **Авто-деление пополам** — если чанк всё же вернулся пустым (обрыв/таймаут),
   он режется пополам и повторяется (до 3 уровней: 20 → 10 → 5 → 2). Это спасает
   колонки с аномально длинными значениями.
3. **Таймаут адаптивный** — считается из реального размера запроса
   (`число значений × 2с + 8с на 1KB промпта`), в границах
   `LLM_TIMEOUT_BASE`…`LLM_TIMEOUT_MAX`. Раньше бюджет рос по *номеру* чанка,
   из-за чего первый (самый большой) чанок каждой колонки гарантированно падал.
4. **Частичный результат лучше потерянной колонки** — сбой одного чанка не отменяет
   обработку поля целиком.

**Стратегия чанкинга:** значения режутся на чанки ≤20 шт. (и в пределах лимита по
символам) → несколько запросов к API → результаты склеиваются в единый словарь.

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

## 📊 Демо-база

### Chinook (музыкальный магазин)
Стандартный тестовый дамп. Проверка основных типов полей: имена, жанры, города, телефоны, почты, даты.

```
examples/chinook_test.sql   — 11 таблиц, ~15 000 строк, 64 поля
```

Это единственная демо-база в поставке — она подставляется в мастер по умолчанию. Свой дамп кладётся в `examples/` и появляется в списке выбора.

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
Локальные трансформеры (`genre`, `date_shuffle`) сохраняют распределение значений: если город `Москва` занимал 40% строк, после обработки он тоже будет встречаться в 40%.

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
│       ├── date_transformer.py           ← Шаффл дат (без LLM)
│       ├── name_transformer.py           ← Имена (LLM)
│       ├── email_transformer.py          ← Почты (SHA256, ZERO LLM)
│       ├── phone_transformer.py          ← Телефоны (format-preserving, ZERO LLM)
│       ├── address_transformer.py        ← Адреса (LLM)
│       ├── company_transformer.py        ← Компании (LLM)
│       ├── composer_transformer.py       ← Композиторы (LLM)
│       ├── title_transformer.py          ← Должности (LLM)
│       └── postal_code_transformer.py    ← Почтовые коды (SHA256, ZERO LLM)
├── config.yaml                           ← Маппинг полей к трансформерам
├── examples/                             ← Тестовые SQL дампы
│   └── chinook_test.sql                  ← Музыкальный магазин (11 табл.)
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
- API ключ LLM-провайдера (ofox, Kodik Router, OpenAI … — любой OpenAI-совместимый)

### Шаг 1: Настройка окружения

```bash
# Скопируйте шаблон .env
cp .env.example .env

# Отредактируйте с вашим API ключом
vim .env
```

Содержимое `.env`:
```ini
LLM_PROVIDER=ofox
LLM_API_KEY=sk-your-key-here
# пусто → endpoint и модель берутся из пресета провайдера
LLM_ENDPOINT=
LLM_MODEL=
LLM_MAX_COMPLETION_TOKENS=32768
LLM_TIMEOUT_BASE=45
LLM_TIMEOUT_MAX=180
SAMPLES_PER_FIELD=50
```

### Шаг 2: Сборка образа

**Вариант A — через docker-compose (рекомендуется):**
```bash
docker compose build
```

**Вариант B — ручная сборка (секреты в образ НЕ попадают):**
```bash
# Собираем без запечённых ключей
docker build -t cloakdb:latest .

# Ключ и провайдер передаются в рантайме:
docker run --rm -e LLM_API_KEY=sk-your-key -e LLM_PROVIDER=ofox \
  -v $(pwd)/output:/output cloakdb:latest --batch /input/dump.sql -o /output/result.sql
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
3. **Кросс-табличная консистентность** — одинаковые значения в разных таблицах заменились одинаково

### Ручная проверка
```bash
# Сравните количество уникальных значений до и после
mysql -e "SELECT COUNT(DISTINCT FirstName) FROM Customer" sanitized_db

# Проверьте что новые значения действительно разные
grep -c "FirstName" result.sql
```

---

**License:** MIT
