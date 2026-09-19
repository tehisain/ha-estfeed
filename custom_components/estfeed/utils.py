"""Small shared helpers for the Estfeed integration."""

from __future__ import annotations

import re


def slugify(name: str) -> str:
    """Reduce a friendly name to the slug used in statistic/entity IDs.

    Shared by ``async_setup_entry`` and the config flow (which rejects
    names that would collide with an already-configured entry's slug).
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "estfeed"
