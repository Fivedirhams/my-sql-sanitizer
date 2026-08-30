"""Phone transformer — deterministic format-preserving replacement, ZERO LLM calls.

Strategy: extract country code + digit groups → hash-derived substitution of last N digits.
Format (parens, dashes, spaces, country code) is preserved exactly.
Each unique input always produces the same output (idempotent).
No file/directory dependencies — fully stateless computation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class PhoneTransformer(BaseTransformer):
    """Generate deterministic replacement phone numbers. No LLM, no files needed."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # Local field-level cache for speed-up within same dump run
        self._field_cache: Dict[str, Dict[str, str]] = {}

    def type_name(self) -> str:
        return "phone"

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
            if val and len(re.sub(r'\D', '', val)) >= 4:
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
    def _gen_digit(seed_byte: int, orig_d: int, idx: int) -> int:
        """Generate a new digit guaranteed different from original.
        
        Uses multi-round hashing with rotation to avoid XOR-only collisions.
        Guaranteed to find a candidate within 3 attempts.
        """
        for attempt in range(5):
            mix = seed_byte ^ ((orig_d << 2) | (idx * 7))
            candidate = (mix + orig_d * 3 + attempt * 11) % 10
            if candidate != orig_d:
                return candidate
        # Fallback (should never reach here)
        return (orig_d + 3) % 10

    @staticmethod
    def _deterministic(phone: str) -> str:
        """Replace last 3-4 significant digits with hash-derived ones.
        Format (parens, dashes, spaces, country code) is preserved exactly.
        """
        raw = phone.strip()
        if not raw:
            return raw

        # ── Step 1: Extract ALL characters with position metadata ──
        chars = list(raw)
        
        # Find digit positions and collect actual digit values
        digit_positions = []
        for i, ch in enumerate(chars):
            if ch.isdigit():
                digit_positions.append(i)
        
        if len(digit_positions) < 4:
            # Too few digits, can't do meaningful masking
            return raw

        total_digits = len(digit_positions)

        # ── Step 2: Determine which digits are "protected" vs "maskable" ──
        # Protect first ~40% of digits as "structural" (CC + area + exchange)
        structural_count = max(2, total_digits // 3)
        structural_count = min(structural_count, max(5, total_digits - 3))
        
        maskable_start = structural_count
        maskable_count = total_digits - maskable_start
        
        if maskable_count < 3:
            maskable_start = max(0, total_digits - 3)
            maskable_count = 3

        # ── Step 3: Generate hash-seeded replacement digits ──
        seed_bytes = hashlib.sha256(raw.encode()).digest()

        # ── Step 4: Reconstruct with original formatting ──
        result = list(chars)  # start with exact copy (keeps all formatting)
        
        for i in range(maskable_count):
            pos = maskable_start + i
            orig_pos = digit_positions[pos]
            orig_d = int(chars[orig_pos])
            byte_idx = (i * 3) % len(seed_bytes)
            
            new_d = PhoneTransformer._gen_digit(seed_bytes[byte_idx], orig_d, i)
            result[orig_pos] = str(new_d)

        return ''.join(result)
