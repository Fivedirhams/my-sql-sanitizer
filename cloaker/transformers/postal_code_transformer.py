"""Postal code transformer — детерминированная подмена с полным сохранением формата.

Стратегия: подмена цифр и буквенных прогонов по затравке из sha256 ЗНАЧЕНИЯ.
Формат сохраняется строго: длина, порядок разрядов, ведущие нули, все
разделители (пробел, дефис, точка) и алфавит каждого символа.
Алфавит выбирается по классу ИСХОДНОГО символа: ASCII-буква остаётся
латиницей того же регистра, кириллическая буква остаётся кириллицей того
же регистра (Никакой «латыни» из кириллицы — N9 из `output/format_audit.md`).

Один и тот же вход всегда даёт один и тот же выход: функция детерминирована
по значению и не зависит от PYTHONHASHSEED, поэтому прогоны воспроизводимы.

Поддерживаемые форматы:
  - US ZIP:        98004, 98004-1234
  - Russian:       123456, 123-456, 012345 (ведущий ноль сохранён)
  - UK:            SW1A 1AA, EC1A 1BB
  - Canadian:      K1A 0B1
  - RU с текстом:  МУР 1234, Киров 610000 (кириллица → кириллица)
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Optional, Tuple

from cloaker.base_transformer import BaseTransformer


# ── Алфавиты и пулы для подмены ──────────────────────────────────────
# Хелперы сознательно дублируются в email/address-трансформерах: общий модуль
# потребовал бы правки base_transformer.py (вне зоны этой задачи).

# Латиница (ASCII) — 26 букв в алфавитном порядке
_LATIN = "abcdefghijklmnopqrstuvwxyz"
# Русская азбука целиком, включая Ё: 33 строчные буквы
_CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"

_DIGITS = "0123456789"

# Согласные/гласные: буквенный прогон заменяется «псевдословом» с их
# чередованием, чтобы подмена читалась значением, а не шумом («ЦЩК»).
_CONSONANTS_RU = "бвгдзжклмнпрстфхцчшщ"
_VOWELS_RU = "аеиоуыэюя"
_CONSONANTS_EN = "bcdfghjklmnprstvwxz"
_VOWELS_EN = "aeiou"


def _is_ascii_letter(ch: str) -> bool:
    """Латинская ли это буква (по классу ИСХОДНОГО символа)."""
    return ch.isascii() and ch.isalpha()


def _is_cyrillic_letter(ch: str) -> bool:
    """Кириллическая ли это буква (не-ASCII буква)."""
    return ch.isalpha() and not ch.isascii()


def _letter_kind(ch: str) -> Optional[str]:
    """Алфавит буквы: ``'ru'`` | ``'en'`` | ``None`` (не буква)."""
    if not ch.isalpha():
        return None
    return "en" if ch.isascii() else "ru"


def _letter_runs(text: str) -> List[Tuple[int, int, str]]:
    """Максимальные прогоны букв одного алфавита: [(start, end, kind)]."""
    runs: List[Tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        kind = _letter_kind(text[i])
        if kind is None:
            i += 1
            continue
        j = i
        while j < n and _letter_kind(text[j]) == kind:
            j += 1
        runs.append((i, j, kind))
        i = j
    return runs


def _different_digit(ch: str, rng: random.Random) -> str:
    """Цифра, гарантированно не равная исходной (класс «цифра» сохранён)."""
    if not (ch.isdigit() and ch.isascii()):
        # не-ASCII цифры (арабо-индийские и т. п.) не «угадываем» — оставляем
        return ch
    for _attempt in range(10):
        candidate = rng.choice(_DIGITS)
        if candidate != ch:
            return candidate
    return str((int(ch) + 1) % 10)


def _different_letter(ch: str, rng: random.Random) -> str:
    """Буква того же алфавита и регистра, не равная исходной (запасной ход)."""
    if _is_ascii_letter(ch):
        alphabet = _LATIN
    elif _is_cyrillic_letter(ch):
        alphabet = _CYRILLIC
    else:
        return ch
    lowered = ch.lower()
    if lowered not in alphabet:
        return ch
    for _attempt in range(20):
        candidate = rng.choice(alphabet)
        if candidate != lowered:
            return candidate.upper() if ch.isupper() else candidate
    idx = alphabet.index(lowered)
    return alphabet[(idx + 1) % len(alphabet)].upper() if ch.isupper() \
        else alphabet[(idx + 1) % len(alphabet)]


def _syllables(text: str, kind: str) -> List[str]:
    """Рисунок «гласная/согласная» по позициям исходного написания."""
    vowels = _VOWELS_RU if kind == "ru" else _VOWELS_EN
    return ["v" if ch.lower() in vowels else "c" for ch in text]


def _pseudo_word(text: str, kind: str, rng: random.Random) -> str:
    """Псевдослово той же длины, алфавита, регистра и слогового рисунка.

    Применяется, когда в пуле нет названия той же маски. Чередование
    согласная/гласная берётся ИЗ исходного слова: «Москва» даёт «Тарлут»,
    а не «Ляцщэя» — формат соблюдён в обоих случаях, но шум выглядит
    опечаткой (претензия N9 к читаемости подмены).
    """
    cons, vows = (_CONSONANTS_RU, _VOWELS_RU) if kind == "ru" else (_CONSONANTS_EN, _VOWELS_EN)
    pattern = _syllables(text, kind)
    if pattern and "v" not in pattern:
        # Сплошь согласные («РД», «Цщк») читаются только с гласной внутри
        pattern = list(pattern)
        pattern[min(1, len(pattern) - 1)] = "v"
    for _attempt in range(20):
        chars = []
        for ch, slot in zip(text, pattern):
            base = rng.choice(cons if slot == "c" else vows)
            chars.append(base.upper() if ch.isupper() else base.lower())
        candidate = "".join(chars)
        if candidate != text:
            return candidate
    # Вероятность пренебрежимо мала, но схлопывание в оригинал недопустимо
    return "".join(_different_letter(ch, rng) for ch in text)


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
        if value is None or not isinstance(value, str):
            return value

        field_key = f"{table}_{column}"

        # Проверка локального кэша (заполняется _load_mapping или прошлыми вызовами)
        if field_key in self._field_cache and value in self._field_cache[field_key]:
            return self._field_cache[field_key][value]

        result = self._deterministic(value)

        # Кэшируем результат, в том числе честный passthrough
        if field_key not in self._field_cache:
            self._field_cache[field_key] = {}
        self._field_cache[field_key][value] = result

        # В глобальный реестр уходит ТОЛЬКО реальная подмена: пары X→X
        # ('NULL', '', '   ', короткие значения) отравляют другие колонки (N6).
        if result != value:
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
            # Ключ — исходное значение целиком (не strip): иначе подмена
            # ищется по «обрезанному» ключу и формат теряется.
            val = s.get("value")
            if not isinstance(val, str):
                continue
            if len(val.strip()) >= 3:  # индексы короче 3 символов не анонимизируем
                seen_values.add(val)

        if field_key not in self._field_cache:
            self._field_cache[field_key] = {}

        # Предвычисление — тем же детерминированным кодом, что и transform
        for val in seen_values:
            self._field_cache[field_key][val] = self._deterministic(val)

        # В реестр — только фактические подмены (см. комментарий в transform)
        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        reg.merge_mappings(
            {k: v for k, v in self._field_cache[field_key].items() if k != v}
        )

    # ── Deterministic generation ──────────────────────────────────────

    @staticmethod
    def _deterministic(code: str) -> str:
        """Подмена букв/цифр с посимвольным сохранением длины и маски.

        Гарантии:
          - ``len(result) == len(code)`` (пробелы по краям НЕ съедаются);
          - цифра → цифра, ASCII-буква → латинская буква, кириллическая буква
            → кириллическая, регистр совпадает по каждой позиции;
          - разделители (пробел, дефис, точка) остаются на своих местах;
          - ведущие нули сохраняются ('012345' → '0' + 5 цифр);
          - '', '   ', 'NULL' (любой регистр) возвращаются как есть.
        """
        if not isinstance(code, str):
            return code

        core = code.strip()

        # Честные passthrough: пустая строка, строка из пробелов, SQL-токены.
        # Раньше '   ' превращалось в '', а 'NULL' — в 'MLUM'.
        if not core or core.upper() == "NULL":
            return code

        # Похоже на почтовый индекс, только если есть что менять (>= 3 букв/цифр)
        alnum_positions = [i for i, c in enumerate(code) if c.isalnum()]
        if len(alnum_positions) < 3:
            return code

        # Затравка — функция от значения: один вход → один выход в любом
        # процессе, независимо от PYTHONHASHSEED (встроенный hash() не используется).
        seed = int.from_bytes(hashlib.sha256(code.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)

        # Ведущие нули — часть формата ('012345'): оставляем их нулями.
        # Но защищаем не весь нулевой прогон, если из-за этого подмены бы не
        # осталось: '0000' → '000' + одна сменённая цифра, а не '0000' целиком.
        core_start = len(code) - len(code.lstrip())
        leading_zero_len = len(core) - len(core.lstrip("0"))
        protected = (
            min(leading_zero_len, len(alnum_positions) - 1)
            if leading_zero_len
            else 0
        )
        keep_positions = {core_start + i for i in range(protected)}

        result = list(code)

        # 1) цифры — по одной позиции, гарантированно другие
        for pos in alnum_positions:
            if pos in keep_positions or not code[pos].isdigit():
                continue
            result[pos] = _different_digit(code[pos], rng)

        # 2) буквы — прогон за прогоном, внутри своего алфавита и регистра
        for start, end, kind in _letter_runs(code):
            result[start:end] = _pseudo_word(code[start:end], kind, rng)

        out = "".join(result)
        if out == code:
            # Страховка от схлопывания в оригинал (теоретически для чисто
            # числовых значений): сдвигаем последнюю не-защищённую цифру.
            for pos in reversed(alnum_positions):
                if pos not in keep_positions and code[pos].isdigit():
                    result[pos] = str((int(code[pos]) + 1) % 10)
                    break
            out = "".join(result)
        return out
