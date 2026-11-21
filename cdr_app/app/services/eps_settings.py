"""#1024 — where this service reads its EPS address and key from.

ips.pdhc is being renamed eps.pdhc (epic #1015, phase 3). The environment
names move with it: ``EPS_BASE_URL`` / ``EPS_API_KEY`` are read first, and the
old ``IPS_BASE_URL`` / ``IPS_API_KEY`` are the DEPRECATED fallback, kept until
ips' per-caller counter shows no traffic to the old name (phase 4).

Every read of those variables goes through this module, so the precedence is
decided in one place. Internally the Flask config keys keep their ``IPS_*``
names (tests, the smoke and ``config_override`` set them); only the
environment names change.

There is NO default. Both names empty means unset, exactly as before: callers
refuse by name ("not configured"), never with a manufactured address (#833).
"""
from __future__ import annotations

import os

#: The settings this service reads, by suffix. EPS_<suffix> wins over IPS_<suffix>.
SUFFIXES = ("BASE_URL", "API_KEY")


def eps_env(suffix: str) -> str:
    """``EPS_<suffix>`` if non-empty, else ``IPS_<suffix>``, else ``""``."""
    if suffix not in SUFFIXES:
        raise KeyError(f"not an EPS setting: {suffix!r}")
    return os.environ.get(f"EPS_{suffix}", "") or os.environ.get(f"IPS_{suffix}", "")


def eps_setting(suffix: str, config=None) -> str:
    """The app's configured value (``config["IPS_<suffix>"]``) if set, else the
    environment via ``eps_env``. ``config`` defaults to ``current_app.config``."""
    if config is None:
        from flask import current_app
        config = current_app.config
    return config.get(f"IPS_{suffix}") or eps_env(suffix)
