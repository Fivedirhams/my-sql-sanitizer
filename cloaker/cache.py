"""Global value cache — unified mapping registry across ALL tables.

Ensures identical source values ALWAYS produce identical masked values,
even across different tables and columns (cross-table consistency).

Два класса полей (и два пространства имён):
  * персонафицированные (имя, телефон, почта, адрес, компания) — общие значение
    и замена во всём дампе: один и тот же человек обязан иметь одну и ту же
    подставу в contacts и в orders. Они используют ГЛОБАЛЬНУЮ карту (`scope=None`).
  * замкнутые категориальные (жанр, статус, город) — словарь допустимых значений
    принадлежит КОНКРЕТНОЙ колонке. Раньше они писали в ту же глобальную карту,
    и циклический сдвиг одного поля отравлял другие: 'Москва'→'Хит' из жанров
    приезжало подменять город в адресе, 'Новый'→'Видео' — подменять статус ENUM
    (аудит N7). Теперь такие поля работают в своей области видимости (`scope`).

Architecture:
  - Global registry: {original_value -> replacement}   (identity fields)
  - Scoped registries: {scope: {original_value -> replacement}}  (categorical)
  - Each transformer reads/writes the registry it belongs to
  - If a value already has a mapping, reuse it (no new LLM call needed)
  - For shuffle-type fields, uses circular rotation within observed set
"""

from __future__ import annotations

from typing import Dict, List, Optional


def column_scope(column: str) -> Optional[str]:
    """Ключ области видимости замкнутого категориального поля — по имени колонки.

    Имя колонки, а не `table.column`: одно и то же слово словаря (`status` в двух
    таблицах) обязано получать одну замену во всём дампе — это согласованность,
    ради которой реестр и существует. А вот в чужой словарь (`genre`) поле не
    заглядывает.
    """
    col = (column or "").strip().strip('`').lower()
    return col or None


def field_scope(field_key: str) -> Optional[str]:
    """Область из field_key вида `table_column` (часть после первого `_`).

    Выводится тем же способом, каким main.py индексирует трансформеры
    (`field_key.split('_', 1)`), — иначе запись в Phase 2 и чтение в Phase 3
    оказались бы в разных областях.
    """
    if not field_key:
        return None
    if '_' not in field_key:
        return column_scope(field_key)
    return column_scope(field_key.split('_', 1)[1])


class GlobalMappingRegistry:
    """Singleton-like global mapping storage.
    
    All transformers share this registry. When generating replacements,
    first check if the original value already exists in the registry.
    If so, reuse it — this guarantees cross-table consistency without
    redundant LLM calls.
    """
    
    _instance: Optional['GlobalMappingRegistry'] = None
    
    def __init__(self) -> None:
        # Global: {original_value -> replacement}
        self._mapping: Dict[str, str] = {}
        # Scoped: {scope: {original_value -> replacement}} — изолированные
        # словари замкнутых категориальных полей (genre и т. п.)
        self._scoped: Dict[str, Dict[str, str]] = {}
        # Per-column shuffles: {field_key -> [values]} for deterministic cycling
        self._shuffles: Dict[str, List[str]] = {}
    
    @classmethod
    def instance(cls) -> 'GlobalMappingRegistry':
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing/multiple runs)."""
        cls._instance = None
    
    def get_replacement(self, original: str, scope: Optional[str] = None) -> Optional[str]:
        """Already-known replacement for this value.

        scope=None — глобальное пространство (персонафицированные поля).
        scope задан — ТОЛЬКО своя область: к глобальной карте обращения нет, и
        наоборот. Именно это запрещает значениям из словаря одной колонки
        просачиваться в другую (аудит N7).
        """
        if scope is not None:
            return self._scoped.get(scope, {}).get(original)
        return self._mapping.get(original)

    def set_mapping(self, original: str, replacement: str,
                    scope: Optional[str] = None) -> None:
        """Register a mapping (globally, or inside one column's domain)."""
        if scope is not None:
            self._scoped.setdefault(scope, {})[original] = replacement
        else:
            self._mapping[original] = replacement

    def merge_mappings(self, mappings: Dict[str, str],
                       scope: Optional[str] = None) -> None:
        """Merge multiple LLM results into the cache (global or one scope).

        Already-present values are preserved (first-wins).
        New values are added.
        """
        if scope is not None:
            bucket = self._scoped.setdefault(scope, {})
            for orig, repl in mappings.items():
                if orig not in bucket:
                    bucket[orig] = repl
            return
        for orig, repl in mappings.items():
            if orig not in self._mapping:
                self._mapping[orig] = repl
    
    def register_shuffle_pool(self, field_key: str, values: List[str]) -> None:
        """Register a pool of values for shuffling (deterministic cycle)."""
        unique_values = list(dict.fromkeys(values))  # Preserve order, dedupe
        self._shuffles[field_key] = unique_values
    
    def get_next_shuffled(self, field_key: str, value: str) -> str:
        """Get the next shuffled value for this field+value combo."""
        pool = self._shuffles.get(field_key)
        if not pool or len(pool) < 2:
            return value  # Can't shuffle with <2 values
        
        # Find index and rotate to next
        try:
            idx = pool.index(value)
            next_idx = (idx + 1) % len(pool)
            return pool[next_idx]
        except ValueError:
            return value  # Value not in pool, keep original
    
    def save_to_file(self, path: str) -> None:
        """Save mappings to JSON file for reuse across runs.

        Формат: {"mapping": {...}, "scoped": {scope: {...}}}. Ключ "mapping"
        сохранён как раньше — файлы прошлых прогонов читаются без миграции.
        ensure_ascii=False + utf-8: значения кириллические, и в ascii-экранированном
        виде файл становится нечитаемым для ручной правки/сверки.
        """
        import json
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({"mapping": self._mapping, "scoped": self._scoped},
                      f, indent=2, ensure_ascii=False)

    def load_from_file(self, path: str) -> bool:
        """Load mappings from JSON file (if exists). Supports old format."""
        import json
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return False

        with open(p, encoding='utf-8') as f:
            data = json.load(f)

        saved_mapping = data.get("mapping", {})
        for orig, repl in saved_mapping.items():
            if orig not in self._mapping:
                self._mapping[orig] = repl

        for scope, pairs in (data.get("scoped") or {}).items():
            bucket = self._scoped.setdefault(scope, {})
            for orig, repl in (pairs or {}).items():
                if orig not in bucket:
                    bucket[orig] = repl

        return True

    # ── Диагностика ────────────────────────────────────────────────────

    @property
    def total_entries(self) -> int:
        """Все известные замены: глобальные + по областям (для отчётов)."""
        return len(self._mapping) + sum(len(v) for v in self._scoped.values())

    def value_exists_anywhere(self, original: str) -> bool:
        """Есть ли у значения уже замена (в глобальной карте или в любой области).

        Нужно только для отчётности: области видимости намеренно не позволяют
        читать чужие замены.
        """
        if original in self._mapping:
            return True
        return any(original in bucket for bucket in self._scoped.values())
