"""Postal code transformer — deterministic format-preserving replacement, ZERO LLM calls.

Strategy: hash-derived substitution of digits and letters, preserving separators.
Format (dashes, spaces, parentheses) is kept exactly intact.
Each unique input always produces the same output (idempotent).
No file/directory dependencies — fully stateless computation.
Cross-table consistency via GlobalMappingRegistry.

Supported formats:
  - US ZIP:        98004, 98004-1234
  - Russian:       123456, 123-456
  - UK:            SW1A 1AA, EC1A 1BB
  - Canadian:      K1A 0B1
  - Any mixed:     (xxx) xxx-xxxx where x=digit or letter
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class PostalCodeTransformer(BaseTransformer):
    """Generate deterministic replacement postal codes. No LLM needed."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # Local field-level cache for speed-up within same dump run
        self._field_cache: Dict[str, Dict[str, str]] = {}

    def type_name(self) -> str:
        return "postal_code"

    # ── Core transform ────────────────────────────────────────────────

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value

        field_key = f"{table}_{column}"
        
        # Check local cache first (populated by _load_mapping or previous transforms)
        if field_key in self._field_cache and value in self._field_cache[field_key]:
            return self._field_cache[field_key][value]

        # Compute deterministic replacement
        result = self._deterministic(value)
        
        # Store in local cache
        if field_key not in self._field_cache:
            self._field_cache[field_key] = {}
        self._field_cache[field_key][value] = result

        # Merge into global registry for cross-table consistency
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        reg.set_mapping(value, result)

        return result

    # ── Mapping pre-computation (for bulk profiling speedup) ───────────

    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        """Pre-compute mappings for known samples. Called once during profile phase."""
        seen_values: set[str] = set()

        for s in samples:
            val = (s.get("value") or "").strip()
            if val and len(val) >= 3:  # Postal codes are at least 3 chars
                seen_values.add(val)

        # Pre-compute ALL mappings deterministically
        if field_key not in self._field_cache:
            self._field_cache[field_key] = {}

        for val in seen_values:
            self._field_cache[field_key][val] = self._deterministic(val)

        # Push to global registry for cross-table consistency
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        reg.merge_mappings(self._field_cache[field_key])

    # ── Deterministic generation ──────────────────────────────────────

    @staticmethod
    def _replace_char(ch: str, seed_byte: int, idx: int) -> str:
        """Replace a single alphanumeric character with a new one, preserving case/pattern.
        
        Digits → different digit (guaranteed != original)
        Letters → different letter in same case (guaranteed != original)
        Non-alphanumeric → returned unchanged (handled externally)
        """
        if ch.isdigit():
            # Generate new digit guaranteed different from original
            orig_d = int(ch)
            for attempt in range(5):
                mix = seed_byte ^ ((orig_d << 2) | (idx * 7))
                candidate = (mix + orig_d * 3 + attempt * 11) % 10
                if candidate != orig_d:
                    return str(candidate)
            return str((orig_d + 3) % 10)  # Fallback
        
        elif ch.isalpha():
            # Preserve case: uppercase stays uppercase, lowercase stays lowercase
            base = ord('A') if ch.isupper() else ord('a')
            orig_idx = ord(ch.lower()) - ord('a')
            
            for attempt in range(5):
                mix = seed_byte ^ ((orig_idx << 2) | (idx * 13))
                candidate = (mix + orig_idx * 5 + attempt * 17) % 26
                if candidate != orig_idx:
                    return chr(base + candidate)
            # Fallback: next letter wrapped
            return chr(base + (orig_idx + 3) % 26)
        
        return ch

    @staticmethod
    def _deterministic(code: str) -> str:
        """Replace all alphanumeric characters with hash-derived ones.
        Separators (space, dash, parens) preserved exactly.
        Length identical to input.
        """
        raw = code.strip()
        if not raw:
            return raw

        # Validate it looks like a postal code (at least 3 alnum chars)
        alnum_count = sum(1 for c in raw if c.isalnum())
        if alnum_count < 3:
            return raw

        chars = list(raw)
        seed_bytes = hashlib.sha256(raw.encode()).digest()
        
        # Collect positions of all alphanumeric characters
        alnum_positions = [i for i, c in enumerate(chars) if c.isalnum()]
        
        # Replace each alphanumeric char with hash-derived alternative
        result = list(chars)
        for pos_offset, orig_pos in enumerate(alnum_positions):
            byte_idx = (pos_offset * 3) % len(seed_bytes)
            result[orig_pos] = PostalCodeTransformer._replace_char(
                chars[orig_pos], 
                seed_bytes[byte_idx], 
                pos_offset
            )

        return ''.join(result)
