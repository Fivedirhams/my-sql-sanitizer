"""Phone transformer — generates realistic phone numbers preserving format patterns."""

from __future__ import annotations

import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class PhoneTransformer(BaseTransformer):
    """Replace phone numbers with realistic alternatives keeping the same format.
    
    Uses GlobalMappingRegistry for cross-table consistency.
    """

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value
        
        # Ensure mapping is loaded
        field_key = f"{table}_{column}"
        if not self._ensure_loaded(field_key):
            return value
        
        # Try LLM-generated mapping first
        if value in self._mapping:
            return self._mapping[value]
        
        # Check global cache
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        cached = reg.get_replacement(value)
        if cached:
            return cached
        
        # Fallback: generate from format pattern (preserves structure)
        return self._format_based(value)
    
    def type_name(self) -> str:
        return "phone"
    
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
            result = client.generate_phone_mapping(field_key, unseen)
            raw_map = result if isinstance(result, dict) else {}
            reg.merge_mappings(raw_map)
            self._mapping.update({str(k): str(v) for k, v in raw_map.items()})
        else:
            self._mapping = {v: reg.get_replacement(v) for v in unique_values}
    
    @staticmethod
    def _format_based(phone: str) -> str:
        """Generate a replacement preserving the original format pattern."""
        # Detect country code pattern
        if phone.startswith("+1"):
            prefix = "+1"
            digits = re.sub(r'\D', '', phone)[2:]
        elif phone.startswith("+7") or phone.startswith("8"):
            prefix = "+7"
            digits = re.sub(r'\D', '', phone.lstrip('8').lstrip('+'))
        else:
            prefix = ""
            digits = re.sub(r'\D', '', phone)
        
        # Generate random digits maintaining length
        new_digits = "".join(str(hash(d + phone) % 10) for d in digits)
        
        # Pad if shorter than original
        while len(new_digits) < len(digits):
            new_digits += str(hash(new_digits) % 10)
        new_digits = new_digits[:len(digits)]
        
        return f"{prefix}{new_digits}"
