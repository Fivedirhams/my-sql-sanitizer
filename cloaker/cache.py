"""Global value cache — unified mapping registry across ALL tables.

Ensures identical source values ALWAYS produce identical masked values,
even across different tables and columns (cross-table consistency).

Architecture:
  - Global singleton registry: {original_value -> replacement}
  - Each transformer reads/writes to this shared cache
  - If a value already has a mapping, reuse it (no new LLM call needed)
  - For shuffle-type fields, uses circular rotation within observed set
"""

from __future__ import annotations

from typing import Dict, List, Optional


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
    
    def get_replacement(self, original: str) -> Optional[str]:
        """Check if there's already a replacement for this value globally."""
        return self._mapping.get(original)
    
    def set_mapping(self, original: str, replacement: str) -> None:
        """Register a mapping globally."""
        self._mapping[original] = replacement
    
    def merge_mappings(self, mappings: Dict[str, str]) -> None:
        """Merge multiple LLM results into the global cache.
        
        Already-present values are preserved (first-wins).
        New values are added.
        """
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
        """Save global mapping to JSON file for reuse across runs."""
        import json
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump({"mapping": self._mapping}, f, indent=2)
    
    def load_from_file(self, path: str) -> bool:
        """Load global mapping from JSON file (if exists)."""
        import json
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return False
        
        with open(p) as f:
            data = json.load(f)
        
        saved_mapping = data.get("mapping", {})
        for orig, repl in saved_mapping.items():
            if orig not in self._mapping:
                self._mapping[orig] = repl
        
        return True
