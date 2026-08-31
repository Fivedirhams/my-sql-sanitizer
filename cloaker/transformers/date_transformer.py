"""Date shuffle transformer — детерминированный сдвиг дат с полным сохранением формата.

Модуль закрывает дефекты N2 и N11 из output/format_audit.md.

N2 (формат). Шаблон вывода больше НЕ выводится из ``str(datetime)`` — он
распознаётся по ИСХОДНОЙ строке значения и применяется к сдвинутой дате:
  * ``%Y-%m-%d``          → остаётся 10 символов, без времени;
  * ``%Y-%m-%d %H:%M:%S`` → остаётся 19 символов, время копируется дословно;
  * ``%Y-%m-%d %H:%M:%S.%f`` → дробная часть (микросекунды) сохраняется вместе
    со своей разрядностью, потому что «хвост» строки переносится литералом;
  * ``%d.%m.%Y``, ``%Y/%m/%d``, ``%d/%m/%Y``, ``%Y%m%d`` → порядок разрядов и
    разделители сохраняются буквально (разделители берутся из оригинала);
  * unix timestamp (10 цифр; 13 цифр = миллисекунды) → целое той же разрядности;
  * неизвестный/неспарсенный формат → возвращается ОРИГИНАЛ без изменений
    (лучше недотрансформированное поле, чем битый литерал).

N11 (scope и детерминизм). Scope читается из имени правила в конфиге
(``date_shuffle_month`` сдвигает только месяц, ``date_shuffle_year`` только
год), а не определяется «догадкой» по колонке; сдвиг не порождает невалидных
дат (31 февраля) и не меняет разрядность полей. Случайность — локальный
``random.Random`` с seed от конкретной входной строки, глобальное состояние
``random`` не используется, поэтому одно и то же значение даёт один и тот же
результат в разных колонках и разных прогонах.

Идемпотентность. Маппинг «оригинал → подмена» кэшируется в ``_mapping``, а все
выданные подмены попадают в множество «уже порождённых», поэтому повторный
проход по уже сдвинутому значению его не меняет. NULL / пустая строка / не-дата
возвращаются как есть.
"""

from __future__ import annotations

import calendar
import hashlib
import random
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from cloaker.base_transformer import BaseTransformer

# ── Константы формата ─────────────────────────────────────────────────

# «Хвост» строки после даты: время (с любой разрядностью дробной части) либо
# компактный HHMMSS. Хвост копируется в результат дословно — поэтому наличие
# или отсутствие времени и разрядность микросекунд сохраняются автоматически.
_TAIL_RE = re.compile(
    r"^(?:"
    r""                                          # пустой хвост — чистая дата
    r"|[T ]\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,9})?"  # ISO-время, в т.ч. с дробью
    r"(?:Z|[+-]\d{2}:?\d{2})?"                    # суффикс часовой зоны
    r"|\d{6}"                                     # компактный HHMMSS
    r"|[.,]\d{1,9}"                                # дробь без секунд (MySQL DATETIME(3))
    r")$"
)

# Дата с разделителем: год (4 цифры) стоит в начале или в конце, разделитель
# между разрядами обязан быть одинаковым (backreference).
_DATE_SEP_RE = re.compile(
    r"^(?P<a>\d{1,4})(?P<sep>[-/.])(?P<b>\d{1,2})(?P=sep)(?P<c>\d{1,4})(?P<tail>.*)$"
)

# Дата без разделителей: YYYYMMDD (опционально с хвостом HHMMSS).
_DATE_COMPACT_RE = re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?P<tail>.*)$")

# Unix timestamp: ровно 10 цифр (секунды) либо ровно 13 (миллисекунды).
_UNIX_RE = re.compile(r"^\d{10}$|^\d{13}$")

# Значения, которые обязаны остаться нетронутыми.
_PASSTHROUGH_LITERALS = frozenset({"NULL", "NONE"})

# Эпоха unix timestamp считается «naive»-арифметикой без tz-базы, иначе
# разрядность могла бы «плыть» в зависимости от часовой зоны хоста.
_UNIX_EPOCH = datetime(1970, 1, 1)

# Допустимые сдвиги года для scope == 'year' (0 исключён: значение обязано меняться).
_YEAR_OFFSETS = (-2, -1, 1, 2)

