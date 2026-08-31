"""Email transformer — детерминированная подмена с полным сохранением формата.

Стратегия (закрывает N9 и N10 из `output/format_audit.md`):
  - в трансформацию входят ТОЛЬКО значения, удовлетворяющие
    ``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$``; всё остальное — честный passthrough;
  - local-part меняется целиком, но сохраняет длину, позиции разделителей
    (``.`` ``-`` ``_`` ``+``), регистр и алфавит каждого символа: кириллица
    остаётся кириллицей, латиница — латиницей;
  - домен не меняется вообще: ни написания, ни регистра
    (``@technoprom.ru`` обязано остаться ``@technoprom.ru``);
  - ``''``, ``'   '`` и ``'NULL'`` (любой регистр) возвращаются как есть,
    синтетические адреса вида ``user<hex>@example.com`` не генерируются;
  - подмена — функция от значения (sha256 → seed для генератора), поэтому
    один и тот же вход даёт один и тот же выход в любом процессе и результат
    воспроизводим между прогонами (никакого встроенного ``hash()``).

Без LLM и без сетевых вызовов: только локальные пулы «скелетов» имён/фамилий.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Dict, List, Any, Optional

from cloaker.base_transformer import BaseTransformer


# ── Пулы для генерации local-part ─────────────────────────────────────
# Хелперы дублируются в трёх «текстовых» трансформерах намеренно: общий модуль
# потребовал бы правки base_transformer.py, а он вне зоны этой задачи.

# «Скелеты» — реальные фамилии/имена; буква «C» не важна, важна длина и
# чередование гласных/согласных. Берём из них кусок нужной длины.
_LATIN_SKELETONS: List[str] = [
    "ivanov", "petrov", "smith", "jones", "miller", "smirnov", "popov",
    "orlov", "kuzmin", "fedorov", "volkov", "kozlov", "novikov", "morozov",
    "pavlov", "semin", "golubev", "vinogradov", "alexeev", "sergeev",
    "dmitriev", "kiselyov", "makarov", "tarasov", "sokolov", "lebedev",
    "davidov", "ermakov", "titov", "zaitsev", "baranov", "gusev", "kulikov",
    "davydov", "poleev", "medvedev", "nikolaev", "krylov", "eremin", "nasonov",
    "info", "office", "sales", "support", "market", "backend", "procure",
    "buh", "ooo", "ip", "mail", "post", "hr", "admin", "demo", "test",
]

_CYRILLIC_SKELETONS: List[str] = [
    "иванов", "петров", "смирнов", "кузнецов", "попов", "орлов", "козлов",
    "новиков", "морозов", "павлов", "семинов", "голубев", "волков", "титарев",
    "соколова", "лебедева", "кузьмина", "федорова", "михайлов", "егоров",
    "никитина", "захарова", "борисова", "королёва", "дубровин", "крутов",
    "белов", "титова", "щербак", "ершова", "жуков", "рыков", "тенин",
    "почта", "контора", "склад", "офис", "приемная", "кадры", "бухгалтерия",
    "отдел", "менеджер", "директор", "инфо", "сервис", "заявки", "клиент",
]

# Согласные/гласные — для «починки» слова до нужной длины (чтобы оно читалось)
_LATIN_CONSONANTS = "bcdfghjklmnprstvwxz"
_LATIN_VOWELS = "aeiou"
_CYR_CONSONANTS = "бвгдзжклмнпрстфхцчшщ"
_CYR_VOWELS = "аеиоуыэюя"


def _is_vowel(ch: str) -> bool:
    """Гласная ли буква (по алфавиту исходного символа)."""
    pool = _LATIN_VOWELS if ch.isascii() else _CYR_VOWELS
    return ch.lower() in pool


def _vowel_like(ch: str, rng: random.Random) -> str:
    """Гласная того же алфавита и регистра — чтобы слово читалось."""
    pool = _LATIN_VOWELS if ch.isascii() else _CYR_VOWELS
    base = rng.choice(pool)
    return base.upper() if ch.isupper() else base


def _apply_case(source: str, replacement: str) -> str:
    """Побуквенно переносит рисунок регистра с `source` на `replacement`.

    Вызывается только для строк равной длины, поэтому «IVANOV» и «ivanov»
    дают подмены со своим рисунком регистра, а не со скелетным.
    """
    return "".join(
        rep.upper() if src.isupper() else rep.lower()
        for src, rep in zip(source, replacement)
    )

_DIGITS = "0123456789"

# Ровно одна «@», без пробелов; домен обязан содержать точку
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Разделители внутри local-part: маска (число сегментов и их позиции) сохраняется
_LOCAL_SPLIT_RE = re.compile(r"([._\-+])")

# Подряд идущие буквы/цифры внутри сегмента
_RUN_RE = re.compile(r"\d+|[A-Za-zА-Яа-яЁё]+|.", re.UNICODE)

# SQL-токены и пустые значения, которые никогда не являются почтой
_NULL_TOKENS = {"NULL"}


def _is_ascii_letter(ch: str) -> bool:
    """Латинская ли это буква (по классу ИСХОДНОГО символа)."""
    return ch.isascii() and ch.isalpha()


_LATIN_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_CYR_LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"


def _other_letter(ch: str, rng: random.Random) -> str:
    """Буква того же алфавита и регистра, заведомо не равная `ch`.

    Нужна, чтобы подмена не «схлопывалась» в оригинал: иначе значение
    протекло бы в дамп без анонимизации.
    """
    letters = _LATIN_LETTERS if _is_ascii_letter(ch) else _CYR_LETTERS
    src = ch.lower()
    for _attempt in range(12):
        candidate = rng.choice(letters)
        if candidate != src:
            return candidate.upper() if ch.isupper() else candidate
    nxt = letters[(letters.index(src) + 1) % len(letters)] if src in letters else letters[0]
    return nxt.upper() if ch.isupper() else nxt


def _random_digit(rng: random.Random) -> str:
    return rng.choice(_DIGITS)


class EmailTransformer(BaseTransformer):
    """Детерминированная подмена email с сохранением маски local-part и домена."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # Локальный кэш по паре (таблица, колонка): value -> replacement
        self._field_cache: Dict[str, Dict[str, str]] = {}

    def type_name(self) -> str:
        return "email"

    # ── Основной вход ─────────────────────────────────────────────────

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None or not isinstance(value, str):
            return value

        field_key = f"{table}_{column}"

        if field_key in self._field_cache and value in self._field_cache[field_key]:
            return self._field_cache[field_key][value]

        result = self._deterministic(value)

        if field_key not in self._field_cache:
            self._field_cache[field_key] = {}
        self._field_cache[field_key][value] = result

        # В глобальный реестр пишем только реальные подмены: пары ''->...,
        # 'NULL'->'user…@example.com' отравляют весь прогон (N6) и выдают
        # синтетику в не-почтовых колонках.
        if result != value:
            from cloaker.cache import GlobalMappingRegistry
            reg = GlobalMappingRegistry.instance()
            reg.set_mapping(value, result)

        return result

    # ── Предварительный маппинг (ускорение фазы профилирования) ────────

    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        """Предвычисление подмен для известных значений (фаза профилирования)."""
        seen_values: set[str] = set()

        for s in samples:
            val = s.get("value")
            if not isinstance(val, str):
                continue
            # Профилируем только настоящие адреса — «мусор» не должен попасть
            # ни в кэш, ни в глобальный реестр.
            if _EMAIL_RE.match(val.strip()):
                seen_values.add(val.strip())

        if field_key not in self._field_cache:
            self._field_cache[field_key] = {}

        for val in seen_values:
            self._field_cache[field_key][val] = self._deterministic(val)

        from cloaker.cache import GlobalMappingRegistry
        reg = GlobalMappingRegistry.instance()
        reg.merge_mappings(
            {k: v for k, v in self._field_cache[field_key].items() if k != v}
        )

    # ── Генерация ─────────────────────────────────────────────────────

    @staticmethod
    def _deterministic(email: str) -> str:
        """Сохраняя формат: домен как есть, local-part — та же длина/маска/алфавит.

        Не-почтовые значения (в т. ч. '', '   ', 'NULL', текст с «@» внутри)
        возвращаются без изменений — генерить для них синтетический адрес
        запрещено. Регистр и написание домена сохраняются побуквенно.
        """
        if not isinstance(email, str):
            return email

        # Пустое значение, строка из пробелов и SQL-токены — passthrough
        core = email.strip()
        if not core or core.upper() in _NULL_TOKENS:
            return email

        # Вход разрешаем только «настоящей» почте: fullmatch не пропускает
        # ни один лишний символ, в том числе пробелы по краям.
        if not _EMAIL_RE.fullmatch(email):
            return email

        name_part, _, domain = email.rpartition("@")

        # seed — функция от значения: стабильно между процессами и прогонами
        seed = int.from_bytes(
            hashlib.sha256(email.encode("utf-8")).digest()[:8], "big"
        )
        rng = random.Random(seed)

        new_local = EmailTransformer._rebuild_local(name_part, rng)

        # Домен НЕ нормализуем: ни регистра, ни написания (N10)
        return f"{new_local}@{domain}"

    @staticmethod
    def _rebuild_local(name_part: str, rng: random.Random) -> str:
        """Пересборка local-part: сегменты и разделители остаются на своих местах."""
        pieces = _LOCAL_SPLIT_RE.split(name_part)
        out: List[str] = []
        for i, piece in enumerate(pieces):
            if i % 2 == 1:
                # разделитель (`.`, `-`, `_`, `+`) — точно на своей позиции
                out.append(piece)
                continue
            out.append(EmailTransformer._rebuild_segment(piece, rng))
        return "".join(out)

    @staticmethod
    def _rebuild_segment(segment: str, rng: random.Random) -> str:
        """Подмена одного сегмента local-part с сохранением классов символов.

        Подряд идущие буквы заменяются «словом» той же длины и того же
        алфавита, подряды цифр — цифрами той же разрядности, прочие символы
        остаются как есть.
        """
        out: List[str] = []
        for run in _RUN_RE.findall(segment):
            if not run:
                continue
            first = run[0]
            if first.isdigit():
                # Класс «цифра» сохраняем по каждой позиции; не-ASCII цифры
                # (например арабо-индийские) оставляем как есть
                digits = "".join(
                    _random_digit(rng) if c.isascii() else c for c in run
                )
                if digits == run:  # не «схлопываем» в оригинал
                    tail = len(digits) - 1
                    digits = digits[:tail] + _random_digit(rng)
                out.append(digits)
            elif first.isalpha():
                out.append(EmailTransformer._rebuild_word(run, rng))
            else:
                # Прочие символы (кавычки, слэши) — часть маски, не трогаем
                out.append(run)
        return "".join(out)

    @staticmethod
    def _rebuild_word(word: str, rng: random.Random) -> str:
        """Подмена буквенного куска local-part: та же длина, алфавит, регистр.

        Сначала — слово той же длины из «скелетов» (реальные фамилии и
        роли: подмена выглядит как настоящий ящик, а не как hex-шум из
        претензии N10). Если слова такой длины в пуле нет — берётся рисунок
        «гласная/согласная» из оригинала: класс символа по позиции
        сохраняется, и подмена всё равно читается.
        """
        latin = all(_is_ascii_letter(c) for c in word)
        skeletons = _LATIN_SKELETONS if latin else _CYRILLIC_SKELETONS
        same_len = [
            s for s in skeletons
            if len(s) == len(word) and s.lower() != word.lower()
        ]
        if same_len:
            return _apply_case(word, rng.choice(same_len))

        letters: List[str] = []
        for ch in word:
            if not ch.isalpha():
                # нестандартный символ — часть маски, оставляем как есть
                letters.append(ch)
                continue
            latin = _is_ascii_letter(ch)
            vowels = _LATIN_VOWELS if latin else _CYR_VOWELS
            cons = _LATIN_CONSONANTS if latin else _CYR_CONSONANTS
            src = ch.lower()
            pool = vowels if src in vowels else cons
            pick = src
            for _attempt in range(8):
                candidate = rng.choice(pool)
                if candidate != src:
                    pick = candidate
                    break
            letters.append(pick.upper() if ch.isupper() else pick)

        res = "".join(letters)

        if res.lower() == word.lower():
            # подмена «схлопнулась» в оригинал — меняем первую букву
            for i, ch in enumerate(word):
                if ch.isalpha():
                    return res[:i] + _other_letter(ch, rng) + res[i + 1:]
            return res

        # Слово сплошь из согласных не читается: добавляем гласную там, где в
        # оригинале был согласный (класс позиции это не меняет).
        if len(word) >= 3 and not any(_is_vowel(c) for c in res if c.isalpha()):
            for i, ch in enumerate(word):
                if ch.isalpha() and not _is_vowel(ch):
                    return res[:i] + _vowel_like(ch, rng) + res[i + 1:]

        return res
