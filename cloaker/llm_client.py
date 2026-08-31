"""LLM API client (OpenAI-compatible) — batch transformation requests with adaptive chunking."""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Dict, Any, List, Optional, Callable

from .config import CloakDBConfig


class LLMTimeoutError(RuntimeError):
    """API не ответил за отведённое время.

    Сообщение намеренно короткое и НЕ содержит команду curl: subprocess.
    TimeoutExpired в стандартном виде печатает всю команду целиком, включая
    заголовок 'Authorization: Bearer <ключ>' — то есть сливает секрет в логи.
    """


# ── Сохранение формата: инструкция модели и отбраковка ответа ───────────────

FORMAT_RULES = """IMPORTANT FORMATTING RULES — follow these to avoid rejection:
1. LENGTH: Free text (names, titles) — keep similar length (±30% OK). 
   Code/phone/email/dates — EXACTLY same length.
2. ALPHABET: If input is Cyrillic, output Cyrillic. If Latin, output Latin.
   (Exception: names can transliterate, but not codes/emails/phones)
3. SEPARATORS: Keep digits, letters, spaces, dots, dashes in same positions.
4. CASE: Preserve capitalization pattern (each word capitalized stays capitalized).
5. DOMAIN: For emails — keep domain exactly the same, change only local part.
6. PLACEHOLDERS: Never output NULL, empty string, 'N/A', '<anonymized>', '***'.
7. OUTPUT: Valid JSON only: {"original": "replacement", ...}"""

DATE_LIKE_RE = re.compile(r'^\d{2,4}[-/.]\d{1,2}[-/.]\d{1,2}')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PLACEHOLDER_RE = re.compile(
    r'^(?:n/?a|null|none|undefined|unknown|-+|_+|\*+|\.{3}|<[^>]*>|'
    r'аноним\w*|скрыт\w*|обфуск\w*|заглушк\w*)$', re.IGNORECASE)

# поле → сколько пар отбраковано (попадает в подсказку модели на следующем чанке)
_FORMAT_VIOLATIONS: Dict[str, int] = {}


def _has_cyrillic(v: str) -> bool:
    return any('\u0430' <= c.lower() <= '\u044f' for c in v)


def _is_mask_like(v: str) -> bool:
    """Код с фиксированной маской, а не свободный текст.

    У '+7 (912) 345-67-89', 'INV-TP-001', '770701001' позиция каждого символа —
    часть формата; у 'Руководитель закупок' позиции слов — просто текст.
    """
    if EMAIL_RE.match(v) or DATE_LIKE_RE.match(v):
        return True
    return any(c.isdigit() for c in v) and any(c in "-/. " for c in v)


def _is_code_like(v: str) -> bool:
    """Код фиксированной ширины (телефон, 'INV-TP-001', номер карты), а не адрес.

    Раздел один: у кода длина позиции — часть формата, у 'ул. Ленина, д. 45'
    длина названия улицы — просто длина названия. Критерий — длина самого
    длинного буквенного запуска: у кодов это аббревиатуры по 2-3 буквы
    (INV, TP, CR), у адресов и названий — полноценные слова.
    """
    runs = [m.group(0) for m in re.finditer(r'[^\W\d_]+', v, re.UNICODE)]
    return max((len(x) for x in runs), default=0) <= 3


def _skeleton(v: str) -> str:
    """Структурные символы по порядку — всё, что не буквы и не цифры."""
    return "".join(c for c in v if not c.isalnum())


def _digit_run_widths(v: str) -> List[int]:
    """Разряд каждой группы цифр: 'д. 45, кв. 3' -> [2, 1]."""
    return [len(m.group(0)) for m in re.finditer(r'\d+', v)]


# В названиях по-русски и по-английски служебные слова пишутся строчно; без
# исключения они ломали бы проверку регистра на честных заголовках
# ('Bohemian Rhapsody' -> 'Wave upon a Dream').
_PARTICLES = {
    "a", "an", "the", "of", "and", "or", "upon", "in", "on", "at", "to", "for",
    "de", "la", "le", "du", "del", "von", "van", "di", "da",
    "и", "в", "с", "на", "о", "у", "к", "от", "из", "для", "по", "но", "или",
}


