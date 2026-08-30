"""CRM Status Transformer — deterministic cyclic swap for enum/status fields.

Preserves exact case/format while cycling through available values.
Used for deal stages, contact statuses, payment states, etc.

Uses GlobalMappingRegistry for cross-table consistency where possible.
"""

from __future__ import annotations

from typing import Dict, List, Any

from cloaker.base_transformer import BaseTransformer


class CRMStatusTransformer(BaseTransformer):
    """Deterministic cyclic replacement for status/enum columns.
    
    Uses GlobalMappingRegistry for cross-table consistency.
    Falls back to preserving original instead of fake hashes.
    """
    
    type_name = "crm_status"

    def transform(self, value: str, table: str = "", column: str = "") -> str:
        if not self._mapping or value not in self._mapping:
            # Check global cache first
            from cloaker.cache import GlobalMappingRegistry
            reg = GlobalMappingRegistry.instance()
            cached = reg.get_replacement(value)
            if cached:
                return cached
            
            # Fallback: preserve original rather than creating fake hashes
            return value
        
        return self._mapping[value]

    def _load_mapping(
        self, 
        samples: List[Dict[str, Any]], 
        field_key: str, 
        stats: Dict[str, Any]
    ) -> None:
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        
        vals = [s["value"] for s in samples if s.get("value")]
        unique_vals = list(dict.fromkeys(vals))  # Preserve order, dedupe
        
        if len(unique_vals) < 2:
            return
        
        # Register shuffle pool
        reg.register_shuffle_pool(field_key, unique_vals)
        
        # Filter out already-mapped values
        unseen = [v for v in unique_vals if not reg.get_replacement(v)]
        
        if len(unseen) >= 2:
            # Cycle through unseen values
            mapping = {}
            n = len(unseen)
            for i, original in enumerate(unseen):
                replacement = unseen[(i + 1) % n]
                mapping[original] = replacement
                reg.set_mapping(original, replacement)
            
            self._mapping.update(mapping)
        else:
            # All values already mapped globally - use those
            for v in unique_vals:
                if reg.get_replacement(v):
                    self._mapping[v] = reg.get_replacement(v)
        
        self.stats.update({
            "type": "deterministic",
            "unique_values_processed": len(unique_vals),
            "method": "cyclic_swap"
        })
