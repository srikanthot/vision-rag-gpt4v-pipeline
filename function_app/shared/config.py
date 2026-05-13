"""
Centralized environment-variable access with explicit configuration errors.

Helpers in this module never raise KeyError mid-request. They raise a
typed ConfigError that the skill envelope (skill_io.handle_skill_request)
will translate into a per-record error with a clear message instead of a
500.
"""

import os


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or empty."""


def required_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"missing required environment variable: {name}. "
            f"Set this on the Function App application settings."
        )
    return val


def optional_env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def feature_enabled(*names: str) -> bool:
    """True if every named env var is present and non-empty. Used to
    gate optional features (e.g. image-hash cache lookup) so they
    silently no-op when not configured."""
    for n in names:
        if not (os.environ.get(n) or "").strip():
            return False
    return True