# Диапазон сдвига для scope == 'days' (фолбэк-гранулярность).
_DAY_OFFSETS = [o for o in range(-30, 31) if o != 0]


class DateFormat(NamedTuple):
    """Распознанный шаблон исходной строки значения.

    order/widths/seps — позиционные: i-й разряд пишется шириной widths[i]
    и отделяется seps[..]; stamp — уже валидный datetime оригинала.
    """

    kind: str                              # 'date' | 'unix'
    stamp: datetime                        # исходная дата/время до сдвига
    order: Tuple[str, ...] = ()            # порядок разрядов в строке: 'Y' | 'm' | 'd'
    widths: Tuple[int, ...] = ()           # разрядность каждого разряда в оригинале
    seps: Tuple[str, ...] = ()             # разделители, взятые из оригинала
    tail: str = ""                         # литерал после даты — переносится как есть
    scale: int = 1                         # для unix: 1 = секунды, 1000 = миллисекунды
    rest: int = 0                          # для unix: остаток младших разрядов (мс)


class DateShuffleTransformer(BaseTransformer):
    """Сдвигает год/месяц/день, сохраняя формат, длину и разделители оригинала.

    Гарантии формата (главное требование):
      1. Длина строки результата равна длине исходной строки.
      2. Классы символов совпадают посимвольно: цифры остаются цифрами,
         разделители — разделителями (проверяется маской в transform).
      3. Шаблон берётся из ИСХОДНОЙ СТРОКИ, а не из str(datetime) — поэтому
         DATE не обрастает временем ``00:00:00`` (дефект N2 аудита).
      4. Дробная часть секунд и «хвост» времени переносятся дословно.
      5. Unix timestamp остаётся числом той же разрядности.
      6. Неизвестный формат, NULL, пустая строка и не-дата проходят насквозь.

    Гранулярность сдвига (scope) берётся из имени правила, а не из догадки:
      1. scope, переданный в конструктор;
      2. имя трансформера в config.transform_rules (date_shuffle_month / ...);
      3. имя колонки;
      4. признак из сэмплов колонки;
      5. «month» — год не двигается, дата не переезжает в другой год (N11).

    Детерминизм: seed считается от исходного значения (SHA-256), глобальный
    random не используется. Одно и то же значение всегда даёт одну и ту же
    подмену, поэтому маппинг стабилен между прогонами и потоками.

    Ограничение: сохранение формата требует, чтобы результат был сдвигом
    конкретного значения, поэтому словарь подмен генерируется детерминированно
    (без LLM). LLM-маппинг из external mappings (другие исполнители) может
    вернуться только как явно сохранённый словарь пар «значение → смещённая
    дата того же формата»; на потоковом пути main.py используется тот же
    детерминированный маппинг (он пишется в self._mapping).
    """

    def __init__(
        self,
        config: Any,
        scope: Optional[str] = None,
        share_produced_guard: bool = False,
    ) -> None:
        """scope: 'month' | 'year' | 'days' — явная гранулярность сдвига.

        Нужна потому, что реестр cloaker.transformers создаёт класс как
        ``cls(config)`` и имя правила (``date_shuffle_month`` против
        ``date_shuffle_year``) внутрь экземпляра не попадает. Достаточно одной
        правки в реестре — ``cls(config, scope=...)``, — и гранулярность будет
        читаться из имени правила; со стороны этого файла поддержка уже есть.
        Без неё scope определяется из config.transform_rules (см. _resolve_scope).

        share_produced_guard: публиковать ли порожденные значения в общий
        реестр cache.GlobalMappingRegistry. По умолчанию выключено.
        """
        super().__init__(config)
        # Явный scope: DateShuffleTransformer(cfg, scope='year') — приоритет над остальным.
        self._explicit_scope = self._normalize_scope(scope)
        # Scope, выведенный из сэмплов колонки (см. _load_mapping).
        self._samples_scope: Optional[str] = None
        self._field_key: str = ""
        # Значения, которые сами являются результатом подмены ЭТИМ экземпляром.
        self._produced: set = set()
        # Оригиналы колонки из профиля: они трансформируются всегда.
        self._known_originals: set = set()
        # Флаг «общего» guards-реестра: по умолчанию выключен, потому что
        # глобальный реестр не различает «это подмена» и «это реальные данные
        # другой колонки» — на массовом дампе это оставляет до 20% реальных дат
        # не замаскированными (проверено на 396 уникальных датах output/*.sql).
        self._share_produced_guard = bool(share_produced_guard)

    # ── Точка входа ───────────────────────────────────────────────────

    def transform(self, value: Optional[str], table: str = "", column: str = "") -> Optional[str]:
        if value is None:
            return value

        text = value if isinstance(value, str) else str(value)

        # Стабильный маппинг: один и тот же оригинал всегда даёт одну и ту же подмену.
        cached = self._mapping.get(text)
        if cached is not None:
            return cached

        lead, core, trail = self._split_ws(text)
        if not core:
            # Пустая строка (или только пробелы) — как есть.
            return value
        if core.upper() in _PASSTHROUGH_LITERALS:
            # Строковый литерал 'NULL' обязан остаться 'NULL'.
            return value

        # Защита от двойной трансформации: значение, которое само является
        # результатом подмены, дальше не уезжает.
        if self._is_produced(core):
            return value

        fmt = self._detect_format(core)
        if fmt is None:
            # Неизвестный формат — надежнее оставить поле как есть.
            return value

        scope = self._resolve_scope(table, column)
        rng = random.Random(self._stable_seed(core))
        shifted = self._shift_within_format(fmt, core, scope, rng)
        if shifted is None:
            # Сохранить формат/разрядность не удалось — оригинал, а не битый литерал.
            return value

        result = lead + shifted + trail
        if len(result) != len(text) or self._mask(result) != self._mask(text):
            # Страховка: любое расхождение длины или маски классов символов = passthrough.
            return value

        self._remember(text, core, result, shifted)
        return result

    def type_name(self) -> str:
        return "date_shuffle"

    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        """Пред-вычисляет детерминированный маппинг по профилю. LLM не нужен."""
        self._field_key = field_key or ""
        table, column = self._split_field_key(self._field_key)

        values: List[str] = []
        for item in samples or []:
            val = item.get("value") if isinstance(item, dict) else item
            if isinstance(val, str) and val:
                values.append(val)

        # Профиль колонки = множество её реальных оригиналов. Значение из этого
        # множества обязано быть трансформировано, даже если оно совпало с чьей-то
        # подменой: не замаскировать реальную дату хуже, чем сдвинуть её дважды.
        for val in values:
            _lead, core, _trail = self._split_ws(val)
            if core and self._detect_format(core) is not None:
                self._known_originals.add(core)

        # Тот же признак, что использует auto-select в main.py: наличие времени у
        # значений колонки. Нужен потому, что main.py не передаёт имя правила в
        # конструктор трансформера.
        if values and self._explicit_scope is None:
            has_time = any(":" in v for v in values)
            self._samples_scope = "month" if has_time else "year"

        for val in values:
            self.transform(val, table, column)

    # ── Scope (N11) ───────────────────────────────────────────────────

    def _resolve_scope(self, table: str, column: str) -> str:
        """Определяет гранулярность сдвига по имени правила, а не по «догадке».

        Порядок: явный scope из конструктора → имя правила в config.transform_rules
        (``date_shuffle_month`` / ``date_shuffle_year``) → эвристика по имени
        колонки → признак из сэмплов колонки → «month».

        Значение по умолчанию — «month», то есть год не трогается: дата не переезжает
        в другой год (дефект N11: ``2024-01-10 → 2023-12-22``).
        """
        if self._explicit_scope:
            return self._explicit_scope

        from_rule = self._scope_from_rules(table, column)
        if from_rule:
            return from_rule

        from_column = self._normalize_scope(self._detect_scope(column))
        # 'days' из эвристики по имени колонки — это «мнения нет», а не гранулярность:
        # дробь по дням меняет и месяц, и год. Явный 'days' возможен только через
        # конструктор (scope='days') или правило date_shuffle_days.
        if from_column and from_column != "days":
            return from_column

        if self._samples_scope:
            return self._samples_scope

        return "month"

    def _scope_from_rules(self, table: str, column: str) -> Optional[str]:
        """Читает scope из имени трансформера, объявленного в конфиге.

        Ключи правил в config.yaml — «Table.Column», а поле-ключ профиля —
        «Table_Column», поэтому перебираются обе формы (плюс «Column»).
        """
        rules = getattr(self.config, "transform_rules", None) or {}
        if not isinstance(rules, dict) or not rules:
            return None

        candidates = {
            "{}.{}".format(table, column).lower(),
            "{}_{}".format(table, column).lower(),
            str(column).lower(),
        }
        if self._field_key:
            candidates.add(self._field_key.lower())
            candidates.add(self._field_key.lower().replace("_", ".", 1))

        for key, rule in rules.items():
            if str(key).lower().strip() not in candidates:
                continue
            scope = self._normalize_scope(rule)
            if scope:
                return scope
        return None

    @staticmethod
    def _normalize_scope(rule: Any) -> Optional[str]:
        """``date_shuffle_month`` → 'month', ``date_shuffle_year`` → 'year'."""
        if not rule:
            return None
        name = str(rule).strip().lower()
        if name.endswith("_month") or name == "month":
            return "month"
        if name.endswith("_year") or name == "year":
            return "year"
        if name.endswith("_days") or name == "days":
            return "days"
        return None

    @staticmethod
    def _detect_scope(column: str) -> str:
        """Эвристика по имени колонки (фолбэк, сохранён для совместимости)."""
        col_lower = (column or "").lower()
        if "_month" in col_lower or col_lower.endswith("month") or "hire" in col_lower:
            return "month"
        if "_year" in col_lower or col_lower.endswith("year") or "birth" in col_lower:
            return "year"
        return "days"

    @staticmethod
    def _split_field_key(field_key: str) -> Tuple[str, str]:
        """``Employee_BirthDate`` → ('Employee', 'BirthDate') — как в main.py."""
        parts = str(field_key or "").split("_", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return "", parts[0] if parts else ""

    # ── Распознавание формата (N2) ────────────────────────────────────

    @classmethod
    def _detect_format(cls, core: str) -> Optional[DateFormat]:
        """Возвращает шаблон по исходной строке либо None для неизвестного формата."""
        if not core:
            return None

        # 1) Unix timestamp: 10 цифр (секунды) или 13 цифр (миллисекунды).
        if _UNIX_RE.match(core):
            scale = 1 if len(core) == 10 else 1000
            whole, rest = divmod(int(core), scale)
            stamp = cls._unix_to_datetime(whole)
            if stamp is None:
                return None
            return DateFormat(kind="unix", stamp=stamp, scale=scale, rest=rest)

        # 2) Дата без разделителей: YYYYMMDD[HHMMSS].
        match = _DATE_COMPACT_RE.match(core)
        if match and cls._tail_ok(match.group("tail")):
            year = int(match.group("y"))
            month = int(match.group("m"))
            day = int(match.group("d"))
            if not cls._valid_date(year, month, day):
                return None
            stamp = cls._combine(year, month, day, match.group("tail"))
            if stamp is None:
                return None
            return DateFormat(
                kind="date",
                stamp=stamp,
                order=("Y", "m", "d"),
                widths=(4, 2, 2),
                seps=("", ""),
                tail=match.group("tail"),
            )

        # 3) Дата с разделителем: год в начале или в конце, разделитель одинаковый.
        match = _DATE_SEP_RE.match(core)
        if not match or not cls._tail_ok(match.group("tail")):
            return None

        a, sep, b, c, tail = (
            match.group("a"),
            match.group("sep"),
            match.group("b"),
            match.group("c"),
            match.group("tail"),
        )
        values = (int(a), int(b), int(c))
        widths = (len(a), len(b), len(c))

        if widths[0] == 4 and widths[2] != 4:
            # %Y<sep>%m<sep>%d
            order: Tuple[str, ...] = ("Y", "m", "d")
        elif widths[0] != 4 and widths[2] == 4:
            # %d<sep>%m<sep>%Y (европейский) либо %m<sep>%d<sep>%Y (американский).
            # Порядок определяем по диапазонной валидности: если средняя группа не
            # может быть месяцем — она день. При полной неоднозначности (оба ≤ 12)
            # выбираем день-вперёд — как в перечне требований (N2/N11).
            order = ("d", "m", "Y")
            if values[1] > 12 >= values[0]:
                order = ("m", "d", "Y")
        else:
            return None

        parts = dict(zip(order, values))
        year, month, day = parts["Y"], parts["m"], parts["d"]
        if not cls._valid_date(year, month, day):
            return None
        # Ширина разряда в оригинале обязана покрывать само значение
        # (ширина 2 ⇒ ведущий ноль, ширина 1 ⇒ без ведущего нуля).
        if any(len(str(value)) > width for value, width in zip(values, widths)):
            return None
        stamp = cls._combine(year, month, day, tail)
        if stamp is None:
            return None

        return DateFormat(
            kind="date",
            stamp=stamp,
            order=order,
            widths=widths,
            seps=(sep, sep),
            tail=tail,
        )

    @staticmethod
    def _tail_ok(tail: str) -> bool:
        return bool(_TAIL_RE.match(tail or ""))

    @staticmethod
    def _valid_date(year: int, month: int, day: int) -> bool:
        if not (1 <= year <= 9999) or not (1 <= month <= 12) or day < 1:
            return False
        return day <= calendar.monthrange(year, month)[1]

    @staticmethod
    def _parse_tail_time(tail: str) -> Tuple[int, int, int, int]:
        """Достает (часы, минуты, секунды, микросекунды) из «хвоста» строки."""
        tail = tail or ""
        match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", tail)
        if match:
            second = int(match.group(3) or 0)
        else:
            compact = re.match(r"^(\d{2})(\d{2})(\d{2})$", tail)
            if not compact:
                return 0, 0, 0, 0
            match = compact
            second = int(compact.group(3))
        fraction = re.search(r"[.,](\d+)", tail[match.end():])
        micro = 0
        if fraction:
            # Разрядность дроби исходной строки сохраняется переносом хвоста;
            # здесь только приводим дробь к микросекундам.
            micro = int(fraction.group(1)[:6].ljust(6, "0"))
        return int(match.group(1)), int(match.group(2)), second, micro

    @classmethod
    def _combine(cls, year: int, month: int, day: int, tail: str) -> Optional[datetime]:
        """Собирает datetime оригинала (валидация времени из хвоста)."""
        hour, minute, second, micro = cls._parse_tail_time(tail)
        try:
            return datetime(year, month, day, hour, minute, second, micro)
        except ValueError:
            return None

    @staticmethod
    def _unix_to_datetime(seconds: int) -> Optional[datetime]:
        try:
            stamp = _UNIX_EPOCH + timedelta(seconds=seconds)
        except (OverflowError, ValueError):
            return None
        if not (1 <= stamp.year <= 9999):
            return None
        return stamp

    @classmethod
    def _parse_date(cls, date_str: Any) -> Optional[datetime]:
        """Парсит поддерживаемые форматы (совместимость со старым API)."""
        if not isinstance(date_str, str):
            return None
        fmt = cls._detect_format(date_str.strip())
        return None if fmt is None else fmt.stamp

    # ── Сдвиг ─────────────────────────────────────────────────────────

    @staticmethod
    def _shift_candidates(day: date, scope: Optional[str], rng: random.Random) -> Iterable[date]:
        """Кандидаты сдвига в рамках scope; всегда валидная дата без смены разрядности года.

        Дробление на «только месяц» / «только год» соответствует именам правил
        ``date_shuffle_month`` / ``date_shuffle_year``: scope «month» сдвигает
        только месяц, scope «year» — только год. День при сдвиге зажимается в
        рамки месяца кандидата, поэтому 31 февраля невозможно.
        """
        if scope == "year":
            offsets = list(_YEAR_OFFSETS)
            rng.shuffle(offsets)
            for offset in offsets:
                year = day.year + offset
                if not 1 <= year <= 9999:
                    continue
                max_day = calendar.monthrange(year, day.month)[1]
                yield date(year, day.month, min(day.day, max_day))
            return

        if scope == "days":
            offsets = list(_DAY_OFFSETS)
            rng.shuffle(offsets)
            for offset in offsets:
                shifted = day + timedelta(days=offset)
                if 1 <= shifted.year <= 9999:
                    yield shifted
            return

        # scope == 'month' (и значение по умолчанию): год и день не трогаем.
        months = [m for m in range(1, 13) if m != day.month]
        rng.shuffle(months)
        for month in months:
            max_day = calendar.monthrange(day.year, month)[1]
            yield date(day.year, month, min(day.day, max_day))

    @classmethod
    def _shift_within_format(
        cls,
        fmt: DateFormat,
        core: str,
        scope: Optional[str],
        rng: random.Random,
    ) -> Optional[str]:
        """Сдвигает дату и рендерит результат строго по шаблону исходной строки."""
        stamp = fmt.stamp
        if fmt.kind == "unix":
            for candidate in cls._shift_candidates(stamp.date(), scope, rng):
                shifted = datetime(
                    candidate.year, candidate.month, candidate.day,
                    stamp.hour, stamp.minute, stamp.second,
                )
                seconds = int((shifted - _UNIX_EPOCH).total_seconds())
                rendered = str(seconds * fmt.scale + fmt.rest)
                # Разрядность целого обязана совпасть с оригиналом.
                if len(rendered) == len(core) and rendered.isdigit():
                    return rendered
            return None

        for candidate in cls._shift_candidates(stamp.date(), scope, rng):
            rendered = cls._render(fmt, candidate)
            if rendered is not None and len(rendered) == len(core):
                return rendered
        return None

    @staticmethod
    def _render(fmt: DateFormat, day: date) -> Optional[str]:
        """Собирает строку по разрядам: те же ширины, те же разделители, тот же хвост."""
        values = {"Y": day.year, "m": day.month, "d": day.day}
        out: List[str] = []
        for index, name in enumerate(fmt.order):
            if index:
                out.append(fmt.seps[index - 1])
            text = str(values[name])
            width = fmt.widths[index]
            if len(text) > width:
                # Разряд не влезает в оригинальную ширину — формат был бы сломан.
                return None
            out.append(text.rjust(width, "0"))
        return "".join(out) + fmt.tail

    @classmethod
    def _format_date(cls, shuffled: datetime, original: Any) -> Optional[str]:
        """Форматирует сдвинутую дату по шаблону ИСХОДНОЙ СТРОКИ (исправление N2).

        Второй аргумент — исходная строка значения; для совместимости со старым
        вызовом принимается и ``datetime`` (тогда шаблон считается по ``str()``).
        Возвращает None, если формат неизвестен или разряд не влезает в оригинал:
        вызывающий код обязан отказаться от подмены и оставить оригинал.
        """
        if isinstance(original, (datetime, date)):
            original = str(original)
        core = str(original).strip()
        fmt = cls._detect_format(core)
        if fmt is None or fmt.kind != "date":
            return None
        return cls._render(fmt, shuffled.date())

    # ── Кэш маппинга и защита от двойной трансформации ────────────────

    def _remember(self, text: str, core: str, result: str, shifted: str) -> None:
        """Фиксирует маппинг и помечает выданное значение как «уже подменённое»."""
        self._mapping[text] = result
        self._produced.add(shifted)
        self._produced.discard(core)
        if self._share_produced_guard:
            registry = self._registry()
            if registry is not None:
                try:
                    # identity-маркер: значение порождено подменой → дальше не едет.
                    registry.set_mapping(shifted, shifted)
                except Exception:
                    pass

    def _is_produced(self, core: str) -> bool:
        """True, если значение само является результатом подмены этого прохода.

        Оригиналы из профиля колонки имеют приоритет: они маскируются всегда.
        """
        if core in self._mapping or core in self._known_originals:
            return False
        if core in self._produced:
            return True
        if not self._share_produced_guard:
            return False
        registry = self._registry()
        if registry is None:
            return False
        try:
            hit = registry.get_replacement(core)
        except Exception:
            return False
        return hit == core

    @staticmethod
    def _registry() -> Optional[Any]:
        """Глобальный реестр — best effort: модуль принадлежит другому исполнителю."""
        try:
            from cloaker.cache import GlobalMappingRegistry

            return GlobalMappingRegistry.instance()
        except Exception:
            return None

    # ── Утилиты ───────────────────────────────────────────────────────

    @staticmethod
    def _split_ws(text: str) -> Tuple[str, str, str]:
        """Разбивает на «пробелы до», ядро, «пробелы после» (для CHAR-полей)."""
        core = text.strip()
        if not core:
            return text, "", ""
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        return lead, core, trail

    @staticmethod
    def _mask(text: str) -> str:
        """Маска классов символов: цифра → 'D', остальное — сам символ."""
        return "".join("D" if ch.isdigit() else ch for ch in text)

    @staticmethod
    def _stable_seed(core: str) -> int:
        """Seed от конкретной входной строки (стабилен между прогонами и процессами)."""
        digest = hashlib.sha256(core.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")
