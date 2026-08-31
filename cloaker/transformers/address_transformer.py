"""Address transformer — подмена адреса с полным сохранением шаблона оригинала.

Гарантии (закрывает N9 из `output/format_audit.md`; раньше fallback для
'ул. Ленина, д. 45, офис 302' выдавал '673 Maple Dr dd40' — и алфавит, и
структура терялись):

  - кириллический адрес остаётся кириллическим, латинский — латинским;
  - служебные префиксы (`ул.`, `пр.`, `пер.`, `ш.`, `бульвар`, `д.`, `корп.`,
    `офис`, `оф.`, `кв.`, `г.`, `обл.`, а также `St`, `Ave`, `Rd` …) остаются
    дословно на своих местах вместе с разделителями «, », точками и пробелами;
  - номера (дом/офис/квартира) меняются в пределах той же разрядности, ведущие
    нули сохраняются;
  - длина результата равна длине оригинала, класс символа в каждой позиции
    (буква/цифра/пробел/разделитель) совпадает с оригиналом;
  - город заменяется городом из того же пула и той же длины;
  - подмена — чистая функция от значения (seed = sha256), встроенный `hash()`
    больше не используется, поэтому результат не зависит от PYTHONHASHSEED и
    воспроизводим между прогонами.

LLM-маппинг фазы профилирования сохранён, но его результат проходит проверку
формата: подмена, сменившая алфавит или «схлопнувшаяся» в оригинал, отбрасывается
в пользу детерминированного fallback.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Dict, List, Any, Optional, Tuple

from cloaker.base_transformer import BaseTransformer


# ── Пулы названий ────────────────────────────────────────────────────────
# Правила отбора: название того же алфавита, что и оригинал, той же длины и с
# той же расстановкой дефисов. Если пары нет — подмена идётся побуквенно, так
# что «та же длина и тот же алфавит» выполняются всегда, а не «обычно».

_CITY_RU: List[str] = [
    # 3–4 буквы
    "Уфа", "Мга", "Обь", "Омск", "Ухта", "Клин", "Тосна",
    # 5 букв
    "Тверь", "Киров", "Углич", "Юрга", "Елец", "Ишим", "Сочи", "Тула",
    "Орёл", "Муром", "Чехов", "Химки", "Псков", "Тайга",
    # 6–7 букв
    "Калуга", "Рязань", "Тамбов", "Брянск", "Липецк", "Элиста", "Сызрань",
    "Яхрома", "Каменка", "Нальчик", "Волхов", "Видное", "Мытищи", "Выборг",
    "Люберцы", "Сергиев", "Саратов", "Балашов", "Кировск", "Воронеж",
    # 8–9 букв
    "Кострома", "Подольск", "Тобольск", "Белгород", "Мурманск", "Смоленск",
    "Балашиха", "Серпухов", "Коломна", "Энгельс", "Волжский", "Бородино",
    "Краснодар", "Пятигорск", "Махачкала", "Хабаровск", "Жуковский",
    "Дзержинск", "Егорьевск", "Раменское", "Урюпинск",
    # 10–12 букв
    "Всеволожск", "Ставрополь", "Балабаново", "Коммунар", "Малоярославец",
    "Стерлитамак", "Уссурийск", "Благовещенск", "Магнитогорск", "Электросталь",
    # дефисные (маска дефиса учитывается при подборе)
    "Улан-Удэ", "Йошкар-Ола", "Усть-Илимск", "Наро-Фоминск",
    "Санкт-Петербург",
]

_STREET_RU: List[str] = [
    "Мира", "Зари", "Речная", "Лесная", "Озерная", "Степная", "Парковая",
    "Дачная", "Крутая", "Ясная", "Пологая", "Садовая", "Северная", "Южная",
    "Школьная", "Вокзальная", "Заводская", "Советская", "Трудовая",
    "Молодежная", "Солнечная", "Зеленая", "Кленовая", "Вишневая", "Полевая",
    "Луговая", "Нагорная", "Просторная", "Свободы", "Победы", "Гагарина",
    "Ленина", "Пушкина", "Кирова", "Свердлова", "Куйбышева", "Чкалова",
    "Горького", "Кутузова", "Суворова", "Жукова", "Матросова", "Гоголя",
    "Достоевского", "Чехова", "Толстого", "Ахшурина", "Строителей",
    "Металлургов", "Энергетиков", "Водников", "Лётная", "Рабочая",
    "Крестьянская", "Дорожная", "Кольцевая", "Пионерская", "Комсомольская",
    "Октябрьская", "Первомайская", "Промышленная", "Ленинградская",
    "Калужская", "Смоленская", "Переяславская",
    # короткие названия — под «пер. Ясный», «туп. Тихая» и т. п.
    "Тихая", "Весна", "Сосны", "Роща", "Дубы", "Связная", "Вольная",
    "Шокаля", "Баумана", "Гастелло", "Крауля", "Лизюкова",
]

# Города и улицы для «западных» значений (Chinook: '111 Thatcher St')
_CITY_EN: List[str] = [
    "Rio", "Perth", "Lyon", "Oslo", "Berna", "Dover", "Kent", "Mesa",
    "Austin", "Boston", "Denver", "Seattle", "Orlando", "Atlanta", "Bristol",
    "Chester", "Fairview", "Highland", "Lakewood", "Rockford", "Springdale",
    "Portland", "Franklin", "Clifton", "Newark", "Trenton", "Richmond",
]

_STREET_EN: List[str] = [
    "Main", "Oak", "Maple", "Park", "Elm", "Cedar", "Birch", "Walnut",
    "Highland", "Lakwood", "Sunrise", "Valley", "Meadow", "Prairie",
    "Thatcher", "Kingsley", "Queensway", "Brookside", "Hillcrest",
    "Westgate", "Eastwood", "Southvale", "Northfield", "Fairview",
    "Rosewood", "Juniper", "Chestnut", "Sycamore", "Willow", "Aspen",
]

# Названия улиц в родительном падеже («имени кого» / «кого»): читаются с любым
# префиксом — и «ул. », и «пр. », и «ш. ». При «пр. Лесная» спотыкаешься,
# поэтому для не-женских префиксов предпочитаем именно этот подпул.
_STREET_RU_GEN: List[str] = [
    "Гагарина", "Ленина", "Пушкина", "Кирова", "Свердлова", "Куйбышева",
    "Чкалова", "Горького", "Кутузова", "Суворова", "Жукова", "Матросова",
    "Гоголя", "Достоевского", "Чехова", "Толстого", "Строителей",
    "Металлургов", "Энергетиков", "Водников", "Свободы", "Победы", "Мира",
    "Зари", "Космонавтов", "Академика", "Геологов", "Нефтяников",
]

# Служебные префиксы/слова (сравниваются в нижнем регистре, без точек):
# они и есть шаблон адреса, поэтому в выходе сохраняются дословно.
_PREFIX_TOKENS = {
    # русские
    "ул", "улица", "пр", "просп", "проспект", "пер", "переулок", "переул",
    "ш", "шоссе", "бр", "б-р", "бульвар", "бул", "наб", "набережная",
    "корп", "корпус", "кор", "к", "д", "дом", "стр", "строение", "офис",
    "оф", "кв", "квартира", "комната", "ком", "пом", "помещение", "каб",
    "кабинет", "эт", "этаж", "г", "город", "обл", "область", "р-н", "рн",
    "район", "с", "село", "дер", "деревня", "дб", "пос", "посёлок", "поселок",
    "ст", "станция", "пл", "площадь", "туп", "тупик", "проезд", "тракт",
    "аллея", "мкр", "микрорайон", "тер", "территория", "вл", "влад", "владение",
    "блок", "лит", "н", "ц", "дк", "озд", "снт", "ут", "уч", "кор",
    "респ", "республика", "ак", "акад", "шт", "цех", "склад", "база",
    "лагерь", "пансионат", "хоз", "комбинат",
    # английские
    "st", "ave", "av", "rd", "blvd", "dr", "ln", "way", "ct", "pl", "hwy",
    "pkwy", "sq", "ter", "cir", "apt", "unit", "suite", "ste", "bldg", "fl",
    "no", "nw", "ne", "se", "sw",
}

# Предлоги/союзы — тоже сохраняются: они держат структуру фразы
_STOP_WORDS = {
    "и", "с", "в", "на", "при", "у", "от", "из", "по", "для", "без", "или",
    "как", "или", "the", "of", "and", "de", "la", "le", "das", "van",
}

# Префиксы «населённого пункта»: слово после них — город/район, а не улица.
_CITY_PREFIXES = {
    "г", "город", "обл", "область", "р-н", "рн", "район", "с", "село",
    "дер", "деревня", "пос", "поселок", "посёлок", "мкр", "микрорайон",
    "ст", "станция", "пл", "площадь", "округ", "волость",
    "респ", "республика",
}

# Префиксы, с которыми название улицы стоит в родительном падеже
_GENITIVE_PREFIXES = {
    "пр", "просп", "проспект", "бр", "б-р", "бульвар", "бул", "ш", "шоссе",
    "наб", "набережная", "туп", "тупик", "проезд", "тракт", "аллея", "пер",
    "переулок", "переул", "мкр", "микрорайон", "кв", "корп", "корпус",
}

# Токены: буквы (кириллица/латиница, дефисные формы вроде «Санкт-Петербург» —
# один токен), цифры, всё остальное (разделители, пробелы, пунктуация).
_TOKEN_RE = re.compile(
    r"(?P<word>[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*)"
    r"|(?P<num>\d+)"
    r"|(?P<other>[^A-Za-zА-Яа-яЁё\d]+)"
)

# SQL-токены и пустые значения, адресом не являющиеся
_NULL_TOKENS = {"NULL"}

_LATIN = "abcdefghijklmnopqrstuvwxyz"
_CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_DIGITS = "0123456789"

# Согласные/гласные для читаемой побуквенной подмены (см. _pseudo_word)
_CONSONANTS_RU = "бвгдзжклмнпрстфхцчшщ"
_VOWELS_RU = "аеиоуыэюя"
_CONSONANTS_EN = "bcdfghjklmnprstvwxz"
_VOWELS_EN = "aeiou"


def _letter_alphabet(ch: str) -> Optional[str]:
    """Алфавит по классу ИСХОДНОГО символа: латиница / кириллица / None.

    Возврат None означает «алфавит не опознан» — такой символ не подменяется,
    чтобы не превращать кириллицу в латынь и наоборот.
    """
    if ch.isascii():
        return _LATIN if ch.isalpha() else None
    return _CYRILLIC if ch.lower() in _CYRILLIC else None


def _different_letter(ch: str, rng: random.Random) -> str:
    """Буква того же алфавита и того же регистра, заведомо не равная `ch`."""
    alphabet = _letter_alphabet(ch)
    if alphabet is None:
        return ch
    lowered = ch.lower()
    for _ in range(8):
        candidate = rng.choice(alphabet)
        if candidate != lowered:
            break
    else:
        idx = alphabet.index(lowered) if lowered in alphabet else 0
        candidate = alphabet[(idx + 1) % len(alphabet)]
    return candidate.upper() if ch.isupper() else candidate


def _letter_kind(ch: str) -> Optional[str]:
    """Алфавит буквы для группировки прогонов: ``'ru'`` | ``'en'`` | ``None``."""
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


def _syllables(text: str, kind: str) -> List[str]:
    """Рисунок «гласная/согласная» по позициям исходного написания."""
    vowels = _VOWELS_RU if kind == "ru" else _VOWELS_EN
    return ["v" if ch.lower() in vowels else "c" for ch in text]


def _pseudo_word(text: str, kind: str, rng: random.Random) -> str:
    """Псевдослово той же длины, алфавита, регистра и слогового рисунка.

    Применяется, когда в пуле нет названия той же маски: чередование
    согласная/гласная берётся ИЗ исходного слова, поэтому подмена читается
    как слово, а не как шум («Здщдд» вместо «Барнев» — формат одинаковый,
    но первое выглядит опечаткой).
    """
    cons, vows = (_CONSONANTS_RU, _VOWELS_RU) if kind == "ru" else (_CONSONANTS_EN, _VOWELS_EN)
    pattern = _syllables(text, kind)
    if pattern and "v" not in pattern:
        # Сплошь согласные читаются только с гласной внутри
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
    return "".join(_different_letter(ch, rng) for ch in text)


def _substitute_runs(text: str, rng: random.Random) -> str:
    """Замена буквенных прогонов на псевдослова; всё остальное — без изменений."""
    out = list(text)
    for start, end, kind in _letter_runs(text):
        out[start:end] = _pseudo_word(text[start:end], kind, rng)
    return "".join(out)


def _format_kind(ch: str) -> str:
    """Класс символа для сверки формата оригинала и готовой подмены."""
    if ch.isdigit():
        return "9"
    if ch.isalpha():
        return "A" if ch.isascii() else "C"
    if ch.isspace():
        return "S"
    return "O"


def _format_of(text: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Отпечаток формата: классы символов + рисунок регистра по позициям."""
    return (
        tuple(_format_kind(ch) for ch in text),
        tuple("u" if ch.isupper() else ("l" if ch.islower() else "-") for ch in text),
    )


