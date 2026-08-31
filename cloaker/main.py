"""SQL stream processor — reads MySQL dump, applies transformations."""

from __future__ import annotations

import codecs
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from cloaker.config import CloakDBConfig
from cloaker.cache import GlobalMappingRegistry
from cloaker.transformers import get_transformer


# ── Кодировка входного дампа ────────────────────────────────────
_SNIFF_BYTES = 1 << 20          # 1 MiB заголовка достаточно, чтобы определить чарсет
_ENC_ALIASES = {
    "utf8": "utf-8", "utf-8": "utf-8", "utf8mb4": "utf-8", "utf8mb3": "utf-8",
    "latin1": "cp1252", "latin": "cp1252", "iso-8859-1": "cp1252",
    "cp1251": "cp1251", "windows-1251": "cp1251", "1251": "cp1251",
    "koi8-r": "koi8-r", "cp866": "cp866", "maccyr": "mac-cyrillic",
}


def normalize_encoding(name: str):
    """Имя чарсета из заголовка дампа -> имя для open(). None, если такого нет."""
    if not name:
        return None
    key = name.strip().lower().replace("_", "-")
    if key in _ENC_ALIASES:
        return _ENC_ALIASES[key]
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _decodes(data: bytes, enc: str) -> bool:
    """Декодируется ли кусок без ошибок — с поправкой на обрезанный символ на конце.

    Срез по _SNIFF_BYTES почти всегда приходит в середину многобайтового символа,
    и наивный decode выдал бы UnicodeDecodeError на валидном UTF-8 дампе.
    """
    try:
        data.decode(enc)
        return True
    except UnicodeDecodeError as e:
        compact = enc.lower().replace("-", "")
        return compact.startswith("utf8") and e.start >= len(data) - 4


