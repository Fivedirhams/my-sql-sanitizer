"""SQL stream processor — reads MySQL dump, applies transformations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Any, Optional, List

from cloaker.config import CloakDBConfig
from cloaker.cache import GlobalMappingRegistry
from cloaker.transformers import get_transformer


class SQLProcessor:
    """Stream processor for MySQL dumps with custom transformers.
    
    Architecture (3-phase flow):
      Phase 1: Scan dump → collect ALL unique values per table+column (no limit)
      Phase 2: Load ALL LLM mappings via chunked batch calls (sized by prompt budget)
      Phase 3: Stream through dump → O(1) dict lookup per cell
    
    Cross-table consistency: GlobalMappingRegistry ensures identical source values
    produce identical masked values across ALL tables/columns.
    """

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
        
        # Try loading existing global mappings from previous run
        mapping_file = str(out_path.parent / "global_mapping.json")
        if self.reg.load_from_file(mapping_file):
            print(f"  🔄 Loaded {len(self.reg._mapping)} pre-existing mappings from {mapping_file}")
        
        # Phase 1: Collect ALL unique samples (no limit)
        print("[1/4] Collecting all unique value samples...")
        samples = self._collect_samples(str(in_path))
        total_unique = sum(len(v) for v in samples.values())
        print(f"  Found {len(samples)} fields, {total_unique} total unique values")
        
        # Phase 2: Load ALL LLM mappings (chunked batch calls sized by prompt budget)
        print("[2/4] Loading transformation mappings via LLM...")
        transformers = self._load_all_mappings(samples)
        
        # Save global mappings for future runs
        try:
            self.reg.save_to_file(mapping_file)
            print(f"  💾 Saved {len(self.reg._mapping)} global mappings")
        except Exception as e:
            print(f"  ⚠️ Could not save mappings: {e}")
        
        # Phase 3: Stream through dump with active transformers
        print(f"[3/4] Streaming anonymized output to {out_path}...")
        row_count = self._stream_transform(in_path, out_path, transformers)
        
        print(f"\n✅ Done! Output: {out_path}")
        print(f"   Processed {row_count} rows")
        print(f"   Global replacements cached: {len(self.reg._mapping)}")
        
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
        
        with open(sql_path) as f:
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
    
    def _load_all_mappings(
        self, samples: Dict[str, list]
    ) -> Dict[str, Any]:
        """Load transformation mappings for all fields (batch LLM calls).
        
        Returns a dict of {field_key: transformer_instance} with loaded mappings.
        """
        transformers: Dict[str, Any] = {}
        loaded = 0
        skipped = 0
        
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
            explicit_rule = self.config.transform_rules.get(field_key)
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
                
                if loaded % 5 == 0 or loaded <= 3:
                    print(f"  [{loaded}] {field_key} ({transformer_type}): {len(transformer._mapping)} entries")
                
            except Exception as e:
                # str(e)[:200]: стандартное сообщение subprocess.TimeoutExpired
                # включает команду целиком — с заголовком Authorization и ключом.
                detail = " ".join(str(e).split())[:200]
                print(f"  [WARN] Failed to load {field_key}: {type(e).__name__}: {detail}")
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
        
        for field_key, items in samples.items():
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
            explicit_rule = self.config.transform_rules.get(field_key)
            transformer_type = explicit_rule or self._auto_select_transformer(column, table, items)
            
            # Get sample stats
            values = [i["value"] for i in items if i.get("value")]
            has_at = any('@' in v for v in values)
            is_numeric = all(re.match(r'^[\d\+\-\(\)\.\s]+$', v) and len(re.sub(r'\D', '', v)) >= 7 for v in values[:5]) if values else False
            
            note = ""
            phone_pattern = r'^[\d\+\-\(\)\.\s]+$'
            if transformer_type == 'email' and has_at:
                note = f"{sum(1 for v in values if '@' in v)} emails"
            elif transformer_type == 'phone' and is_numeric:
                phone_count = sum(1 for v in values if re.match(phone_pattern, v))
                note = f"{phone_count} phone-like"
            elif transformer_type in ('genre', 'crm_status'):
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
        
        # 2. Business IDs — deterministic checksum generation (INN/KPP/OGRN/passport)
        business_id_patterns = ('inn', 'kpp', 'ogrn', 'passport', 'contract', 'vat', 'tax_id', 'registration', 'document')
        if any(p in col_lower for p in business_id_patterns):
            return "id_guardian"
        
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
        
        # 14. CRM status/stage/terms/method/payment_type
        if any(p in col_lower for p in ('status', 'stage', 'terms', 'method', 'payment_type')):
            return "crm_status"
        
        # 15. Lead source / marketing channel
        if any(p in col_lower for p in ('leadsource', 'lead_source', 'marketing', 'campaign', 'referral')):
            return "crm_status"
        
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
        
        # LeadSource / marketing_source — business enum
        if any(p in col_lower for p in ('leadsource', 'lead_source', 'marketing', 'campaign', 'referral')):
            return "crm_status"  # Cyclic swap
        
        # Website/URL — contains domain which has emails
        if 'website' in col_lower or 'url' in col_lower or 'domain' in col_lower:
            return "skip"  # Free-text URL, hard to anonymize meaningfully
        
        # Notes/description/free-text — skip (too variable for per-row anonymization)
        if any(p in col_lower for p in ('notes', 'description', 'comment', 'memo', 'biography')):
            return "skip"
        
        # Genre/media-type/category/type
        if any(p in col_lower for p in ('genre', 'media_type', 'playlist', 'type', 'category')):
            return "genre"
        
        # CRM status/stage/terms/method — enum-like fields
        if any(p in col_lower for p in ('status', 'stage', 'terms', 'method', 'payment_type')):
            return "crm_status"  # Cyclic swap
        
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
        
        with open(in_path) as fin, open(out_path, 'w') as fout:
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
                        processed = self._process_value_line(
                            stripped, current_table, current_columns, table_transformers
                        )
                        if processed is not None:
                            row_count += 1
                            fout.write(processed)
                            
                            # End of INSERT? Line ends with ); or standalone );
                            if stripped.rstrip().endswith(');'):
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
        """Transform a single value line like "(1, 'Rock')," or "(2, 'Jazz');"."""
        ends_semi = line.rstrip().endswith(');')
        
        tuples = self._extract_value_tuples(line)
        if not tuples:
            return None
        
        new_tuples = []
        
        for tuple_str in tuples:
            vals = self._parse_values_tuple(tuple_str)
            new_vals = []
            
            for i, val in enumerate(vals):
                # Bounds check
                if i >= len(columns):
                    new_vals.append(self._safe_sql_val(val))
                    continue
                
                column = columns[i]
                
                # Skip PK/FK — preserve integrity  
                if self._is_skip_column(column):
                    new_vals.append(self._safe_sql_val(val))
                    continue
                
                # Look up transformer in nested tx_map
                tx_map = table_tx.get(table, {})
                transformer = tx_map.get(column)
                
                raw = val.strip()
                
                # Extract the actual data value from SQL representation
                if raw.startswith("N'"):
                    inner = raw[2:]  # strip N prefix
                elif raw.startswith("'"):
                    inner = raw[1:]
                else:
                    inner = raw
                
                # Strip surrounding quotes
                data = inner.strip(chr(39) + chr(34))
                # Unescape doubled quotes 
                data = data.replace("''", "'")
                
                replacement = None
                
                # Try mapping lookup first (LLM-based transformers)
                if transformer and transformer._mapping:
                    replacement = transformer._mapping.get(data)
                
                # Fallback: use transform() method for stateless transformers
                # (IDGuardian, Phone, Email without LLM cache, etc.)
                if replacement is None and transformer and hasattr(transformer, 'transform'):
                    try:
                        replacement = transformer.transform(data, table=table, column=column)
                    except Exception:
                        pass  # Transform failed — will preserve original
                
                if replacement is not None:
                    # Detect original quote style
                    prefix = "N'" if raw.startswith("N'") else "'" if raw.startswith("'") else ""
                    
                    if prefix:
                        safe = replacement.replace("'", "''")
                        new_vals.append(prefix + safe + "'")
                    else:
                        new_vals.append(replacement)
                    continue
                
                # No transformation — preserve original
                new_vals.append(self._safe_sql_val(val))
            
            new_tuples.append("(" + ",".join(new_vals) + ")")
        
        # Preserve indentation
        m = re.match(r"^(\s+)", line)
        indent = m.group(1) if m else ""
        
        # Join with proper separator
        sep = ",\n" if len(new_tuples) > 1 else " "
        result = sep.join(indent + t for t in new_tuples)
        
        if ends_semi:
            result += ");"
        
        return result
    
    @staticmethod
    def _safe_sql_val(raw: str) -> str:
        """Return raw SQL value cleanly stripped."""
        return raw.strip()
