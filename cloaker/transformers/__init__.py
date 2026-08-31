"""Transformer registry — import all transformers here for auto-discovery."""

from cloaker.base_transformer import BaseTransformer
from cloaker.transformers.name_transformer import NameTransformer
from cloaker.transformers.email_transformer import EmailTransformer
from cloaker.transformers.phone_transformer import PhoneTransformer
from cloaker.transformers.address_transformer import AddressTransformer
from cloaker.transformers.date_transformer import DateShuffleTransformer
from cloaker.transformers.title_transformer import TitleTransformer
from cloaker.transformers.company_transformer import CompanyTransformer
from cloaker.transformers.genre_transformer import GenreTransformer
from cloaker.transformers.composer_transformer import ComposerTransformer
from cloaker.transformers.postal_code_transformer import PostalCodeTransformer
import inspect
import re
import warnings

from cloaker.transformers.skip_transformer import SkipTransformer

TRANSFORMER_MAP = {
    "name": NameTransformer,
    "email": EmailTransformer,
    "phone": PhoneTransformer,
    "address": AddressTransformer,
    "date_shuffle_month": DateShuffleTransformer,
    "date_shuffle_year": DateShuffleTransformer,
    "title": TitleTransformer,
    "company": CompanyTransformer,
    "genre": GenreTransformer,
    "composer": ComposerTransformer,
    "postal_code": PostalCodeTransformer,
    # Явный passthrough: поля, которые намеренно не меняются (деньги, примечания).
    "skip": SkipTransformer,
}


def get_transformer(transformer_type: str, config, **kwargs) -> BaseTransformer:
    """Get a transformer instance by type name.

    Имя правила может нести гранулярность: в TRANSFORMER_MAP на один класс
    DateShuffleTransformer ведут ключи date_shuffle_month и date_shuffle_year.
    Раньше вызов ``cls(config)`` терял суффикс, и трансформер угадывал scope по
    имени колонки — из-за этого date_shuffle_year вёл себя как month (дефект N11).
    Теперь суффикс доезжает до конструктора, если класс принимает scope.
    """
    cls = TRANSFORMER_MAP.get(transformer_type)
    if cls is None:
        raise ValueError(f"Unknown transformer type: {transformer_type}")

    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):        # класс без интроспекции сигнатуры
        params = {}
    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def accepts(name: str) -> bool:
        return name in params or (has_var_kw and name not in params and False)

    filtered = {k: v for k, v in kwargs.items() if accepts(k)}
    dropped = [k for k in kwargs if k not in filtered]
    if dropped:
        # Молчаливый пропуск параметра — будущая «исчезающая» настройка, как раз
        # так и потерялся scope. Пусть будет видно, что класс его не принимает.
        warnings.warn(f"{cls.__name__} не принимает {', '.join(dropped)} — параметр игнорируется")

    if accepts("scope") and "scope" not in filtered:
        m = re.match(r'^\w*_shuffle_(month|year|days)$', transformer_type)
        if m:
            filtered["scope"] = m.group(1)

    return cls(config, **filtered)
