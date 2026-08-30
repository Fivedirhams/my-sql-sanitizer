"""Address transformer — generates realistic address replacements."""

from __future__ import annotations

import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class AddressTransformer(BaseTransformer):
    """Replace addresses with realistic alternatives preserving structure.
    
    Uses GlobalMappingRegistry for cross-table consistency.
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
        
        return self._fallback_address(value)
    
    def type_name(self) -> str:
        return "address"
    
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
        
        # Filter out values that already have global mappings
        unseen = [v for v in unique_values if not reg.get_replacement(v)]
        
        if unseen:
            result = client.generate_address_mapping(field_key, unseen, stats)
            raw_map = result if isinstance(result, dict) else {}
            reg.merge_mappings(raw_map)
            self._mapping.update({str(k): str(v) for k, v in raw_map.items()})
        else:
            self._mapping = {v: reg.get_replacement(v) for v in unique_values}
    
    @staticmethod
    def _fallback_address(value: str) -> str:
        """Generate a plausible address replacement when no mapping available."""
        import hashlib
        h = hashlib.md5(f"{value}".encode()).hexdigest()[:4]
        numbers = ["101", "215", "347", "489", "562", "673", "721", "834", "905", "118"]
        streets = ["Main St", "Oak Ave", "Maple Dr", "Park Blvd", "Elm Rd"]
        
        num = numbers[hash(value) % len(numbers)]
        street = streets[hash(value + "street") % len(streets)]
        
        return f"{num} {street} {h}"
