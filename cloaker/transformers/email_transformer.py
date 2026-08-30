"""Email transformer — deterministic generation, ZERO LLM calls.

Strategy: hash-based realistic name part, preserve domain exactly.
Each unique input always produces the same output (idempotent).
Cross-table consistency via GlobalMappingRegistry (merged during transform).
No file/directory dependencies — fully stateless computation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


# ── Deterministic "word" fragments (realistic-looking parts) ──────────

_CONSONANTS = "bcdfghjklmnprstvwxz"
_VOWELS     = "aeiou"


class EmailTransformer(BaseTransformer):
    """Generate deterministic replacement emails. No LLM, no files needed."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # Local field-level cache for speed-up within same dump run
        self._field_cache: Dict[str, Dict[str, str]] = {}

    def type_name(self) -> str:
        return "email"

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
            val = s.get("value", "") or ""
            if "@" not in val:
                continue
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
    def _deterministic(email: str) -> str:
        """Hash-based: keep domain intact, replace name part with realistic token."""
        if "@" not in email:
            return f"user{hashlib.sha256(email.encode()).hexdigest()[:8]}@example.com"

        name_part, domain = email.rsplit("@", 1)
        domain = domain.strip().lower()

        # Build a short realistic name from hash
        h = hashlib.sha256(email.encode()).hexdigest()

        name_clean = re.sub(r'[^a-zA-Z0-9]', '', name_part).lower()

        # Create 2-3 syllable name like real people
        syllables = []
        offset = int(h[:2], 16)

        for i in range(3):
            ci = (offset + i * 7) % len(_CONSONANTS)
            vi = (offset + i * 13 + 3) % len(_VOWELS)
            syllables.append(_CONSONANTS[ci] + _VOWELS[vi])

        new_name = "".join(syllables)

        # Mix in original word fragment for recognisability
        if name_clean:
            prefix_len = min(2, len(name_clean))
            new_name = name_clean[:prefix_len] + new_name[2:]

        return f"{new_name}@{domain}"
