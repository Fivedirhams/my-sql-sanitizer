"""Email transformer — generates realistic emails preserving domain distribution."""

from __future__ import annotations

import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class EmailTransformer(BaseTransformer):
    """Generate realistic replacement emails maintaining domain patterns.
    
    Uses GlobalMappingRegistry for cross-table consistency.
    Falls back to deterministic generation (not hash) for unseen values.
    """

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value
        
        # Ensure mapping is loaded
        field_key = f"{table}_{column}"
        if not self._ensure_loaded(field_key):
            return value
        
        # Check direct mapping first (LLM-generated)
        if value in self._mapping:
            return self._mapping[value]
        
        # Check global cache (value mapped from another table/column)
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        cached = reg.get_replacement(value)
        if cached:
            return cached
        
        # Fallback: generate deterministic email preserving domain
        return self._fallback_email(value)
    
    def type_name(self) -> str:
        return "email"
    
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

        unique_values = list(set(s["value"] for s in samples if s.get("value") and "@" in s["value"]))
        
        # Extract domain stats
        domain_stats = {}
        for s in samples:
            val = s.get("value", "")
            if "@" in val:
                domain = val.split("@")[-1]
                domain_stats[domain] = domain_stats.get(domain, 0) + 1
        
        # Filter out values that already have global mappings
        unseen = [v for v in unique_values if not reg.get_replacement(v)]
        
        if unseen:
            result = client.generate_email_mapping(field_key, unseen, domain_stats)
            raw_map = result if isinstance(result, dict) else {}
            reg.merge_mappings(raw_map)
            self._mapping.update({str(k): str(v) for k, v in raw_map.items()})
        else:
            self._mapping = {v: reg.get_replacement(v) for v in unique_values}
    
    @staticmethod
    def _fallback_email(original_value: str) -> str:
        """Generate a realistic-looking email as fallback (preserves domain)."""
        import hashlib
        
        # Try to preserve original domain
        if "@" in original_value:
            name_part, domain = original_value.rsplit("@", 1)
        else:
            name_part = original_value.strip().strip("'\"").lower()
            domain = "example.com"
        
        # Generate random-ish name part
        h = hashlib.sha256(f"{original_value}".encode()).hexdigest()[:8]
        clean_words = re.findall(r'[a-z]+', name_part)
        
        if len(clean_words) >= 2:
            new_name = f"{clean_words[0][:4]}{clean_words[-1][:3]}{h[:3]}"
        elif clean_words:
            new_name = f"{clean_words[0][:8]}{h[:3]}"
        else:
            new_name = f"user{h[:5]}"
        
        return f"{new_name}@{domain}"
