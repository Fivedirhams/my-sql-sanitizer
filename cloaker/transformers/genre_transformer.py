"""Genre/MediaType/Playlist transformer — random value swapping (no LLM needed)."""

from __future__ import annotations

import random
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class GenreTransformer(BaseTransformer):
    """Swap values within the same set (preserves distribution, hides real data).
    
    Uses GlobalMappingRegistry for cross-table consistency.
    Cyclic permutation ensures all observed values are transformed.
    """

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value
        
        # Ensure mapping is loaded
        field_key = f"{table}_{column}"
        if not self._ensure_loaded(field_key):
            return value
        
        # Check direct mapping first (cyclic swap)
        if value in self._mapping:
            return self._mapping[value]
        
        # Check global cache (value swapped from another table/column)
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        cached = reg.get_replacement(value)
        if cached:
            return cached
        
        # Last resort: random swap from pool or keep original
        if hasattr(self, '_shuffle_pool') and self._shuffle_pool and len(self._shuffle_pool) >= 2:
            try:
                idx = self._shuffle_pool.index(value)
                next_idx = (idx + 1) % len(self._shuffle_pool)
                return self._shuffle_pool[next_idx]
            except ValueError:
                pass
        
        return value  # Preserve original
    
    def type_name(self) -> str:
        return "genre"
    
    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        
        unique_values = list(dict.fromkeys(s["value"] for s in samples if s.get("value")))
        
        if len(unique_values) < 2:
            return
        
        # Register shuffle pool
        reg.register_shuffle_pool(field_key, unique_values)
        
        # Filter out already-mapped values
        unseen = [v for v in unique_values if not reg.get_replacement(v)]
        
        # Create cyclic permutation for unseen values
        for i, val in enumerate(unseen):
            next_val = unseen[(i + 1) % len(unseen)]
            self._mapping[val] = next_val
            reg.set_mapping(val, next_val)
        
        # Add globally mapped values
        for v in unique_values:
            if v not in self._mapping and reg.get_replacement(v):
                self._mapping[v] = reg.get_replacement(v)
