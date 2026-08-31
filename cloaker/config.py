"""Configuration loader — YAML config + environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any


@dataclass
class LLMConfig:
    api_key: str = ""
    model: str = "bailian/qwen3.8-flash"
    endpoint: str = "https://api.ofox.ai/v1"
    max_tokens: int = 32768  # Increased from 4096 to support ~150 values per API call


@dataclass
class ProcessingConfig:
    batch_size: int = 20
    sample_limit: int = 50


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
    profiles_dir: str = "output/profiles"
    transforms_output: str = "output/transforms"


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

    # Load LLM settings from env (provider-neutral: LLM_API_KEY preferred, KODIK_API_KEY as legacy fallback)
    cfg.llm.api_key = os.environ.get("LLM_API_KEY") or os.environ.get("KODIK_API_KEY", "")
    cfg.llm.model = os.environ.get("LLM_MODEL", "bailian/qwen3.8-flash")
    cfg.llm.endpoint = os.environ.get("LLM_ENDPOINT", "https://api.ofox.ai/v1")
    cfg.llm.max_tokens = int(os.environ.get("LLM_MAX_COMPLETION_TOKENS", "32768"))

    # Process settings from env
    cfg.processing.batch_size = int(os.environ.get("BATCH_SIZE", "20"))
    cfg.processing.sample_limit = int(os.environ.get("SAMPLES_PER_FIELD", "50"))
    cfg.profiles_dir = os.environ.get("PROFILES_DIR", "output/profiles")
    cfg.transforms_output = os.environ.get("TRANSFORMS_OUTPUT", "output/transforms")

    # Load transform rules from YAML (Table.Column: transformer_type)
    config_file = Path(config_path)
    if config_file.exists():
        cfg.transform_rules = _parse_yaml_simple(str(config_file))
    
    return cfg
