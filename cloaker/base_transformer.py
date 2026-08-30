"""Base transformer class — all anonymization transformers inherit from this."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Optional, List

from .config import CloakDBConfig


class BaseTransformer(ABC):
    """Abstract base for all field-type transformers."""

    def __init__(self, config: CloakDBConfig) -> None:
        self.config = config
        self._mapping: Dict[str, str] = {}
        self._loaded = False
        self.profiles_dir = Path(config.profiles_dir)
        self.stats: Dict[str, Any] = {}
        # Shuffle pool for cycling unknown values (set by subclasses)
        self._shuffle_pool: List[str] = []
    
    @abstractmethod
    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        """Transform a single cell value."""
        ...
    
    @abstractmethod
    def type_name(self) -> str:
        """Return transformer type name (used in config)."""
        ...
    
    # ── Profile loading helpers ────────────────────────────────────────
    
    def _load_samples(self, field_key: str) -> Optional[List[Dict[str, Any]]]:
        """Load sample data for a field from the profiled samples directory."""
        sample_file = self.profiles_dir / f"{field_key}_samples.json"
        
        if not sample_file.exists():
            return None
        
        with open(sample_file) as f:
            data = json.load(f)
        
        items = data.get("items", [])
        limit = self.config.processing.sample_limit
        return items[:limit]
    
    def _load_profile_stats(self, field_key: str) -> Optional[Dict[str, Any]]:
        """Load statistics from the classification profile."""
        profile_file = self.profiles_dir / "profiles.json"
        
        if not profile_file.exists():
            return None
        
        with open(profile_file) as f:
            profiles = json.load(f)
        
        stats = profiles.get(field_key, {})
        return stats if isinstance(stats, dict) else None

    def _ensure_loaded(self, field_key: str) -> bool:
        """Ensure mapping is loaded (lazy initialization)."""
        if self._loaded or not self.profiles_dir.exists():
            return True
        
        try:
            samples = self._load_samples(field_key)
            if samples is None or len(samples) == 0:
                self._loaded = True
                return False
            
            stats = self._load_profile_stats(field_key)
            
            self._load_mapping(samples, field_key, stats or {})
            self._loaded = True
            return True
        except Exception as e:
            print(f"[WARN] {self.type_name()} failed to load: {e}")
            self._loaded = True
            return False

    @abstractmethod
    def _load_mapping(self, samples: List[Dict[str, Any]], field_key: str, stats: Dict[str, Any]) -> None:
        """Load transformation mapping from samples via LLM or deterministic logic."""
        ...


# ── Simple value mapper (no LLM) ──────────────────────────────────────

class RandomValueMapper(BaseTransformer):
    """Pick replacement values from the same set (preserves distribution, hides real data)."""
    
    def __init__(self, config: CloakDBConfig, pool: Optional[List[str]] = None) -> None:
        super().__init__(config)
        self._pool = pool or []
    
    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None or not self._pool:
            return value
        if value in self._pool:
            import random
            candidate = random.choice(self._pool)
            while candidate == value and len(self._pool) > 1:
                candidate = random.choice(self._pool)
            return candidate
        return value
    
    def type_name(self) -> str:
        return "random_value"
    
    def _load_mapping(self, samples: List[Dict[str, Any]], field_key: str, stats: Dict[str, Any]) -> None:
        self._pool = [s["value"] for s in samples if s.get("value")]