def detect_sql_encoding(path) -> str:
    """Кодировка дампа: подсказка из заголовка, иначе последовательная эвристика.

    mysqldump пишет в шапку `SET NAMES 'cp1251'` и `DEFAULT CHARSET=...`, а для
    старых русских баз cp1251 — норма. Раньше профиль читался вообще без encoding
    (то есть по локали контейнера: в образе с LANG=C там ascii), а поток — жёстко
    по utf-8, и не-UTF-8 дамп ронял мастер на шаге 2 с UnicodeDecodeError.
    """
    with open(path, "rb") as f:
        head = f.read(_SNIFF_BYTES)
    text = head.decode("latin-1", "replace")
    for pat in (r"SET\s+NAMES\s+'?([A-Za-z0-9_-]+)'?",
                r"DEFAULT\s+CHARSET\s*=\s*'?([A-Za-z0-9_-]+)",
                r"--\s*Charset:\s*([A-Za-z0-9_-]+)"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            enc = normalize_encoding(m.group(1))
            if enc and _decodes(head, enc):
                return enc
    for enc in ("utf-8", "cp1251", "koi8-r", "cp1252"):
        if _decodes(head, enc):
            # BOM — единственный случай, когда стоит соврать «как есть»: utf-8-sig
            # декодирует и обычный ASCII, поэтому проверять нужно именно байты BOM,
            # а не порядок попыток декодирования.
            if enc == "utf-8" and head.startswith(b"\xef\xbb\xbf"):
                return "utf-8-sig"
            return enc
    return "latin-1"     # декодирует любые байты — лучше, чем бить на полфайла


def writer_encoding(read_encoding: str) -> str:
    """В выходной дампер BOM не нужен: utf-8-sig при записи превращаем в utf-8."""
    return "utf-8" if read_encoding == "utf-8-sig" else read_encoding


class DumpNotAnonymizable(RuntimeError):
    """Да́мп не поддаётся анонимизации — и потому не годится на выход.

    Отдельный класс, чтобы CLI падал с кодом 1, а не печатал «✅ Done!»: раньше
    файл без CREATE TABLE (типичная выгрузка `mysqldump --no-create-info`) проходил
    с нулём найденных полей и нулём обработанных строк, и на выход уходил
    оригинальный дамп с настоящими персональными данными.
    """

class SQLProcessor:
    """Stream processor for MySQL dumps with custom transformers.
    
    Architecture (3-phase flow):
      Phase 1: Scan dump → collect ALL unique values per table+column (no limit)
      Phase 2: Load ALL LLM mappings via chunked batch calls (sized by prompt budget)
      Phase 3: Stream through dump → O(1) dict lookup per cell
    
    Cross-table consistency: GlobalMappingRegistry ensures identical source values
    produce identical masked values across ALL tables/columns.
    """

    # Значение по умолчанию нужно для прямых вызовов вне process_file — например,
    # шаг 2 мастера читает профиль сразу после создания SQLProcessor.
    _dump_encoding: str = "utf-8"

    def __init__(self, config: CloakDBConfig) -> None:
        self.config = config
        # Get singleton instance of global mapping registry
        self.reg = GlobalMappingRegistry.instance()
        
        # Schema discovered from CREATE TABLE statements
        self.schema: Dict[str, list] = {}
        
        # Active context during parsing
        self._current_table: Optional[str] = None
        self._current_columns: list = []
    
    # ── Public API ─────────────────────────────────────────────────────
    
    def process_file(self, input_path: str, output_path: str) -> None:
        """Read a SQL file, apply transformations, write sanitized output."""
        in_path = Path(input_path)
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Кодировка определяется один раз по входу и та же используется для выхода,
        # чтобы дамп импортировался в тот же чарсет, из которого был выгружен.
        self._dump_encoding = detect_sql_encoding(in_path)
        if self._dump_encoding not in ("utf-8", "utf-8-sig"):
            print(f"  ℹ️  Дамп в кодировке {self._dump_encoding} — читаю и пишу в ней же")
        
        # Try loading existing global mappings from previous run
        mapping_file = str(out_path.parent / "global_mapping.json")
        if self.reg.load_from_file(mapping_file):
            print(f"  🔄 Loaded {self.reg.total_entries} pre-existing mappings "
                  f"(global + по областям колонок) from {mapping_file}")
        
        # Phase 1: Collect ALL unique samples (no limit)
        print("[1/4] Collecting all unique value samples...")
        samples = self._collect_samples(str(in_path))
        total_unique = sum(len(v) for v in samples.values())
        print(f"  Found {len(samples)} fields, {total_unique} total unique values")

        # Fail-closed: ноль найденных полей означает, что разбиратель не увидел ни
        # одного `INSERT INTO tbl (cols) VALUES` — типично для выгрузки без DDL
        # (`mysqldump --no-create-info`). Молча писать в выход входной дамп с
        # настоящими персональными данными — худший из возможных исходов.
        if not samples:
            raise DumpNotAnonymizable(
                f"В {in_path.name} не найдено ни одного столбца для анонимизации "
                f"(нет `INSERT INTO ... (список столбцов) VALUES` — например, дамп "
                f"без DDL/`--no-create-info`). Файл НЕ анонимизирован.")
        
        # Phase 2: Load ALL LLM mappings (chunked batch calls sized by prompt budget)
        print("[2/4] Loading transformation mappings via LLM...")
        transformers = self._load_all_mappings(samples)
        
        # Save global mappings for future runs
        try:
            self.reg.save_to_file(mapping_file)
            print(f"  💾 Saved {self.reg.total_entries} mappings "
                  f"({len(self.reg._mapping)} глобальных + "
                  f"{sum(len(v) for v in self.reg._scoped.values())} по областям)")
        except Exception as e:
            print(f"  ⚠️ Could not save mappings: {e}")
        
        # Phase 3: Stream through dump with active transformers
        print(f"[3/4] Streaming anonymized output to {out_path}...")
        row_count = self._stream_transform(in_path, out_path, transformers)

        if row_count == 0:
            # Выход уже записан и это тот же дамп, что пришёл: удаляем, чтобы его
            # никто не забрал как «анонимизированный», и падаем с ненулевым кодом.
            out_path.unlink(missing_ok=True)
            raise DumpNotAnonymizable(
                f"Не обработано ни одной строки из {in_path.name} — выход удалён, "
                f"данные не обезличены. Проверьте, что дамп содержит INSERT "
                f"по таблицам, объявленным в CREATE TABLE.")
        
        print(f"\n✅ Done! Output: {out_path}")
        print(f"   Processed {row_count} rows")
        print(f"   Replacements cached: {self.reg.total_entries} "
              f"(глобально {len(self.reg._mapping)}, в областях колонок "
              f"{sum(len(v) for v in self.reg._scoped.values())})")
        
        return row_count
    
    # ── Phase 1: Sample collection ─────────────────────────────────────
    
    def _collect_samples(self, sql_path: str) -> Dict[str, list]:
        """First pass: read dump, collect ALL unique values per table+column.
        
        Chinook format example:
          INSERT INTO `Artist` (`ArtistId`, `Name`) VALUES
              (1, N'AC/DC'),
              (2, N'Aerosmith'),
        
        Strategy: extract table+columns from INSERT header, then process
        value tuples on ALL subsequent lines until next INSERT/DDL.
        
        NO SAMPLE LIMIT: we collect EVERY unique value per column so that
        LLM gets complete context. Strings in SQL dumps are short (names, cities,
        emails etc.), even 10k uniques = ~200KB RAM — negligible.
        """

        
        samples: Dict[str, list] = {}
        seen: Dict[str, set] = {}
        
        current_table = None
        current_columns: list = []
        
        with open(sql_path, encoding=self._dump_encoding) as f:
            for line in f:
                stripped = line.strip()
                
                # Detect INSERT INTO `table` (`col1`, ...) VALUES
                insert_header = re.search(
                    r'INSERT\s+INTO\s+`?(\w+)`?\s*\(?',
                    stripped, re.IGNORECASE
                )
                if insert_header:
                    current_table = insert_header.group(1)
                    
                    # Extract column names from backticks in this line
                    all_backticks = re.findall(r'`(\w+)`', stripped)
                    # Filter out SQL keywords AND the table name itself
                    valid_cols = [
                        c for c in all_backticks
                        if c.upper() not in ('VALUES', 'INTO', 'SET', 'SELECT', 'INSERT')
                        and c != current_table
                    ]
                    current_columns = valid_cols
                    
                    # Check if we also have VALUES keyword
                    has_values = bool(re.search(r'VALUES\s*$', stripped, re.IGNORECASE))
                    
                    if has_values:
                        # This is the INSERT header with VALUES marker
                        # Values start on NEXT lines, so skip this line
                        continue
                    else:
                        # No VALUES yet, wait for more input
                        continue
                
                # If we have table and columns but missing columns somehow
                if current_table and current_columns:
                    # Skip lines that don't contain value tuples
                    if not re.search(r'\(', stripped):
                        continue
                    
                    # Reset state on DDL statements
                    if stripped.startswith('CREATE ') or stripped.startswith('DROP '):
                        current_table = None
                        current_columns = []
                        continue
                
                if not current_table or not current_columns:
                    continue
                
                # Extract value tuples from this line
                tuples = self._extract_value_tuples(stripped)
                
                for tuple_str in tuples:
                    vals = self._parse_values_tuple(tuple_str)
                    
                    # Only process up to the number of known columns
                    n_cols = min(len(vals), len(current_columns))
                    
                    for i in range(n_cols):
                        val = vals[i]
                        if val is None or (isinstance(val, str) and val.upper() == 'NULL'):
                            continue
                        
                        # Clean the value string
                        clean_val = val.strip().rstrip(',')  # Remove trailing comma
                        if clean_val.startswith("N'"):
                            clean_val = clean_val[1:]
                        elif clean_val.startswith('N"'):
                            clean_val = clean_val[1:]
                        # Strip surrounding quotes
                        if len(clean_val) >= 2 and clean_val[0] == "'" and clean_val[-1] == "'":
                            clean_val = clean_val[1:-1]
                        elif len(clean_val) >= 2 and clean_val[0] == '"' and clean_val[-1] == '"':
                            clean_val = clean_val[1:-1]
                        clean_val = clean_val.strip()
                        if not clean_val:
                            continue
                        
                        col_key = f"{current_table}_{current_columns[i]}"
                        
                        if col_key not in seen:
                            seen[col_key] = set()
                            samples[col_key] = []
                        
                        if col_key not in seen:
                            seen[col_key] = set()
                            samples[col_key] = []
                        
                        if clean_val not in seen[col_key]:
                            seen[col_key].add(clean_val)
                            samples[col_key].append({"value": clean_val})
        
        return samples

    @staticmethod
    def _extract_value_tuples(line: str) -> list:
        """Extract all (val1, val2, ...) tuples from a SQL line.
        
        Handles: (1, N'Rock'), (2, N'Jazz'), or multi-line continuations.
        Skips trailing commas between tuples like "(val),".
        """
        result = []
        i = 0
        n = len(line)
        
        while i < n:
            # Skip whitespace
            while i < n and line[i] in ' \t\n\r':
                i += 1
            if i >= n:
                break
                
            # Find opening (
            if line[i] != '(':
                # Could be a trailing comma or other noise - skip
                i += 1
                continue
            
            # Find matching closing ),
            depth = 0
            start = i
            in_quote = False
            j = i
            
            while j < n:
                ch = line[j]
                if ch == "'":
                    in_quote = not in_quote
                elif not in_quote:
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth <= 0:
                            # Found end of tuple
                            tuple_str = line[start:j+1].strip().rstrip(',').strip()
                            if tuple_str and tuple_str.startswith('('):
                                result.append(tuple_str)
                            i = j + 1
                            break
                j += 1
            else:
                # No matching close found - use what we have
                if j >= n:
                    tuple_str = line[start:].strip().rstrip(',').strip()
                    if tuple_str and tuple_str.startswith('('):
                        result.append(tuple_str)
                    i = n
        
        return result
    
    @staticmethod
    def _parse_values_tuple(tuple_str: str) -> list:
        """Parse '(val1, val2)' into raw value strings.
        
        Handles various SQL formats: (1, N'Rock'), ('Test',), etc.
        Cleans up trailing commas and whitespace from values.
        """
        tuple_str = tuple_str.strip()
        
        # Remove trailing comma/space before )  e.g. "(1, 'val')," -> "(1, 'val')"
        tuple_str = tuple_str.rstrip(',')
        # Now strip outer parens
        while tuple_str.startswith('('):
            tuple_str = tuple_str[1:]
        while tuple_str.endswith(')'):
            tuple_str = tuple_str[:-1]
        
        values = []
        current = []
        in_single = False
        
        for ch in tuple_str:
            if ch == "'":
                in_single = not in_single
            
            if ch == ',' and not in_single:
                values.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        
        remaining = "".join(current).strip()
        if remaining:
            values.append(remaining)
        
        return values
    
    # ── Phase 2: Load LLM mappings ─────────────────────────────────────
    
    def _explicit_rule(self, table: str, column: str, field_key: str) -> Optional[str]:
        """Явное правило из config.yaml для поля.

        В конфиге ключи пишутся `Table.Column`, а внутри движка поле — это
        `Table_Column`. Поиск шёл прямым сравнением по внутреннему ключу, поэтому
        ни одно из 39 правил конфига не совпадало, и поля, которым предписан
        дешёвый локальный `genre` (Genre.Name, MediaType.Name, ...), уезжали в
        платную LLM-ветку `name`.
        """
        rules = self.config.transform_rules
        if not rules:
            return None
        return rules.get(f"{table}.{column}") or rules.get(field_key)

    def _load_all_mappings(
        self, samples: Dict[str, list]
    ) -> Dict[str, Any]:
        """Load transformation mappings for all fields (batch LLM calls).
        
        Returns a dict of {field_key: transformer_instance} with loaded mappings.
        """
        transformers: Dict[str, Any] = {}
        loaded = 0
        skipped = 0
        t_start = time.monotonic()
        total_fields = len(samples)
        
        for field_key, items in samples.items():
            parts = field_key.split('_', 1)
            if len(parts) != 2:
                skipped += 1
                continue
            
            table, column = parts
            
            # Skip PK/FK
            if self._is_skip_column(column):
                skipped += 1
                continue
            
            # Need at least some samples to justify an LLM call
            if len(items) < 2:
                skipped += 1
                continue
            
            # Check for explicit transform rule
            explicit_rule = self._explicit_rule(table, column, field_key)
            if explicit_rule:
                transformer_type = explicit_rule
            else:
                transformer_type = self._auto_select_transformer(column, table, items)
            
            try:
                transformer = get_transformer(transformer_type, self.config)
                
                # Calculate stats from samples for better LLM context
                stats = self._calc_sample_stats(items)
                
                transformer._load_mapping(items, field_key, stats)
                transformer._loaded = True
                
                transformers[field_key] = transformer
                loaded += 1
                
                # Пишем по каждому полю и с таймером: раньше печать шла раз на 5
                # полей, и шаг 4 выглядел зависшим на десятки минут.
                print(f"  [{loaded + skipped}/{total_fields}] {field_key} "
                      f"({transformer_type}): {len(transformer._mapping)} маппингов, "
                      f"{time.monotonic() - t_start:.0f} с с начала")
                
            except Exception as e:
                # str(e)[:200]: стандартное сообщение subprocess.TimeoutExpired
                # включает команду целиком — с заголовком Authorization и ключом.
                detail = " ".join(str(e).split())[:200]
                print(f"  [{loaded + skipped}/{total_fields}] ⚠️  {field_key}: "
                      f"{type(e).__name__}: {detail} — пропуск, {time.monotonic() - t_start:.0f} с"
                      )
                skipped += 1
        
        print(f"\n  Loaded mappings: {loaded}, Skipped (PK/FK/<2 samples): {skipped}")
        return transformers
    
    @staticmethod
    def _calc_sample_stats(items: list) -> Dict[str, Any]:
        """Calculate basic statistics from sample items."""
        values = [i["value"] for i in items if i.get("value")]
        lengths = [len(v) for v in values]
        return {
            "unique_count": len(values),
            "avg_length": sum(lengths) / len(lengths) if lengths else 0,
            "min_length": min(lengths) if lengths else 0,
            "max_length": max(lengths) if lengths else 0,
        }
    
    @staticmethod
    def _is_skip_column(column: str) -> bool:
        """Check if column should be skipped (PK/FK pattern or numeric-only).
        
        Handles ALL capitalization variants: CustomerId, ContactID, DealId, track_id...
        """
        col_lower = column.lower()
        
        # PK/FK ending: *_id, *Id, *ID (all case combos)
        if col_lower.endswith('_id') or col_lower.endswith('id'):
            return True
        
        # Standalone ID / PK
        if col_lower in ('id', 'pk'):
            return True
        
        # Known FK patterns (no _id suffix but still foreign keys)
        fk_patterns = ('reportsto', 'managerid', 'repid')
        if col_lower in fk_patterns:
            return True
        
        # Skip numeric/measure columns using exact match or substring
        lower_col = column.lower()
        numeric_indicators = (
            'quantity', 'unitprice', '_total', 'total', 
            'bytes', 'milliseconds', 'duration', 'weight'
        )
        for ind in numeric_indicators:
            if ind in lower_col:
                return True
        
        return False
    
    def select_transformers(self, samples: Dict[str, list]) -> Dict[str, dict]:
        """Pre-analyze samples and return field classification for wizard display.
        
        Used by interactive mode to show user what will be sanitized and how.
        Does NOT load mappings — just classifies types based on naming patterns.
        
        Returns dict of {field_key: {type, samples count, note}}
        """
        selected_fields = {}
        # Ограничение для автодетекта типов - используем только первые N значений
        sample_limit = self.config.processing.sample_limit
        
        for field_key, items in samples.items():
            # Для классификации типов используем только sample_limit значений
            items_for_classify = items[:sample_limit]
            parts = field_key.split('_', 1)
            if len(parts) != 2:
                selected_fields[field_key] = {
                    'type': 'skip',
                    'samples': len(items),
                    'note': 'Invalid format'
                }
                continue
            
            table, column = parts
            
            # Skip PK/FK
            if self._is_skip_column(column):
                selected_fields[field_key] = {
                    'type': 'skip',
                    'samples': len(items),
                    'note': 'PK/FK detected'
                }
                continue
            
            # Check explicit config rule first
            explicit_rule = self._explicit_rule(table, column, field_key)
            # Для автодетекта используем ограниченный набор (sample_limit)
            transformer_type = explicit_rule or self._auto_select_transformer(column, table, items_for_classify)
            
            # Get sample stats (для заметок используем те же ограниченные данные)
            values = [i["value"] for i in items_for_classify if i.get("value")]
            has_at = any('@' in v for v in values)
            is_numeric = all(re.match(r'^[\d\+\-\(\)\.\s]+$', v) and len(re.sub(r'\D', '', v)) >= 7 for v in values[:5]) if values else False
            
            note = ""
            phone_pattern = r'^[\d\+\-\(\)\.\s]+$'
            if transformer_type == 'email' and has_at:
                note = f"{sum(1 for v in values if '@' in v)} emails"
            elif transformer_type == 'phone' and is_numeric:
                phone_count = sum(1 for v in values if re.match(phone_pattern, v))
                note = f"{phone_count} phone-like"
            elif transformer_type == 'genre':
                unique_count = len(set(values))
                note = f"{unique_count} unique → deterministic swap"
            
            selected_fields[field_key] = {
                'type': transformer_type,
                'samples': len(items),
                'note': note
            }
        
        return selected_fields
    
    def _auto_select_transformer(
        self, column: str, table: str, samples: list
    ) -> str:
        """Auto-select transformer type based on column name and value patterns.
        
        Order matters: named-pattern checks first (email, dates, phone, geo),
        THEN numeric check. This ensures postal codes and dates aren't skipped
        as "just numbers" when they belong to specific categories.
        """
        col_lower = column.lower()
        n_samples = len(samples)
        
        # ═══ PHASE A: Pattern-matched types (checked by VALUE content) ═══
        
        # 1. Email — '@' character
        has_at = any('@' in s.get("value", "") for s in samples)
        if has_at:
            return "email"
        
        # 2. Регистрационные и налоговые номера — не трогаем совсем. Менять их
        # можно только генератором с контрольной суммой (ИНН/КПП/ОГРН), а он был
        # нужен единственной базе с русскими реквизитами, которая из проекта убрана.
        business_id_patterns = ('inn', 'kpp', 'ogrn', 'passport', 'vat', 'tax_id')
        if any(p in col_lower for p in business_id_patterns):
            return "skip"
        
        # 3. Dates — YYYY-MM-DD / YYYY/MM/DD (with separators) OR YYYYMMDD (exactly 8 digits)
        # Strict mode prevents false positives on long numeric IDs like '3570236' (bytes)
        date_re_with_sep = re.compile(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}')
        date_re_exact = re.compile(r'^\d{8}$')  # Only pure 8-digit numbers (no separators)
        def _is_date(sample):
            val = sample.get("value", "")
            if date_re_with_sep.match(val):
                return True
            # For unseparated dates, require exactly 8 digits (not 7, not 9)
            if date_re_exact.match(val) and len(val) == 8:
                return True
            return False
        dates_found = sum(1 for s in samples if _is_date(s))
        if dates_found > n_samples * 0.7:
            has_time = any(':' in s.get("value", "") for s in samples)
            scope = "_month" if has_time else "_year"
            return f"date_shuffle{scope}"
        
        # 4. Phones — must have PHONE-SPECIFIC formatting signs
        # Acceptable patterns: +1 (...) | (+7) ... | 8-... | (495) ... | ...-..-..
        # REJECT: plain digit strings like '3570236' (could be bytes, ms, id, etc.)
        phone_format_patterns = [
            r'\+\d',           # +7, +1, +44...
            r'\(\d{2,4}\)',    # (425), (+7), (495)... 
            r'^8-\d',           # 8-916-...
            r'\d{2,4}[\- ]\d', # 495-123, 495 123...
            r'\d{4}-\d{2}',     # 1234-56 (Russian internal format)
        ]
        phone_count = sum(
            1 for s in samples
            if any(p.search(s.get("value", "")) for p in map(re.compile, phone_format_patterns))
            and len(re.sub(r'\D', '', s.get("value", ""))) >= 7
        )
        if phone_count > n_samples * 0.5:
            return "phone"
        
        # 5. Geo fields — city, state, country, postal code (SUBSTRING match)
        # Postal codes ARE numeric but must be genre-swapped, NOT skipped.
        geo_patterns = ('city', 'state', 'country', 'postalcode', 'postal_code', 'town')
        if any(p in col_lower for p in geo_patterns):
            return "genre"
        
        # ═══ PHASE B: Column-name-only types (value-insensitive) ═══
        
        # 6. Company (BEFORE name! — 'CompanyName' must not match on 'name')
        if 'company' in col_lower:
            return "company"
        
        # 7. Composer — musical composer names
        if 'composer' in col_lower:
            return "composer"
        
        # 8. Name — First/Last/Middle names
        # After 'company' check so CompanyName doesn't get misclassified
        if any(p in col_lower for p in ('name', 'firstname', 'lastname', 'first_name', 'last_name')):
            return "name"
        
        # 9. Title / Job title
        if col_lower == 'title':
            return "title"
        
        # 10. Address (incl. legal_address)
        if 'address' in col_lower or 'legal' in col_lower:
            return "address"
        
        # ═══ PHASE C: Enum & categorical types ═══
        
        # 11. Genre/media-type/category/type
        if any(p in col_lower for p in ('genre', 'media_type', 'playlist', 'type', 'category')):
            return "genre"
        
        # 12. Industry/sector/classification
        if 'industry' in col_lower or 'sector' in col_lower or 'classification' in col_lower:
            return "genre"
        
        # 13. Currency codes (USD, EUR, RUB — very few unique values)
        if 'currency' in col_lower or col_lower in ('code', 'iso_code'):
            return "genre"
        
        # 14. Статусы/этапы/условия оплаты — замкнутое перечисление, циклическая замена
        if any(p in col_lower for p in ('status', 'stage', 'terms', 'method', 'payment_type')):
            return "genre"
        
        # 15. Источник лида / маркетинговый канал — тоже замкнутое перечисление
        if any(p in col_lower for p in ('leadsource', 'lead_source', 'marketing', 'campaign', 'referral')):
            return "genre"
        
        # ═══ PHASE D: Skip decisions ═══
        
        # 16. Numeric columns — prices, quantities, bytes, milliseconds (LAST resort)
        # By this point, dates/phones/postal codes already handled above.
        all_numeric = all(
            re.match(r'^[\d.,\-+]+$', s.get("value", ""))
            for s in samples if s.get("value")
        ) if n_samples > 0 else False
        if all_numeric:
            return "skip"
        
        # 17. Free-text fields — too variable, skip per-row anonymization
        if any(p in col_lower for p in ('notes', 'description', 'comment', 'memo', 'biography')):
            return "skip"
        
        # 18. URL/Website — free-form URLs
        if 'website' in col_lower or 'url' in col_lower or 'domain' in col_lower:
            return "skip"
        
        # Default: cycle-safe genre swap
        return "genre"
        if any(p in col_lower for p in ('name', 'firstname', 'lastname', 'first_name', 'last_name')):
            return "name"
        
        # Composer specifically
        if 'composer' in col_lower:
            return "composer"
        
        # Title/job title
        if col_lower == 'title':
            return "title"
        
        # Company
        if 'company' in col_lower:
            return "company"
        
        # Industry/sector/classification — categorical enum
        if 'industry' in col_lower or 'sector' in col_lower or 'classification' in col_lower:
            return "genre"  # Cyclic swap
        
        # Currency/monetary codes — small enum set
        if 'currency' in col_lower or col_lower in ('code', 'iso_code'):
            return "genre"  # Cyclic swap (USD, EUR, RUB — only few unique values)
        
        # Источник лида / маркетинговый канал — замкнутое перечисление
        if any(p in col_lower for p in ('leadsource', 'lead_source', 'marketing', 'campaign', 'referral')):
            return "genre"  # Cyclic swap
        
        # Website/URL — contains domain which has emails
        if 'website' in col_lower or 'url' in col_lower or 'domain' in col_lower:
            return "skip"  # Free-text URL, hard to anonymize meaningfully
        
        # Notes/description/free-text — skip (too variable for per-row anonymization)
        if any(p in col_lower for p in ('notes', 'description', 'comment', 'memo', 'biography')):
            return "skip"
        
        # Genre/media-type/category/type
        if any(p in col_lower for p in ('genre', 'media_type', 'playlist', 'type', 'category')):
            return "genre"
        
        # Статусы/этапы/способы оплаты — замкнутое перечисление
        if any(p in col_lower for p in ('status', 'stage', 'terms', 'method', 'payment_type')):
            return "genre"  # Cyclic swap
        
        # Default fallback
        return "genre"
    
    # ── Phase 3: Stream transformation ─────────────────────────────────
    
    def _stream_transform(
        self, in_path: Path, out_path: Path, transformers: Dict[str, Any]
    ) -> int:
        """Stream through SQL dump, applying cached transformations."""
        row_count = 0
        transform_count = 0
        
        # Index transformers by table
        table_transformers: Dict[str, Dict[str, Any]] = {}
        for field_key, transformer in transformers.items():
            parts = field_key.split('_', 1)
            if len(parts) != 2:
                continue
            tbl, col = parts
            if tbl not in table_transformers:
                table_transformers[tbl] = {}
            table_transformers[tbl][col] = transformer
        
        current_table = None
        current_columns: list = []
        in_insert = False
        
        # Ввод/вывод в кодировке дампа (см. detect_sql_encoding): жёсткий utf-8
        # ронял мастер на cp1251-выгрузках, а чтение без encoding зависело от
        # локали контейнера. Выход — в той же кодировке, чтобы IMPORT не сломался.
        enc = self._dump_encoding
        with open(in_path, encoding=enc) as fin, \
                open(out_path, 'w', encoding=writer_encoding(enc)) as fout:
            for line in fin:
                stripped = line.strip()
                
                # Pass through comments, DDL, etc. unchanged
                if (not stripped or stripped.startswith('/*') or 
                    stripped.startswith('--') or stripped.startswith('SET ') or
                    stripped.startswith('USE ') or stripped.startswith('DROP ') or
                    stripped.startswith('CREATE DATABASE') or
                    stripped.startswith('LOCK TABLES') or
                    stripped.startswith('UNLOCK TABLES') or
                    stripped.startswith('START TRANSACTION') or
                    stripped.startswith('COMMIT') or stripped.startswith('BEGIN') or
                    stripped.startswith('SHOW ') or stripped.startswith('ALTER')):
                    fout.write(line)
                    continue
                
                # Track INSERT INTO ... (columns) VALUES
                insert_match = re.search(
                    r'INSERT\s+INTO\s+`(\w+)`\s+\(([^)]+)\)',
                    stripped, re.IGNORECASE
                )
                if insert_match:
                    current_table = insert_match.group(1)  # Table name from group(1)
                    current_columns = [re.sub(r'`', '', c.strip()) for c in insert_match.group(2).split(',')]
                    fout.write(line)
                    continue
                
                # If inside INSERT VALUES section, process this line
                if current_table and current_columns:
                    if stripped.startswith('('):
                        # ВАЖНО: передаём исходную строку целиком (включая перевод
                        # строки), а не stripped() — иначе теряется хвостовая
                        # запятая между кортежами и `\n`, и INSERT склеивается.
                        processed = self._process_value_line(
                            line, current_table, current_columns, table_transformers
                        )
                        if processed is not None:
                            row_count += 1
                            fout.write(processed)
                        else:
                            fout.write(line)

                        # Конец INSERT? (терминальный `);` в исходной строке)
                        if stripped.endswith(');'):
                            in_insert = False
                            current_table = None
                            current_columns = []
                        continue
                
                fout.write(line)
        
        return row_count
    
    def _process_value_line(
        self, line: str, table: str, columns: list,
        table_tx: Dict[str, Dict[str, Any]]
    ) -> Optional[str]:
        """Трансформация строки VALUES заменой значений ПО ПОЗИЦИЯМ.

        Ключевое отличие от прежней реализации: исходная строка служит
        шаблоном, в котором перезаписываются только токены значений.
        Разделители между кортежами (`,`), переводы строк, терминальный `;`,
        кавычки, префиксы `N'..'`, экранирование и отступы сохраняются
        дословно — пересобрать INSERT с потерей структуры, как делал старый
        код (`"(".join`, `sep = ",\\n" if len>1 else " "`, `+ ");"`), теперь
        невозможно в принципе.

        Возвращает None, если кортежей в строке нет (тогда строка пишется как есть).
        """
        spans = self._iter_tuple_spans(line)
        if not spans:
            return None

        pieces: List[str] = []
        pos = 0
        for start, end in spans:
            pieces.append(line[pos:start])          # текст между кортежами — как в оригинале
            pieces.append(self._process_tuple(
                line[start:end], table, columns, table_tx))
            pos = end
        pieces.append(line[pos:])                   # хвост: ',', ';' и перевод строки
        return "".join(pieces)

    # ── Разбор SQL-строки с учётом кавычек и экранирования ─────────────

    @staticmethod
    def _skip_quoted(s: str, i: int) -> int:
        """i указывает на открывающую кавычку. Вернуть индекс за закрывающей.

        Учтены оба способа экранирования: удвоение ('') и обратный слэш (\').
        """
        quote = s[i]
        n = len(s)
        i += 1
        while i < n:
            ch = s[i]
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and s[i + 1] == quote:
                    i += 2
                    continue
                return i + 1
            i += 1
        return n                                    # незакрытая кавычка

    @classmethod
    def _match_paren(cls, s: str, start: int) -> int:
        """Индекс за парной закрывающей скобкой для s[start] == '(' или -1."""
        depth = 0
        i = start
        n = len(s)
        while i < n:
            ch = s[i]
            if ch in ("'", '"', '`'):
                i = cls._skip_quoted(s, i)
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return -1

    @classmethod
    def _iter_tuple_spans(cls, line: str) -> List[Tuple[int, int]]:
        """Список (start, end) всех кортежей (…) в строке; end — исключительно.

        `)` внутри строкового значения (например 'Blues (Live)') кортеж не закрывает.
        """
        spans: List[Tuple[int, int]] = []
        i = 0
        n = len(line)
        while i < n:
            if line[i] != '(':
                i += 1
                continue
            end = cls._match_paren(line, i)
            if end == -1:
                break                               # незакрытый кортеж — остаток не трогаем
            spans.append((i, end))
            i = end
        return spans

    @classmethod
    def _split_top_level(cls, inner: str) -> List[Tuple[str, int, int]]:
        """Разбить содержимое кортежа на токены значений со спаннами.

        Возвращает [(текст_токена_включая_пробелы, start, end), ...] с индексами
        внутри `inner`; разделительные запятые в токены не входят.
        """
        parts: List[Tuple[str, int, int]] = []
        n = len(inner)
        depth = 0
        i = 0
        token_start = 0
        while i < n:
            ch = inner[i]
            if ch in ("'", '"', '`'):
                i = cls._skip_quoted(inner, i)
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                parts.append((inner[token_start:i], token_start, i))
                i += 1
                token_start = i
                continue
            i += 1
        parts.append((inner[token_start:n], token_start, n))
        return parts

    # ── Трансформация кортежа и отдельного значения ───────────────────

    def _process_tuple(self, tuple_text: str, table: str, columns: list,
                       table_tx: Dict[str, Dict[str, Any]]) -> str:
        """Перезаписать только токены значений: скобки и запятые остаются нетронутыми."""
        if not (tuple_text.startswith('(') and tuple_text.endswith(')')):
            return tuple_text
        inner = tuple_text[1:-1]
        parts = self._split_top_level(inner)
        if not parts:
            return tuple_text

        pieces: List[str] = []
        pos = 0
        for idx, (token, s, e) in enumerate(parts):
            pieces.append(inner[pos:s])
            pieces.append(self._transform_token(token, idx, table, columns, table_tx))
            pos = e
        pieces.append(inner[pos:])
        return "(" + "".join(pieces) + ")"

    def _transform_token(self, token: str, idx: int, table: str, columns: list,
                         table_tx: Dict[str, Dict[str, Any]]) -> str:
        """Замена одного SQL-токена значения с сохранением способа его записи.

        Гарантии формата:
        * `NULL` (в любом регистре) — не данные, остаётся `NULL` (без кавычек);
        * кавычки и префикс (`N'..'`, `'..'`, `".."`) сохраняются как в оригинале;
        * голый токен (число) меняется ТОЛЬКО на значение того же класса, иначе
          числовая колонка получила бы строку — это потеря типа;
        * нет замены (или трансформер не назначен) — токен возвращается нетронутым.
        """
        lead = token[:len(token) - len(token.lstrip())]
        trail = token[len(token.rstrip()):]
        body = token.strip()
        if not body:
            return token

        # SQL-литералы, не являющиеся данными.
        if body.upper() in ('NULL', 'DEFAULT'):
            return token

        if idx >= len(columns):
            return token
        column = columns[idx]
        if self._is_skip_column(column):
            return token

        # Разобрать представление значения.
        prefix = suffix = ''
        if len(body) >= 3 and body.startswith("N'") and body.endswith("'"):
            prefix, suffix, literal = "N'", "'", body[2:-1]
        elif len(body) >= 2 and body[0] == "'" and body[-1] == "'":
            prefix, suffix, literal = "'", "'", body[1:-1]
        elif len(body) >= 2 and body[0] == '"' and body[-1] == '"':
            prefix, suffix, literal = '"', '"', body[1:-1]
        else:
            literal = body                                  # голый литерал (число и т. п.)

        quoted = bool(prefix)
        data = self._unescape_sql(literal) if quoted else body

        tx_map = table_tx.get(table, {})
        transformer = tx_map.get(column)
        if transformer is None:
            return token

        replacement = None
        if getattr(transformer, '_mapping', None):
            replacement = transformer._mapping.get(data)
        if replacement is None and hasattr(transformer, 'transform'):
            try:
                replacement = transformer.transform(data, table=table, column=column)
            except Exception:
                replacement = None

        if replacement is None:
            return token
        replacement = str(replacement)
        if replacement == data:
            return token

        if quoted:
            return f"{lead}{prefix}{self._escape_sql(replacement, prefix)}{suffix}{trail}"

        if not self._same_literal_class(body, replacement):
            return token                                  # не ломать тип колонки
        return f"{lead}{replacement}{trail}"

    @staticmethod
    def _unescape_sql(text: str) -> str:
        """Тело SQL-литерала → исходное значение ('' и \\' → ', \\n → перевод строки)."""
        out: List[str] = []
        i = 0
        n = len(text)
        escapes = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0',
                   '\\': '\\', "'": "'", '"': '"', 'b': '\b', 'Z': '\x1a'}
        while i < n:
            ch = text[i]
            if ch == '\\' and i + 1 < n:
                out.append(escapes.get(text[i + 1], text[i + 1]))
                i += 2
                continue
            if ch == "'" and i + 1 < n and text[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _escape_sql(value: str, prefix: str) -> str:
        """Исходное значение → тело литерала в том же стиле кавычки.

        Удвоение кавычки валидно для MySQL независимо от sql_mode (в отличие от
        обратной косой черты при NO_BACKSLASH_ESCAPES).
        """
        if prefix.endswith("'"):
            return value.replace("'", "''")
        return value.replace('"', '""')

    _NUM_LITERAL = re.compile(r'^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$')
    _DATE_LITERAL = re.compile(r'^\d{4}-\d{2}-\d{2}')

    @classmethod
    def _same_literal_class(cls, original: str, replacement: str) -> bool:
        """Одного ли SQL-класса значения: число ↔ число той же разрядности."""
        if cls._NUM_LITERAL.match(original):
            return (bool(cls._NUM_LITERAL.match(replacement))
                    and len(replacement) == len(original))
        if cls._DATE_LITERAL.match(original):
            return bool(cls._DATE_LITERAL.match(replacement))
        return False

    @staticmethod
    def _safe_sql_val(raw: str) -> str:
        """Return raw SQL value cleanly stripped."""
        return raw.strip()
