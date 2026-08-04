from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from data_sources.config.schema import ConnectorConfig
from data_sources.core.exceptions import ConfigurationError


def load_config(path: str | Path) -> ConnectorConfig:
    """Load a `ConnectorConfig` from a YAML file."""
    file_path = Path(path)
    try:
        raw = file_path.read_text()
    except OSError as exc:
        raise ConfigurationError(f"Could not read config file '{file_path}'") from exc

    data = yaml.safe_load(raw) or {}
    return load_config_dict(data)


def load_config_dict(data: dict[str, Any]) -> ConnectorConfig:
    """Build a `ConnectorConfig` from an already-parsed mapping."""
    try:
        return ConnectorConfig.model_validate(data)
    except Exception as exc:
        raise ConfigurationError(f"Invalid connector configuration: {exc}") from exc