def _case_violation(o: str, r: str):
    """Регистр значимых слов сохраняется, служебные слова — исключение."""
    wo = [w for w in re.split(r"\s+", o.strip()) if w]
    wr = [w for w in re.split(r"\s+", r.strip()) if w]
    if len(wo) != len(wr):
        return None            # число слов поехало — сравнивать регистр не с чем
    for a, b in zip(wo, wr):
        strip = ".,()!?:;-\u00ab\u00bb\"'"
        if a.strip(strip).lower() in _PARTICLES or b.strip(strip).lower() in _PARTICLES:
            continue
        if a[:1].isupper() != b[:1].isupper():
            return "регистр слов не сохранён"
    return None


def format_violation(original: str, replacement: str, field: str = ""):
    """Причина, по которой пару нельзя пускать в дамп, либо None.

    Пороги разные для трёх родов значений, потому что «формат» у них разный:
    у кода (телефон, 'INV-TP-001', индекс) позиция каждого символа — часть
    формата; у адреса фиксирована структура ('ул. X, д. N'), а не длина; у
    свободного текста (имена, жанры, названия) не зафиксировано почти ничего,
    кроме алфавита, регистра и разумной длины.
    """
    o, r = str(original), str(replacement)
    if not r.strip():
        return "замена пустая"
    if PLACEHOLDER_RE.match(r.strip()):
        return f"заглушка вместо данных ({r.strip()[:12]!r})"
    if r == o:
        return "значение не изменено"
    # Проверка алфавита ослаблена - для имен допускаем транслитерацию
    # (например, "Иван" -> "Ivan"), но сохраняем для email/phone/кодов
    if not (_has_cyrillic(o) or _has_cyrillic(r)):
        pass  # оба на латинице - ок
    elif _has_cyrillic(o) and _has_cyrillic(r):
        pass  # оба на кириллице - ок
    elif _is_mask_like(o) or EMAIL_RE.match(o) or DATE_LIKE_RE.match(o):
        # Для кодов/email/дат - алфавит менять нельзя
        return "сменён алфавит (кириллица<->латиница)"
    if EMAIL_RE.match(o):
        if not EMAIL_RE.match(r):
            return "замена не похожа на email"
        if o.rsplit('@', 1)[1] != r.rsplit('@', 1)[1]:
            return "изменён домен"
        return None if len(o) == len(r) else "длина email не сохранена"
    if DATE_LIKE_RE.match(o):
        if not DATE_LIKE_RE.match(r) or len(o) != len(r):
            return "сменён шаблон даты"
        if bool(re.search(r'\d\d:\d\d', o)) != bool(re.search(r'\d\d:\d\d', r)):
            return "время появилось или пропало"
        return None
    if o.isdigit():
        # Чистое число (почтовый индекс, код, сумма): разряд и есть формат,
        # ведущие нули теряются именно здесь ('012345' -> '4797').
        if not r.isdigit():
            return "число перестало быть числом"
        if len(o) != len(r):
            return f"разрядность {len(o)} -> {len(r)} (ведущие нули?)"
        return None
    if _is_mask_like(o):
        if not _is_code_like(o):
            # Адрес/объект с номером: структура обязательна, длина — нет.
            if _skeleton(o) != _skeleton(r):
                return "пропущен структурный символ адреса"
            if _digit_run_widths(o) != _digit_run_widths(r):
                return "разрядность номеров не сохранена"
            return None
        if len(o) != len(r):
            return f"длина маски {len(o)} -> {len(r)}"
        for a, b in zip(o, r):
            if a.isalnum() != b.isalnum():
                return "буква стала разделителем (или наоборот)"
            if a.isdigit() != b.isdigit():
                return "цифра стала буквой (или наоборот)"
            # Разделитель — часть формата, а не «любой не-буквенный символ»:
            # 'INV-TP-001' -> 'INV_TP_001' перестаёт читаться парсерами кода.
            if not a.isalnum() and a != b:
                return f"разделитель {a!r} -> {b!r}"
        return None
    # Свободный текст (имена, названия): проверяем только алфавит и разумную длину.
    # Ослаблено: LLM может менять длину, главное - тот же алфавит и не слишком короткое/длинное
    lo, hi = max(1, len(o) // 3), len(o) * 3 + 10  # ±200% вместо ±100%
    if not (lo <= len(r) <= hi):
        return f"длина свободного текста {len(o)} -> {len(r)}"
    return _case_violation(o, r)


def _format_hint(field: str) -> str:
    """Обратная связь модели: на этом же поле уже ломали формат."""
    n = _FORMAT_VIOLATIONS.get(field, 0)
    if not n:
        return ""
    return (f"NOTE: on the previous chunk of this same field, {n} of your "
            f"replacements were rejected for breaking the original format. "
            f"Re-read rules 1-6: match length, alphabet and separators exactly.")


class LLMClient:
    """Call an OpenAI-compatible LLM /chat/completions API for batch data generation.

    Chunking is driven by GENERATION TIME, not prompt size: gateways such as ofox
    drop the connection at ~60s (curl rc=52), so a chunk must finish well inside
    that window. Chunks are capped by value count (see max_values_per_chunk) and
    are halved-and-retried whenever one comes back empty.
    """

    def __init__(self, config: CloakDBConfig) -> None:
        self.config = config
        self._call_count = 0
        self._last_call = 0.0
        # Chunking parameters for large payloads
        self.chunk_max_chars = 2500       # Approx chars per sub-request
        # ЖЁСТКИЙ лимит на число значений в одном запросе. Узкое место — не размер
        # промпта, а длительность генерации. Замеры на реальном пути (ofox, чанк из
        # реального дампа, max_tokens=32768):
        #   qwen3.8-flash  20 значений → 13.3s и 20.5s, обе попытки полные (20/20)
        #   qwen3.5-flash  20 значений → 54.2s; 40 значений → 179.1s лишь благодаря
        #                  авто-делению (сам запрос не возвращается: ofox обрывает
        #                  коннект на ~60s, curl rc=52)
        #   40 значений на 3.8-flash → 158.7s, тоже только через деление
        # Отсюда 20 — не «на глаз», а половина того, что 3.5-flash успевает за стену.
        # Пустой ответ лечится по-разному в зависимости от ПРИРОДЫ (см. _classify_empty),
        # и дробление помогает лишь в одном из двух случаев.
        self.max_values_per_chunk = 20
        self.max_split_depth = 3          # 20 -> 10 -> 5 -> 2
        # Потолок бюджета генерации при разовом расширении (см. EMPTY_BUDGET).
        # Выше 64k модель уже не «думает», а конфигурация большинства шлюзов
        # отказывает. Ставим с запасом: обрезка ответа стоит дороже лишней генерации.
        self.max_tokens_ceiling = 65536
        self._last_empty_kind: Optional[str] = None
        # Бюджет времени считается АДАПТИВНО под каждый запрос (_chunk_timeout):
        # reasoning-модели тратят скрытые reasoning-токены из того же max_tokens,
        # поэтому чанк на ~100 значений может идти минуты, а не десятки секунд.
        self.timeout_base = max(config.llm.timeout_base, 5)
        self.timeout_max = max(config.llm.timeout_max, self.timeout_base)
        self.timeout_per_value = 2.0      # сек на одно значение в чанке
        self.timeout_per_kb = 8.0         # сек на 1KB промпта

    def _safe_rate_limit(self) -> None:
        """Minimum interval between calls."""
        if self._last_call > 0:
            elapsed = time.time() - self._last_call
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

    EMPTY_GATEWAY = "gateway"   # обрыв коннекта / rc!=0 / нет choices → лечить дроблением
    EMPTY_BUDGET = "budget"     # finish_reason=length → лечить бюджетом, не чанком

    def _classify_empty(self, response_data: Dict[str, Any], content: str) -> None:
        """Запомнить ПРИРОДУ пустого ответа: от неё зависит способ лечения.

        Различение обязательно: раньше обе причины давали `{}`, и клиент начинал
        делить чанк пополам. Дробление при исчерпанном бюджете генерации бессильно
        (каждый осколок съедает те же скрытые токены reasoning) и лишь сжигает по
        запросу на каждый уровень глубины: замер 20 значений при max_tokens=2048
        дал 4 запроса, 150.7 с и 13/20 значений вместо одного точного повтора.
        """
        self._last_empty_kind = self.EMPTY_GATEWAY
        if content:
            return
        choices = response_data.get("choices") or []
        finish = (choices[0].get("finish_reason") or "").lower() if choices else ""
        if finish == "length":
            self._last_empty_kind = self.EMPTY_BUDGET
            details = ((response_data.get("usage") or {})
                       .get("completion_tokens_details") or {})
            import sys
            print(f"  [WARN] пустой ответ: finish_reason=length, "
                  f"reasoning-токенов={details.get('reasoning_tokens')} — бюджет "
                  f"max_tokens съеден reasoning'ом, размер чанка ни при чём", file=sys.stderr)

    def _build_payload(self, prompt: str, system_prompt: str = "",
                       max_tokens: Optional[int] = None) -> dict:
        """Build the JSON payload dict for a single API call."""
        payload = {
            "model": self.config.llm.model,
            "max_tokens": max_tokens or self.config.llm.max_tokens,
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload["messages"] = messages
        return payload

    def _chunk_timeout(self, n_values: int, prompt_len: int) -> int:
        """Таймаут одного запроса, сек, по реальному объёму работы.

        Замена прежнего `base + chunk_index * 5`: масштабирование по ИНДЕКСУ
        чанка голодало самый первый (и самый большой) чанок каждой колонки —
        он стабильно падал на 30-секундном лимите.
        """
        est = (
            self.timeout_base
            + n_values * self.timeout_per_value
            + (prompt_len / 1000.0) * self.timeout_per_kb
        )
        return int(max(self.timeout_base, min(est, self.timeout_max)))

    def _call_chunk_with_split(self, chunk: List[str], build_prompt_fn: Callable,
                               index: int = 0, total: int = 1,
                               depth: int = 0,
                               field: str = "") -> Dict[str, str]:
        """Один чанк с рекурсивным дроблением при пустом ответе.

        Пустой ответ на длинном запросе почти всегда означает не «модель не смогла»,
        а обрыв соединения на стороне гейтвея (упор в ~60s). Такой чанк делится
        пополам и повторяется, пока не упрётся в разумный минимум или потолок
        глубины. Это и есть реальная «адаптивность» — прежняя адаптивность по
        символам от этого не защищала.
        """
        prompt, system_prompt = build_prompt_fn(chunk)
        # Правила формата — в system каждой задачи. Часть билдеров (value_replacement,
        # phone, postal, email) вообще не передавала system prompt, и требование
        # формата до модели не доходило.
        system_prompt = system_prompt or ""
        system_prompt = FORMAT_RULES + (("\n\n" + system_prompt) if system_prompt else "")
        hint = _format_hint(field)
        if hint:
            system_prompt += "\n\n" + hint
        timeout = self._chunk_timeout(len(chunk), len(prompt))
        label = f"chunk {index}/{total}" if not depth else f"{index}/{total}.d{depth}"

        try:
            result = self._call_single(prompt, system_prompt, timeout=timeout)
        except LLMTimeoutError as e:
            print(f"  [WARN] {label}: {e}")
            result = {}

        # Пусто по причине бюджета — сначала ОДИН повтор того же чанка с расширенным
        # max_tokens. Делить пополам будем только если и он не принёс результата.
        if not result and self._last_empty_kind == self.EMPTY_BUDGET:
            bigger = min(max(self.config.llm.max_tokens, 8192) * 2,
                         self.max_tokens_ceiling)
            if bigger > self.config.llm.max_tokens:
                print(f"      ⤷ {label}: обрезано по limit — повторяю тот же чанк "
                      f"({len(chunk)} знач.) с max_tokens={bigger}")
                try:
                    result = self._call_single(prompt, system_prompt,
                                               timeout=timeout, max_tokens=bigger)
                except LLMTimeoutError as e:
                    print(f"  [WARN] {label}: {e}")
                    result = {}

        if result or depth >= self.max_split_depth or len(chunk) <= 3:
            if depth == 0 and total > 1:
                print(f"  [{label}] {len(chunk)} значений → {len(result)} записей")
            return result

        mid = len(chunk) // 2
        print(f"      ⤷ {label}: пусто при {len(chunk)} значениях — делю пополам "
              f"(глубина {depth + 1})")
        left = self._call_chunk_with_split(chunk[:mid], build_prompt_fn, index, total,
                                           depth + 1, field=field)
        right = self._call_chunk_with_split(chunk[mid:], build_prompt_fn, index, total,
                                            depth + 1, field=field)
        merged = dict(left)
        merged.update(right)
        return merged

    def _execute_curl(self, payload: dict, timeout: int) -> Dict[str, Any]:
        """Execute a single curl subprocess call. Returns {} on any error."""
        cmd = [
            "curl", "-s", "-X", "POST",
            f"{self.config.llm.endpoint}/chat/completions",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {self.config.llm.api_key}",
            "-d", json.dumps(payload),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise LLMTimeoutError(f"API timeout after {timeout}s") from None
        
        if result.returncode != 0:
            return {}
        
        try:
            response_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        
        choices = response_data.get("choices", [])
        if not choices:
            return {}
        
        content = choices[0].get("message", {}).get("content") or ""
        self._classify_empty(response_data, content)
        
        # Debug: log raw response for failed extractions
        if not content:
            import sys
            print(f"  [DEBUG] Empty content in API response", file=sys.stderr)
            print(f"  [DEBUG] Full response: {result.stdout[:500]}", file=sys.stderr)
        
        return json.loads(json.dumps(content)) if isinstance(content, str) else {}

    def _retry_once(self, payload: dict, timeout: int) -> Optional[str]:
        """Retry a failed/empty response once (common with qwen models)."""
        time.sleep(1.5)
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 f"{self.config.llm.endpoint}/chat/completions",
                 "-H", "Content-Type: application/json",
                 "-H", f"Authorization: Bearer {self.config.llm.api_key}",
                 "-d", json.dumps(payload)],
                capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise LLMTimeoutError(f"API timeout after {timeout}s (retry)") from None
        if result.returncode != 0:
            return ""
        try:
            response_data = json.loads(result.stdout)
            choices = response_data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content") or ""
                self._classify_empty(response_data, content)
                return content
        except (json.JSONDecodeError, KeyError):
            pass
        return ""

    def _call_single(self, prompt: str, system_prompt: str = "",
                     timeout: Optional[int] = None,
                     max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Make ONE API call with rate limiting and retry logic.

        max_tokens переопредляется только повтором с расширенным бюджетом
        (см. _classify_empty / EMPTY_BUDGET).
        """
        self._safe_rate_limit()
        self._call_count += 1
        self._last_call = time.time()
        self._last_empty_kind = None

        # Явный timeout не задан — считаем по размеру промпта.
        if timeout is None:
            timeout = self._chunk_timeout(0, len(prompt))

        payload = self._build_payload(prompt, system_prompt, max_tokens=max_tokens)
        
        content = self._execute_curl(payload, timeout)
        
        # If empty, retry once. Повтор получает увеличенный бюджет: идентичный
        # запрос с тем же таймаутом повторил бы ровно тот же timeout.
        if not content:
            content = self._retry_once(
                payload, min(int(timeout * 1.5), self.timeout_max)
            )

        if not content:
            return {}  # Safe fallback
        
        extracted = self._extract_json(content)
        
        # Debug logging for empty extractions
        if not extracted and content:
            import sys
            print(f"  [DEBUG] Empty extraction for response length {len(content)}", file=sys.stderr)
            print(f"  [DEBUG] Response snippet: {content[:300]}...", file=sys.stderr)
        
        return extracted

    def _estimate_chars(self, items: List[str]) -> int:
        """Estimate total character count for an API prompt."""
        base_overhead = 400  # System prompt + metadata overhead per call
        item_cost = 80       # Average chars per item in formatted prompt
        return base_overhead + len(items) * item_cost

    def _split_by_budget(self, samples: List[str], max_chars: int = None,
                         max_values_per_chunk: int = None) -> List[List[str]]:
        """Split samples into chunks based on both size AND value count limits.

        Лимит по числу значений первичен: запрос должен успеть сгенерироваться до
        обрыва соединения на стороне гейтвея (~60s у ofox), а не влезть в контекст.

        Args:
            samples: List of unique values to transform
            max_chars: Char budget per chunk (optional, uses estimate_chars default)
            max_values_per_chunk: Hard cap on values per chunk
                (default: self.max_values_per_chunk)
        """
        if not samples:
            return []

        if max_values_per_chunk is None:
            max_values_per_chunk = self.max_values_per_chunk

        chunks = []
        current_chunk = []
        current_estimated = self._estimate_chars([])  # Start with base overhead
        char_budget = max_chars or self.chunk_max_chars * 4  # Allow larger char budget since values are short
        
        for s in samples:
            item_est = 80 + len(s.encode('utf-8'))  # Format overhead + raw value
            # Split if adding this item would exceed char budget OR value count cap
            if (current_estimated + item_est > char_budget or len(current_chunk) >= max_values_per_chunk) and current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_estimated = self._estimate_chars([])
            current_chunk.append(s)
            current_estimated += item_est
        
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks

    def _split_into_chunks(self, samples: List[str], max_chars: int = None) -> List[List[str]]:
        """Legacy method — delegates to _split_by_budget."""
        return self._split_by_budget(samples, max_chars)

    def _merge_chunk_results(self, chunk_results: List[Dict[str, str]]) -> Dict[str, str]:
        """Merge multiple chunk mappings into one unified dict."""
        merged = {}
        for chunk_map in chunk_results:
            if isinstance(chunk_map, dict):
                merged.update(chunk_map)
        return merged

    def _call_api_chunked(
        self,
        build_prompt_fn: Callable[[List[str]], tuple],
        samples: List[str],
        stats: Optional[Dict] = None,
        field_key: str = "",
    ) -> Dict[str, str]:
        """Chunk a large sample list into smaller API calls and merge results.
        
        Args:
            build_prompt_fn: callable(samples) -> (prompt_str, system_prompt_str)
            samples: List of unique values to transform
            stats: Optional column statistics for context
        
        Returns:
            Merged {original: replacement} dict from all chunks
        """
        # Один путь для всех случаев: режем на чанки и идём по ним. Раньше
        # колонки до 150 значений уходили ОДНИМ запросом — на гейтвее с обрезкой
        # коннекта на 60s это гарантированно означало потерянную колонку целиком.
        # РАМКИ ОБЪЁМА ЗДЕСЬ НЕТ — это намеренно. Задача движка — один раз
        # прогнать через API ВСЕ уникальные значения поля, слить их в
        # global_mapping.json и потом на стриминге только читать маппинг. Любое
        # усечение здесь означало бы, что часть значений вообще не поменяется
        # (transform() оставляет оригинал, если значения нет ни в маппинге, ни в
        # пуле) — то есть персональные данные остались бы в выходе. Долго — ок.
        chunks = self._split_by_budget(samples)
        if not chunks:
            return {}

        print(f"      🔄 Calling LLM ({len(samples)} values → {len(chunks)} chunk(s)"
              f" × ≤{self.max_values_per_chunk})...")

        all_results = []
        for i, chunk in enumerate(chunks):
            all_results.append(
                self._call_chunk_with_split(chunk, build_prompt_fn, i + 1, len(chunks),
                                            field=field_key)
            )
            
            # Brief pause between chunks to avoid rate limits
            if i < len(chunks) - 1:
                time.sleep(0.5)
        
        merged = self._merge_chunk_results(all_results)
        kept, dropped = self._check_format(merged, field_key)
        if dropped:
            print(f"      ⚠️  по формату отбраковано {len(dropped)}/{len(merged)} — "
                  f"значения уйдут в локальный генератор, который формат держит")
            for orig, repl, why in dropped[:5]:
                print(f"         • {orig[:24]!r} → {repl[:24]!r}: {why}")
        print(f"  Chunked merge: {len(kept)} записей из {len(merged)} "
              f"({len(chunks)} chunk(s))")
        return kept

    @staticmethod
    def _check_format(mapping: Dict[str, str], field: str = "") -> tuple:
        """Прогнать маппинг через правила формата: (оставшие, [(ориг, замена, почему)])."""
        kept: Dict[str, str] = {}
        dropped = []
        for original, replacement in mapping.items():
            if not isinstance(original, str) or not isinstance(replacement, str):
                dropped.append((str(original)[:24], str(replacement)[:24], "не строка"))
                continue
            why = format_violation(original, replacement, field)
            if why:
                dropped.append((original, replacement, why))
            else:
                kept[original] = replacement
        if dropped and field:
            _FORMAT_VIOLATIONS[field] = _FORMAT_VIOLATIONS.get(field, 0) + len(dropped)
        return kept, dropped

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Extract JSON object from LLM response text.
        
        Uses multiple fallback strategies to extract valid JSON,
        since LLMs sometimes return malformed or wrapped responses.
        """
        if not text:
            return {}
        text = text.strip()
        
        # Strategy 1: Already valid JSON?
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        
        # Strategy 2: Extract from markdown code blocks
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue
        
        # Strategy 3: Find first {...} block (handles extra text before/after)
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        
        # Strategy 4: Try to fix common JSON issues
        # Remove trailing commas before }/\]
        fixed = re.sub(r',\s*([}\]])', r'\1', text)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
        
        # Strategy 5: Single quotes to double quotes
        try:
            single_to_double = fixed.replace("'", '"')
            return json.loads(single_to_double)
        except json.JSONDecodeError:
            pass
        
        # Strategy 6: Try to extract key-value pairs manually
        kv_pattern = re.compile(r'"([^"]+)"\s*:\s*"([^"]+)"')
        matches = kv_pattern.findall(text)
        if len(matches) > 1:
            return dict(matches)
        
        # Debug: if extraction failed, log the text
        import sys
        print(f"  [DEBUG] JSON extraction failed from text of length {len(text)}", file=sys.stderr)
        print(f"  [DEBUG] First 400 chars: {text[:400]}", file=sys.stderr)
        
        return {}  # Return empty dict rather than raising - caller handles gracefully

    # ── Public API Methods ────────────────────────────────────────
    # Each method wraps a callback-based prompt builder for chunking support.

    def generate_name_mapping(
        self,
        field_key: str,
        samples: List[str],
        description: str,
        stats: Optional[Dict] = None,
    ) -> Dict[str, str]:
        """Generate replacement mapping for names via LLM (supports chunking).
        
        Passes ALL unique values — will auto-chunk if needed for large cardinality.
        """
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            system_prompt = (
                "You are a professional data anonymization expert. "
                "Generate realistic anonymized replacements for each name. "
                "IMPORTANT: Preserve the same ALPHABET (Latin stays Latin, Cyrillic stays Cyrillic). "
                "Keep name length similar (±30%). "
                "Match the case pattern (first letters capitalized). "
                "All output must be valid JSON only, no explanation text."
            )
            prompt = f"""Field: {field_key}
Description: {description}

Original values ({len(chunk_samples)} total):
{sample_str}

Replace each name with a realistic alternative name. 
- Keep same alphabet (if original is Cyrillic, output Cyrillic; if Latin, output Latin)
- Keep similar length (±30% is OK)
- Preserve case pattern (first letter capitalized)
- Return ONLY valid JSON: {{"original": "replacement", ...}}
Example: {{"John Smith": "James Anderson", "Иван Петров": "Алексей Сидоров"}}"""
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, stats, field_key=field_key)

    def generate_value_replacement(
        self,
        field_key: str,
        samples: List[str],
        description: str,
    ) -> Dict[str, str]:
        """Generic value replacement for genre/media_type/playlist names (chunked).
        
        Passes ALL unique values — will auto-chunk if needed for large cardinality.
        """
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Description: {description}

Original values ({len(chunk_samples)} total):
{sample_str}

Replace each value with a realistic alternative. Keep similar length (±30%).
Return JSON: {{"original": "replacement", ...}}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "Preserve the same alphabet and case pattern. "
                "Keep length similar (±30% for text). Return valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, field_key=field_key)

    def generate_phone_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate phone number mappings preserving format (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = "\n".join(f"- {s}" for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each phone number with another realistic phone number.
- Keep EXACTLY same format (brackets, dashes, spaces, country code)
- Same length as original
- Return JSON: {{"original": "replacement", ...}}
Phones:
{sample_str}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "Phone format must be EXACT: same number of digits, same separators, same country code. "
                "Output valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, field_key=field_key)

    def generate_address_mapping(
        self,
        field_key: str,
        samples: List[str],
        stats: Optional[Dict] = None,
    ) -> Dict[str, str]:
        """Generate address mappings preserving structure (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = "\n".join(f'- "{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each address with a realistic alternative.
- Keep same structure (street, city, state, postal code)
- Preserve separators and case
- Return JSON: {{"original": "replacement", ...}}
Addresses:
{sample_str}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "Preserve address structure: same number of parts, same separators, same case pattern. "
                "Output valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, stats, field_key=field_key)

    def generate_email_mapping(
        self,
        field_key: str,
        samples: List[str],
        domain_stats: Optional[Dict[str, int]] = None,
    ) -> Dict[str, str]:
        """Generate email mappings preserving domain distribution (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            domain_info = ""
            if domain_stats:
                top_domains = dict(list(domain_stats.items())[:5])
                domain_info = f"\nTop domains to preserve: {top_domains}"
            prompt = f"""Field: {field_key}
Generate replacement emails.{domain_info}
- Keep EXACT same domain (e.g., @gmail.com stays @gmail.com)
- Change only the local part before @
- Same length as original email
- Return JSON: {{"original": "replacement", ...}}
Emails: {sample_str}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "CRITICAL: Domain must be EXACTLY the same (case-sensitive). "
                "Only change the part before @. Output valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, field_key=field_key)

    def generate_title_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate job title mappings (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each job title with another realistic title at similar level.
- Keep similar length (±30%)
- Preserve case pattern (each word capitalized)
- Return JSON: {{"original": "replacement", ...}}
Titles: {sample_str}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "Preserve alphabet and case pattern. Keep length similar. Output valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, field_key=field_key)

    def generate_company_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate company name mappings (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each company name with a realistic alternative.
- Keep similar length (±30%)
- Preserve case pattern
- Return JSON: {{"original": "replacement", ...}}
Companies: {sample_str}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "Preserve alphabet and case pattern. Keep length similar. Output valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, field_key=field_key)

    def generate_postal_code_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate postal code mappings preserving format (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = "\n".join(f'- "{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each postal code with another valid code of the same format.
- Keep EXACTLY same length
- Keep same separators (space, dash, none)
- Return JSON: {{"original": "replacement", ...}}
Codes:
{sample_str}"""
            system_prompt = (
                "You are a data anonymization expert. "
                "CRITICAL: Postal code must be EXACTLY same length and format. "
                "Output valid JSON only."
            )
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, field_key=field_key)
