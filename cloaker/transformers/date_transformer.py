"""Date shuffle transformer — replaces dates with random dates in same period."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


class DateShuffleTransformer(BaseTransformer):
    """Replace dates with random dates within same month or year range."""

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None:
            return value
        
        # Auto-detect shuffle scope from column name
        # _month = shuffle within same month+year
        # _year  = shuffle within same year
        # default = shuffle within ±30 days of original
        scope = self._detect_scope(column)
        
        try:
            dt = self._parse_date(str(value))
            if dt:
                shuffled = self._shuffle(dt, scope)
                return self._format_date(shuffled, dt)
        except (ValueError, TypeError):
            pass
        
        return value
    
    def type_name(self) -> str:
        return "date_shuffle"
    
    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        # No LLM needed for date shuffling - it's deterministic math
        pass
    
    @staticmethod
    def _detect_scope(column: str) -> str:
        """Detect shuffle granularity from column name."""
        col_lower = column.lower()
        if "_month" in col_lower or "hire" in col_lower:
            return "month"
        elif "_year" in col_lower or "birth" in col_lower:
            return "year"
        return "days"
    
    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """Parse various date formats."""
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%Y%m%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        return None
    
    @staticmethod
    def _shuffle(dt: datetime, scope: str) -> datetime:
        """Replace date with a random date in the same scope."""
        import random
        
        if scope == "month":
            # Keep year + month, change day
            new_day = random.randint(1, 28)
            try:
                return dt.replace(day=new_day)
            except ValueError:
                return dt.replace(day=28)
        
        elif scope == "year":
            # Keep only year, random month/day
            new_month = random.randint(1, 12)
            max_day = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][new_month - 1]
            new_day = random.randint(1, min(max_day, 28))
            try:
                return dt.replace(month=new_month, day=new_day)
            except ValueError:
                return dt.replace(month=new_month, day=28)
        
        else:  # days
            # Random offset ±30 days
            delta = timedelta(days=random.randint(-30, 30))
            return dt + delta
    
    @staticmethod
    def _format_date(shuffled: datetime, original: datetime) -> str:
        """Format the shuffled date to match the original format style."""
        orig_str = str(original)
        if ":" in orig_str and "." not in orig_str:
            return shuffled.strftime("%Y-%m-%d %H:%M:%S")
        elif "." in orig_str:
            return shuffled.strftime("%Y-%m-%d %H:%M:%S.%f")
        else:
            return shuffled.strftime("%Y-%m-%d")
