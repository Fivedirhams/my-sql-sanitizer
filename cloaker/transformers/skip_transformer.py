"""Skip transformer — явный no-op для полей, которые не надо менять.

В config.yaml для money/notes/description-полей используется значение `skip`.
Раньше такого типа в реестре не было, и каждое такое поле при каждом прогоне
выдавало `[WARN] Failed to load ...: ValueError: Unknown transformer type: skip`
— то есть поле молча выпадало из обработки, а логи замусоривались.

Семантика: значение возвращается как есть (passthrough), маппинг не строится.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cloaker.base_transformer import BaseTransformer


class SkipTransformer(BaseTransformer):
    """Ничего не делает: оставляет исходное значение без изменений."""

    def __init__(self, config) -> None:
        super().__init__(config)
        # Ни одного маппинга не нужно — поля `skip` не анонимизируются.
        self._mapping: Dict[str, str] = {}

    def transform(self, value: Optional[str], table: str, column: str) -> Optional[str]:
        return value

    def type_name(self) -> str:
        return "skip"

    def _load_mapping(self, samples: List[Dict[str, Any]], field_key: str,
                      stats: Dict[str, Any]) -> None:
        # Намеренно пусто: passthrough-полю не нужен ни LLM-запрос, ни пул значений.
        return
