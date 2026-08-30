"""Composer transformer — person name replacement (similar to NameTransformer)."""

from __future__ import annotations

import hashlib
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class ComposerTransformer(BaseTransformer):
    """Replace composer names with realistic alternatives.
    
    Uses GlobalMappingRegistry for cross-table consistency.
    Falls back to shuffle pool instead of fake hashes.
    """

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value
        
        # Ensure mapping is loaded
        field_key = f"{table}_{column}"
        if not self._ensure_loaded(field_key):
            return value
        
        if value in self._mapping:
            return self._mapping[value]
        
        # Check global cache
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        cached = reg.get_replacement(value)
        if cached:
            return cached
        
        # Fallback: cycle within observed composers
        if hasattr(self, '_shuffle_pool') and self._shuffle_pool and len(self._shuffle_pool) >= 2:
            try:
                idx = self._shuffle_pool.index(value)
                next_idx = (idx + 1) % len(self._shuffle_pool)
                return self._shuffle_pool[next_idx]
            except ValueError:
                pass
        
        return value  # Preserve original
    
    def type_name(self) -> str:
        return "composer"
    
    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        from cloaker.llm_client import LLMClient
        from cloaker.cache import GlobalMappingRegistry
        client = LLMClient(self.config)
        reg = GlobalMappingRegistry.instance()

        unique_values = list(set(s["value"] for s in samples if s.get("value")))
        
        # Register shuffle pool
        self._shuffle_pool = sorted(unique_values)
        reg.register_shuffle_pool(field_key, unique_values)
        
        # Filter out values that already have global mappings
        unseen = [v for v in unique_values if not reg.get_replacement(v)]
        
        if unseen:
            result = client.generate_name_mapping(
                field_key, unseen, 
                "Music composer/person who composed the track", 
                stats
            )
            raw_map = result if isinstance(result, dict) else {}
            reg.merge_mappings(raw_map)
            self._mapping.update({str(k): str(v) for k, v in raw_map.items()})
        else:
            self._mapping = {v: reg.get_replacement(v) for v in unique_values}
