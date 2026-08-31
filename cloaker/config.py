"""Configuration loader — YAML config + environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any


# ── Provider presets ────────────────────────────────────
# Всё OpenAI-совместимо; достаточно указать провайдер (или свой endpoint) в .env.
PROVIDER_ENDPOINTS: Dict[str, str] = {
    "ofox": "https://api.ofox.ai/v1",
    "kodik": "https://api.kodikrouter.ru/v1",
    "kodikrouter": "https://api.kodikrouter.ru/v1",
    "openai": "https://api.openai.com/v1",
}
PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "ofox": "bailian/qwen3.8-flash",
    "kodik": "qwen/qwen3.7-flash",
    "kodikrouter": "qwen/qwen3.7-flash",
    "openai": "gpt-4o-mini",
}


@dataclass
class LLMConfig:
    provider: str = "ofox"
    api_key: str = ""
    model: str = "bailian/qwen3.8-flash"
    endpoint: str = "https://api.ofox.ai/v1"
    max_tokens: int = 32768  # Increased from 4096 to support ~150 values per API call
    timeout_base: int = 45   # нижняя граница таймаута одного запроса, сек (env: LLM_TIMEOUT_BASE)
    timeout_max: int = 180   # потолок таймаута самого крупного чанка, сек (env: LLM_TIMEOUT_MAX)


@dataclass
class ProcessingConfig:
    sample_limit: int = 50  # уникальных значений на колонку для профилирования (env: SAMPLES_PER_FIELD)


@dataclass
class FieldRule:
    """A single field transformation rule."""
    name: str  # e.g. 'FirstName', 'LastName'
    table_name: str  # parent table (e.g. 'Customer')
    transformer_type: str  # 'name', 'address', 'email', etc.
    profile_key: str  # e.g. 'Customer_FirstName'


@dataclass
class DatabaseProfile:
    schema: str = "public"
    tables: Dict[str, list] = field(default_factory=dict)


@dataclass
class CloakDBConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    transform_rules: Dict[str, str] = field(default_factory=dict)  # {"Table.Column": "type"}
    db_profile: DatabaseProfile = field(default_factory=DatabaseProfile)
    profiles_dir: str = "output/profiles"  # env: PROFILES_DIR


def _parse_yaml_simple(path: str) -> Dict[str, str]:
    """Lightweight YAML parser for our simple key-value transform config."""
    rules = {}
    try:
        with open(path) as f:
            in_transforms = False
            
            for line in f:
                stripped = line.strip()
                
                # Skip comments and empty lines
                if not stripped or stripped.startswith('#'):
                    continue
                
                # Detect transforms section start
                if stripped == 'transforms:':
                    in_transforms = True
                    continue
                
                # Exit transforms section when we hit another top-level section
                if in_transforms and (stripped.startswith('processing:') or stripped.startswith('output:') or stripped.startswith('# ===')):
                    in_transforms = False
                    continue
                
                if not in_transforms:
                    continue
                
                # Parse "Table.Column: type" or just "Column: type" lines
                if ':' in stripped and not stripped.startswith('-'):
                    key, _, value = stripped.partition(':')
                    key = key.strip().split('#')[0].strip()  # Strip comments inline
                    value = value.strip().split('#')[0].strip()
                    if key and value:
                        # Accept both Table.Column and standalone Column patterns
                        rules[key] = value
    except Exception:
        pass
    
    return rules


def load_env() -> None:
    """Load .env file if it exists."""
    dotenv_path = Path(__file__).parent.parent / ".env"
    if dotenv_path.exists():
        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")


def load_config(config_path: str = "config.yaml") -> CloakDBConfig:
    """Load configuration from YAML file and .env."""
    load_env()

    cfg = CloakDBConfig()

    # ── LLM settings — provider-neutral, настрока целиком через .env ──
    #   LLM_PROVIDER   ofox | kodik | openai | ...   (пресет endpoint/модели)
    #   LLM_API_KEY    токен доступа (алиас: LLM_API_TOKEN)
    #   LLM_ENDPOINT   явный URL  (иначе берётся пресет провайдера)
    #   LLM_MODEL      id модели  (иначе дефолт провайдера)
    provider = (os.environ.get("LLM_PROVIDER") or "ofox").strip().lower()
    cfg.llm.provider = provider

    cfg.llm.api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("LLM_API_TOKEN")
        or ""
    )

    _ep = os.environ.get("LLM_ENDPOINT", "").strip()
    cfg.llm.endpoint = _ep or PROVIDER_ENDPOINTS.get(provider, PROVIDER_ENDPOINTS["ofox"])

    _mdl = os.environ.get("LLM_MODEL", "").strip()
    cfg.llm.model = _mdl or PROVIDER_DEFAULT_MODELS.get(provider, "")

    cfg.llm.max_tokens = int(os.environ.get("LLM_MAX_COMPLETION_TOKENS", "32768"))

    # Таймауты одного запроса: считаются адаптивно по размеру чанка, здесь только границы.
    cfg.llm.timeout_base = int(os.environ.get("LLM_TIMEOUT_BASE", "45"))
    cfg.llm.timeout_max = int(os.environ.get("LLM_TIMEOUT_MAX", "180"))

    # Process settings from env
    # (размер LLM-чанка фиксирован внутри llm_client: ~150 значений / 2500 симв. — не env-ручка)
    cfg.processing.sample_limit = int(os.environ.get("SAMPLES_PER_FIELD", "50"))
    cfg.profiles_dir = os.environ.get("PROFILES_DIR", "output/profiles")

    # Load transform rules from YAML (Table.Column: transformer_type)
    config_file = Path(config_path)
    if config_file.exists():
        cfg.transform_rules = _parse_yaml_simple(str(config_file))
    
    return cfg
