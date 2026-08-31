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
        # промпта, а длительность генерации: гейтвей ofox обрывает коннект на ~60s
        # (замерено: 20 значений ≈ 26s успешно, 40+ ≈ curl rc=52 empty reply).
        # Поэтому чанки маленькие, плюс авто-деление пополам при пустом ответе.
        self.max_values_per_chunk = 20
        self.max_split_depth = 3          # 20 -> 10 -> 5 -> 2
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

    def _build_payload(self, prompt: str, system_prompt: str = "") -> dict:
        """Build the JSON payload dict for a single API call."""
        payload = {
            "model": self.config.llm.model,
            "max_tokens": self.config.llm.max_tokens,
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
                               depth: int = 0) -> Dict[str, str]:
        """Один чанк с рекурсивным дроблением при пустом ответе.

        Пустой ответ на длинном запросе почти всегда означает не «модель не смогла»,
        а обрыв соединения на стороне гейтвея (упор в ~60s). Такой чанк делится
        пополам и повторяется, пока не упрётся в разумный минимум или потолок
        глубины. Это и есть реальная «адаптивность» — прежняя адаптивность по
        символам от этого не защищала.
        """
        prompt, system_prompt = build_prompt_fn(chunk)
        timeout = self._chunk_timeout(len(chunk), len(prompt))
        label = f"chunk {index}/{total}" if not depth else f"{index}/{total}.d{depth}"

        try:
            result = self._call_single(prompt, system_prompt, timeout=timeout)
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
                                           depth + 1)
        right = self._call_chunk_with_split(chunk[mid:], build_prompt_fn, index, total,
                                            depth + 1)
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
                return choices[0].get("message", {}).get("content") or ""
        except (json.JSONDecodeError, KeyError):
            pass
        return ""

    def _call_single(self, prompt: str, system_prompt: str = "",
                     timeout: Optional[int] = None) -> Dict[str, Any]:
        """Make ONE API call with rate limiting and retry logic."""
        self._safe_rate_limit()
        self._call_count += 1
        self._last_call = time.time()

        # Явный timeout не задан — считаем по размеру промпта.
        if timeout is None:
            timeout = self._chunk_timeout(0, len(prompt))

        payload = self._build_payload(prompt, system_prompt)
        
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
        chunks = self._split_by_budget(samples)
        if not chunks:
            return {}

        print(f"      🔄 Calling LLM ({len(samples)} values → {len(chunks)} chunk(s)"
              f" × ≤{self.max_values_per_chunk})...")

        all_results = []
        for i, chunk in enumerate(chunks):
            all_results.append(
                self._call_chunk_with_split(chunk, build_prompt_fn, i + 1, len(chunks))
            )
            
            # Brief pause between chunks to avoid rate limits
            if i < len(chunks) - 1:
                time.sleep(0.5)
        
        merged = self._merge_chunk_results(all_results)
        print(f"  Chunked merge: {len(merged)} total entries from {len(chunks)} chunks")
        return merged

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
                "Generate realistic anonymized replacements for each name provided. "
                "All output must be valid JSON only, no explanation text."
            )
            prompt = f"""Field: {field_key}
Description: {description}

Original values ({len(chunk_samples)} total):
{sample_str}

Replace each of these names with a realistic, culturally appropriate alternative name.
Return a JSON object where keys are the original names and values are the new names.
Example: {{"John Smith": "James Anderson", "Jane Doe": "Sarah Williams"}}"""
            return prompt, system_prompt
        
        return self._call_api_chunked(build_prompt, samples, stats)

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

Replace each value with a realistic alternative for this type of data.
Return JSON: {{"original": "replacement", ...}}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples)

    def generate_phone_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate phone number mappings preserving format (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = "\n".join(f"- {s}" for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each phone number with another realistic phone number of the same format.
Return JSON: {{"original_phone": "new_phone", ...}}
Phones:
{sample_str}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples)

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
Replace each address with a realistic alternative address.
Preserve the general structure but change street names, cities, etc.
Return JSON: {{"original": "new", ...}}
Addresses:
{sample_str}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples, stats)

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
                domain_info = f"\nTop domains to preserve distribution across: {top_domains}"
            prompt = f"""Field: {field_key}
Generate realistic replacement emails preserving domain distribution.{domain_info}
Return JSON: {{"original_email": "new_email", ...}}
Emails: {sample_str}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples)

    def generate_title_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate job title mappings (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each job title with another realistic job title at a similar level.
Return JSON: {{"original_title": "new_title", ...}}
Titles: {sample_str}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples)

    def generate_company_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate company name mappings (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = ", ".join(f'"{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each company name with a realistic alternative company name.
Return JSON: {{"original_company": "new_company", ...}}
Companies: {sample_str}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples)

    def generate_postal_code_mapping(
        self,
        field_key: str,
        samples: List[str],
    ) -> Dict[str, str]:
        """Generate postal code mappings preserving format (chunked)."""
        
        def build_prompt(chunk_samples):
            sample_str = "\n".join(f'- "{s}"' for s in chunk_samples)
            prompt = f"""Field: {field_key}
Replace each postal code with another valid postal code of the same format.
Return JSON: {{"original": "new", ...}}
Codes:
{sample_str}"""
            return prompt, ""
        
        return self._call_api_chunked(build_prompt, samples)
