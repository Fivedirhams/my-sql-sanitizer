"""Postal code transformer — format-preserving replacements."""

from __future__ import annotations

import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class PostalCodeTransformer(BaseTransformer):
    """Replace postal codes preserving their format/length.
    
    Uses GlobalMappingRegistry for cross-table consistency.
    Falls back to format-preserving generation for unseen values.
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
        
        # Fallback: preserve length with deterministic pseudo-random digits
        return self._format_fallback(value)
    
    def type_name(self) -> str:
        return "postal_code"
    
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
            result = client.generate_postal_code_mapping(field_key, unseen)
            raw_map = result if isinstance(result, dict) else {}
            reg.merge_mappings(raw_map)
            self._mapping.update({str(k): str(v) for k, v in raw_map.items()})
        else:
            self._mapping = {v: reg.get_replacement(v) for v in unique_values}
    
    @staticmethod
    def _format_fallback(code: str) -> str:
        """Generate a replacement preserving length and character pattern."""
        result = []
        for ch in code:
            if ch.isdigit():
                result.append(str(hash(ch + code) % 10))
            elif ch.isalpha():
                result.append(chr(ord('A') + hash(ch + code) % 26))
            else:
                result.append(ch)  # Keep separators (space, dash)
        return "".join(result)