def _apply_case(source: str, replacement: str) -> str:
    """Побуквенно переносит рисунок регистра с `source` на `replacement`.

    Вызывается только для строк равной длины, поэтому «Москва» (с заглавной)
    и «МОСКВА» дают подмены с сохранением именно своего рисунка регистра.
    """
    return "".join(
        rep.upper() if src.isupper() else rep.lower()
        for src, rep in zip(source, replacement)
    )


class AddressTransformer(BaseTransformer):
    """Подмена адресов, сохраняющая структуру, длину и алфавит оригинала.

    Использует GlobalMappingRegistry для кросс-табличной согласованности, но не
    принимает из реестра подмены, ломающие формат.
    """

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        if value is None or not isinstance(value, str):
            return value

        core = value.strip()
        # Пустая строка, строка из пробелов и SQL-токены — честный passthrough:
        # сочинять адрес из «нет данных» нельзя (тот же класс дефектов, что N6).
        if not core or core.upper() in _NULL_TOKENS:
            return value

        # Убеждаемся, что маппинг загружен
        field_key = f"{table}_{column}"
        if not self._ensure_loaded(field_key):
            return value

        cached = self._mapping.get(value)
        if cached is None:
            # Глобальный кэш: значение могло появиться из другой таблицы
            from cloaker.cache import GlobalMappingRegistry
            reg = GlobalMappingRegistry.instance()
            cached = reg.get_replacement(value)

        # Берём готовую подмену только если она не ломает формат оригинала
        if cached and self._acceptable(value, str(cached)):
            return str(cached)

        result = self._fallback_address(value)

        # Кросс-табличная согласованность: один адрес -> одна подмена и в
        # other таблице. В реестр не пишутся passthrough-значения (X→X), иначе
        # «NULL» оседает в маппинге и всплывает в чужих колонках (N6).
        if result != value:
            from cloaker.cache import GlobalMappingRegistry
            GlobalMappingRegistry.instance().set_mapping(value, result)

        return result

    def type_name(self) -> str:
        return "address"

    def _load_mapping(
        self,
        samples: List[Dict[str, Any]],
        field_key: str,
        stats: Dict[str, Any],
    ) -> None:
        """Готовит подмены для известных значений (фаза профилирования).

        В self._mapping попадают только пары, сохраняющие формат: main.py
        подставляет их напрямую, в обход transform(). Иначе LLM-адрес вида
        «ул. Пушкина, д. 24» на место «ул. Гагарина, д. 8» ломает каркас (N9):
        длина та же, а запятая и цифры уехали по позициям.
        """
        from cloaker.llm_client import LLMClient
        from cloaker.cache import GlobalMappingRegistry
        client = LLMClient(self.config)
        reg = GlobalMappingRegistry.instance()

        unique_values = [
            value
            for value in {sample.get("value") for sample in samples}
            if isinstance(value, str) and value
        ]

        # Значения, для которых глобальной подмены ещё нет — в LLM
        unseen = [value for value in unique_values if not reg.get_replacement(value)]

        raw_map: Dict[str, str] = {}
        if unseen:
            result = client.generate_address_mapping(field_key, unseen, stats)
            if isinstance(result, dict):
                raw_map = {str(k): str(v) for k, v in result.items()}

        accepted = {
            original: replacement
            for original, replacement in raw_map.items()
            if original != replacement and self._acceptable(original, replacement)
        }
        if accepted:
            # В реестр (и далее в global_mapping.json) — только корректные
            # адреса: кривая подмена ушла бы в другие колонки (N6).
            reg.merge_mappings(accepted)

        prepared = dict(accepted)
        for value in unique_values:
            hit = reg.get_replacement(value)
            if hit and hit != value and self._acceptable(value, hit):
                prepared[value] = hit

        self._mapping.update(prepared)

    # ── Проверка «не сломать формат» ──────────────────────────────────

    @staticmethod
    def _acceptable(original: str, replacement: str) -> bool:
        """Годится ли готовая подмена из LLM-маппинга или глобального реестра.

        Отклоняем: пустые/пробельные, совпадающие с оригиналом (утечка PII) и
        любые, что меняют длину, класс или регистр хотя бы в одной позиции.
        Реестр наполняется и прошлыми прогонами, и LLM-маппингом фазы
        профилирования, поэтому «доверие по умолчанию» к нему — прямой путь
        к '673 Maple Dr dd40' на месте RU-адреса.
        """
        if not replacement or not replacement.strip():
            return False
        if replacement.strip() == original.strip():
            return False  # совпадение с оригиналом — не анонимизация
        if len(replacement) != len(original):
            return False
        return _format_of(original) == _format_of(replacement)

    # ── Детерминированный fallback: повторить шаблон оригинала ────────

    @staticmethod
    def _normalize(word: str) -> str:
        """Токен для сверки с префиксами: нижний регистр, без точек."""
        return word.strip().lower().rstrip(".")

    @classmethod
    def _fallback_address(cls, value: str) -> str:
        """Подмена по токенам оригинала: меняются только содержательные части.

        Разделители, префиксы, порядок и длина токенов сохраняются. Функция
        детерминирована: seed = sha256(значение), поэтому один вход даёт один
        выход в любом процессе и в любом прогоне.
        """
        # seed — чистая функция от значения; встроенный hash() солится
        # PYTHONHASHSEED и давал разную подмену в разных прогонах (N9).
        seed = int.from_bytes(
            hashlib.sha256(value.encode("utf-8")).digest()[:8], "big"
        )
        rng = random.Random(seed)

        tokens = list(_TOKEN_RE.finditer(value))

        # Есть ли адресные префиксы — по ним решаем, уличный это адрес или
        # одиночное название (город/посёлок), и какой пул применять.
        has_address_prefix = any(
            cls._normalize(m.group("word") or "") in _PREFIX_TOKENS
            and (m.group("word") or "").strip(" .-")
            for m in tokens
        )

        out: List[str] = []
        last_prefix = ""          # последний служебный префикс перед словом
        for match in tokens:
            token = match.group(0)
            if match.group("num") is not None:
                out.append(cls._replace_number(token, rng))
            elif match.group("word") is not None:
                word = token
                replacement = cls._replace_word(
                    word, rng, has_address_prefix, last_prefix
                )
                out.append(replacement)
                if cls._normalize(word) in _PREFIX_TOKENS and replacement == word:
                    # префикс пережил подмену — он и есть каркас следующего слова
                    last_prefix = cls._normalize(word)
            else:
                # Разделители («, », точка, тире, пробел) — дословно
                out.append(token)

        return "".join(out)

    @classmethod
    def _replace_word(
        cls,
        word: str,
        rng: random.Random,
        structured: bool,
        last_prefix: str = "",
    ) -> str:
        """Подмена буквенного токена с сохранением длины, алфавита и регистра.

        Пул выбирается по каркасу: после «г.»/«обл.» — города, после
        «пр.»/«ш.» — названия в родительном падеже, после «ул.» — прилагательные.
        """
        norm = cls._normalize(word)

        # Префиксы («ул.», «д.», «офис», «кв.»), предлоги и одиночные буквы —
        # это и есть шаблон: оставляем дословно.
        if norm in _PREFIX_TOKENS or norm in _STOP_WORDS or len(norm) <= 1:
            return word

        # Двухбуквенные сокращения внутри адреса («ак.», «шт.») — тоже каркас:
        # подменять их нечем, а псевдослово на их месте портит вид строки.
        if structured and len(norm) <= 2:
            return word

        latin = word.isascii()

        # Порядок пулов: сначала «свой» по каркасу, затем остальные — лишь бы
        # подмена осталась топонимом, а не псевдословом.
        if last_prefix in _CITY_PREFIXES:
            pools = [_CITY_EN, _STREET_EN] if latin else [_CITY_RU, _STREET_RU]
        elif structured:
            if latin:
                pools = [_STREET_EN, _CITY_EN]
            elif last_prefix in _GENITIVE_PREFIXES:
                # «пр. Гагарина» читается лучше, чем «пр. Лесная»
                pools = [_STREET_RU_GEN, _STREET_RU, _CITY_RU]
            else:
                pools = [_STREET_RU, _CITY_RU]
        else:
            pools = [_CITY_EN, _STREET_EN] if latin else [_CITY_RU, _STREET_RU]

        for pool in pools:
            candidate = cls._pick_by_mask(word, pool, rng)
            if candidate is not None:
                return candidate

        # Дефисное название без пары целиком («Санкт-Петербург»): собираем из
        # частей той же длины — «Киров-Пятигорск» читается топонимом, а не шумом
        if "-" in word:
            parts = word.split("-")
            for pool in pools:
                picks = [cls._pick_by_mask(part, pool, rng) for part in parts]
                if any(picks):
                    return "-".join(
                        pick if pick else _substitute_runs(part, rng)
                        for pick, part in zip(picks, parts)
                    )

        # Нет ни одной пары из пула: побуквенная подмена прогонами. Алфавит и
        # регистр берутся из исходного символа, дефисы токена остаются на местах.
        return _substitute_runs(word, rng)

    @staticmethod
    def _pick_by_mask(
        word: str, pool: List[str], rng: random.Random
    ) -> Optional[str]:
        """Название из пула: та же длина и та же расстановка дефисов."""
        if not pool:
            return None
        target = len(word)
        dashes = [i for i, ch in enumerate(word) if ch == "-"]
        options = [
            name for name in pool
            if len(name) == target
            and [i for i, ch in enumerate(name) if ch == "-"] == dashes
            and name.lower() != word.lower()
        ]
        if not options:
            return None
        # Сортировка не нужна: пул — константа модуля, choice по rng детерминирован
        return _apply_case(word, rng.choice(options))

    @staticmethod
    def _replace_number(number: str, rng: random.Random) -> str:
        """Число той же разрядности; ведущие нули сохраняются, новые не появляются.

        '305' не должно превратиться в '036': ведущий ноль меняет разрядность
        номера с точки зрения формата адреса. Поэтому нулевой префикс
        воспроизводится посимвольно, а первая значимая цифра берётся из 1-9.
        """
        lead_zeros = len(number) - len(number.lstrip("0"))
        if lead_zeros == len(number):
            # Всё число — нули: менять нечего, оставляем как есть
            return number

        for _attempt in range(10):
            digits = "".join(rng.choice(_DIGITS) for _ in number)
            if lead_zeros:
                digits = "0" * lead_zeros + digits[lead_zeros:]
            elif len(digits) > 1:
                digits = rng.choice("123456789") + digits[1:]
            if digits != number:
                return digits

        # Запасной ход: сдвигаем последнюю цифру, не трогая нулевой префикс
        tail = len(number) - 1
        last = int(number[tail])
        return number[:tail] + str((last + 1) % 10)
