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
from cloaker.transformers.crm_status_transformer import CRMStatusTransformer
from cloaker.transformers.id_guardian_transformer import IDGuardianTransformer

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
    "crm_status": CRMStatusTransformer,
    "id_guardian": IDGuardianTransformer,
}


def get_transformer(transformer_type: str, config) -> BaseTransformer:
    """Get a transformer instance by type name."""
    cls = TRANSFORMER_MAP.get(transformer_type)
    if cls is None:
        raise ValueError(f"Unknown transformer type: {transformer_type}")
    return cls(config)
