"""Constants for the Estfeed integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "estfeed"

CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_FRIENDLY_NAME: Final = "friendly_name"
CONF_RESOLUTION: Final = "resolution"
CONF_BACKFILL_MONTHS: Final = "backfill_months"

DEFAULT_FRIENDLY_NAME: Final = "Estfeed"
DEFAULT_BACKFILL_MONTHS: Final = 12
MAX_BACKFILL_MONTHS: Final = 84
MIN_BACKFILL_MONTHS: Final = 1

UPDATE_INTERVAL: Final = timedelta(hours=1)
ROLLING_CACHE_DAYS: Final = 62
DATA_FRESH_THRESHOLD: Final = timedelta(hours=30)

API_BASE_URL: Final = "https://estfeed.elering.ee"
KEYCLOAK_TOKEN_URL: Final = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
RATE_LIMIT_SECONDS: Final = 5.0
TOKEN_REFRESH_MARGIN_SECONDS: Final = 30
REQUEST_TIMEOUT_SECONDS: Final = 30
MAX_EICS_PER_REQUEST: Final = 10
MAX_DAYS_PER_REQUEST: Final = 31
RECENT_REQUESTS_BUFFER_SIZE: Final = 5

ATTRIBUTION: Final = "Data provided by Elering Estfeed"


class Resolution(StrEnum):
    """API resolution values."""

    QUARTER_HOUR = "fifteen_min"
    HOUR = "one_hour"
    DAY = "one_day"
    WEEK = "one_week"
    MONTH = "one_month"


class Kind(StrEnum):
    """Metering data kind."""

    CONSUMPTION = "consumption"
    PRODUCTION = "production"


class CommodityType(StrEnum):
    """Estfeed commodity types."""

    ELECTRICITY = "ELECTRICITY"
    NATURAL_GAS = "NATURAL_GAS"
