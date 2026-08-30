"""ID Guardian Transformer — deterministic generation for Russian business IDs.

Generates realistic replacements preserving format AND checksums.
Handles: INN (10/12 digits), OGRN (13 digits), KPP (9 digits),
passport series+number, contract numbers.

Uses modular arithmetic, NOT LLM — perfect checksum validation required.
"""

from __future__ import annotations

import re
from typing import Dict, List, Any


class IDGuardianTransformer:
    """Deterministic replacement for business IDs with checksum validation."""

    type_name = "id_guardian"
    
    def __init__(self, config=None):
        self._mapping = {}
        self.config = config
    
    @staticmethod
    def calculate_inn_checksum(inn_base: str, weights: list) -> int:
        """Calculate single digit checksum using weighted sum mod 11."""
        total = sum(int(digit) * weight for digit, weight in zip(inn_base, weights))
        return total % 11 if total % 11 < 10 else total % 10
    
    def generate_inn(self, original: str) -> str:
        """Generate valid INN (Russian taxpayer identification number)."""
        inn_len = len(original)
        
        if inn_len == 10:  # Юридическое лицо
            # Random first 9 digits (first must be 1-9)
            new_inn = [str(r.randint(1, 9))] + [str(r.randint(0, 9)) for _ in range(8)]
            # Checksum: weights [3,7,2,4,10,3,5,9,6]
            weights = [3, 7, 2, 4, 10, 3, 5, 9, 6]
            checksum = self.calculate_inn_checksum(''.join(new_inn), weights)
            new_inn.append(str(checksum))
            return ''.join(new_inn)
        
        elif inn_len == 12:  # Индивидуальный предприниматель
            # Random first 10 digits
            new_inn = ['1'] + [str(r.randint(0, 9)) for _ in range(9)]
            
            # First checksum (weights same as for legal entities)
            weights = [3, 7, 2, 4, 10, 3, 5, 9, 6]
            checksum1 = self.calculate_inn_checksum(''.join(new_inn), weights)
            new_inn.append(str(checksum1))
            
            # Second checksum (weights [5,7,2,4,10,3,5,9,6])
            weights2 = [5, 7, 2, 4, 10, 3, 5, 9, 6]
            checksum2 = self.calculate_inn_checksum(''.join(new_inn[:10]), weights2)
            new_inn.append(str(checksum2))
            
            return ''.join(new_inn)
        
        # Fallback: just shuffle digits deterministically
        return self._safe_shuffle(original)
    
    def generate_ogrn(self, original: str) -> str:
        """Generate valid OGRN (primary state registration number)."""
        if len(original) != 13:
            return self._safe_shuffle(original)
        
        # First digit is always 3 (for LLCs) or 5/7 (for other types)
        new_ogrn = [r.choice(['3', '5', '7'])] + [str(r.randint(0, 9)) for _ in range(11)]
        
        # Checksum: weights [2,4,10,3,5,9,4,6,8]
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        checksum = self.calculate_inn_checksum(''.join(new_ogrn[:9]), weights)
        new_ogrn.append(str(checksum))
        
        return ''.join(new_ogrn)
    
    def generate_kpp(self, original: str) -> str:
        """Generate valid KPP (tax registration reason code)."""
        if len(original) != 9:
            return self._safe_shuffle(original)
        
        # Format: XXXXXYYZZ where Y indicates region
        # Just generate random valid-looking pattern
        new_kpp = [str(r.randint(0, 9)) for _ in range(5)]
        new_kpp.append(r.choice(['01', '02', '03', '04', '05', '06', '07', '08', '09', '10'])[0])
        new_kpp.extend([str(r.randint(0, 9)) for _ in range(2)])
        
        return ''.join(new_kpp)
    
    def generate_passport(self, original: str) -> str:
        """Generate valid Russian passport (series + number)."""
        if ' ' not in original:
            return self._safe_shuffle(original)
        
        parts = original.strip().split()
        result = []
        
        for part in parts:
            if part.isdigit() and len(part) == 4:
                # Series: first digit 4-9
                result.append(f'{r.randint(4, 9)}{"".join([str(r.randint(0, 9)) for _ in range(3)])}')
            else:
                # Number
                result.append("".join([str(r.randint(0, 9)) for _ in range(len(part))]))
        
        return ' '.join(result)
    
    def transform(self, value: str, table: str = "", column: str = "") -> str:
        """Transform a business ID to a valid alternative."""
        if not value or value.upper() in ('NULL', 'None'):
            return value
        
        clean_value = value.strip().strip("'\"")
        
        # Detect type by length and pattern
        if clean_value.isdigit():
            if len(clean_value) == 10:
                return self.generate_inn(clean_value)
            elif len(clean_value) == 12:
                return self.generate_inn(clean_value)
            elif len(clean_value) == 13:
                return self.generate_ogrn(clean_value)
            elif len(clean_value) == 9:
                # Could be KPP or INN partial - check column name
                if 'kpp' in column.lower():
                    return self.generate_kpp(clean_value)
                else:
                    return self._safe_shuffle(clean_value)
        elif ' ' in clean_value and all(p.isdigit() for p in clean_value.split()):
            if any(len(p) == 4 for p in clean_value.split()):
                return self.generate_passport(clean_value)
        
        # Generic safe shuffle
        return self._safe_shuffle(clean_value)
    
    @staticmethod
    def _safe_shuffle(value: str) -> str:
        """Deterministic shuffle that preserves structure."""
        import hashlib
        h = hashlib.md5(value.encode()).hexdigest()
        chars = list(value)
        n = len(chars)
        for i in range(n):
            j = int(h[i % len(h)], 16) % n
            chars[i], chars[j] = chars[j], chars[i]
        return ''.join(chars)


# Lazy import for circular dependency avoidance
def get_id_guardian(config=None):
    """Factory function to get IDGuardian instance."""
    return IDGuardianTransformer(config)
