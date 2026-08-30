"""Name transformer — LLM-generated realistic name replacements."""

from __future__ import annotations

import json
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class NameTransformer(BaseTransformer):
    """Replace names with realistic alternatives via LLM batch call.
    
    Uses GlobalMappingRegistry for cross-table consistency:
    - If a name already has a replacement in global cache, reuse it
    - Only send unseen values to LLM
    """

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value
        
        # Ensure mapping is loaded
        field_key = f"{table}_{column}"
        if not self._ensure_loaded(field_key):
            return value  # Return original if loading failed
        
        # Check direct mapping first (LLM-generated)
        if value in self._mapping:
            return self._mapping[value]
        
        # Check global cache (value mapped from another table/column)
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        cached = reg.get_replacement(value)
        if cached:
            return cached
        
        # Fallback: shuffle within observed pool for this column
        if self._shuffle_pool and len(self._shuffle_pool) >= 2:
            try:
                idx = self._shuffle_pool.index(value)
                next_idx = (idx + 1) % len(self._shuffle_pool)
                return self._shuffle_pool[next_idx]
            except ValueError:
                pass
        
        return value  # Unknown value — keep original (safer than fake hashes)
    
    def type_name(self) -> str:
        return "name"
    
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

        # Extract unique values
        unique_values = list(set(s["value"] for s in samples if s.get("value")))
        
        # Register shuffle pool for cycling unknown values
        self._shuffle_pool = sorted(unique_values)
        reg.register_shuffle_pool(field_key, unique_values)
        
        # Filter out values that already have global mappings
        unseen = [v for v in unique_values if not reg.get_replacement(v)]
        
        desc = stats.get("description", "")
        
        if unseen:
            result = client.generate_name_mapping(field_key, unseen, desc, stats)
            raw_map = result if isinstance(result, dict) else {}
            # Merge into global cache (preserves existing mappings)
            reg.merge_mappings(raw_map)
            self._mapping.update({str(k): str(v) for k, v in raw_map.items()})
        else:
            # All values already mapped globally
            self._mapping = {v: reg.get_replacement(v) for v in unique_values}
