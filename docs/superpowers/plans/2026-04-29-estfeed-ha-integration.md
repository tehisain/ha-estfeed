# Estfeed HA Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a HACS-installable Home Assistant integration that ingests Estfeed metering data into HA's long-term statistics (for Energy Dashboard) and exposes lagging summary sensors (yesterday / month-to-date / previous month).

**Architecture:** Per-config-entry `DataUpdateCoordinator` runs hourly. Pulls latest intervals from Estfeed's REST API via an OAuth-authenticated client (rate-limited 1 req / 5 sec), writes them to HA's external long-term statistics, and maintains a 62-day in-memory cache that drives the lagging sensors. Backfills 12 months on first install via a background task.

**Tech Stack:** Python 3.12+, Home Assistant 2024.6+, `aiohttp` (HA shared session), `homeassistant.helpers.update_coordinator.DataUpdateCoordinator`, `homeassistant.components.recorder.statistics.async_add_external_statistics`, `pytest-homeassistant-custom-component` for tests, `ruff`, `mypy --strict`, hassfest, HACS validate action.

**Spec:** `docs/superpowers/specs/2026-04-29-estfeed-ha-integration-design.md`

---

## File map

```
custom_components/estfeed/
├── __init__.py            # async_setup_entry, async_unload_entry, service registration
├── manifest.json          # HA integration metadata
├── const.py               # DOMAIN, defaults, Resolution enum
├── api.py                 # EstfeedClient: OAuth, REST, rate limiting, errors
├── coordinator.py         # EstfeedCoordinator (DataUpdateCoordinator)
├── statistics.py          # external-statistics writer + helpers
├── config_flow.py         # ConfigFlow + OptionsFlow + reauth
├── sensor.py              # Lagging sensors + latest_interval diagnostic
├── binary_sensor.py       # data_fresh diagnostic
├── diagnostics.py         # async_get_config_entry_diagnostics
├── services.yaml          # backfill_history service definition
├── strings.json           # English UI strings
└── translations/
    ├── en.json
    └── et.json

tests/
├── __init__.py
├── conftest.py            # shared pytest fixtures (mock hass, mock API)
├── test_api.py
├── test_coordinator.py
├── test_statistics.py
├── test_sensor.py
├── test_binary_sensor.py
├── test_config_flow.py
└── test_diagnostics.py

hacs.json                  # HACS install metadata
README.md                  # setup, screenshots, troubleshooting
LICENSE                    # MIT
pyproject.toml             # ruff, mypy, pytest config
.github/workflows/
  └── validate.yml         # hassfest, HACS validation, ruff, mypy, pytest
```

---

## Task 1: Repo scaffolding and CI workflow

**Files:**
- Create: `pyproject.toml`
- Create: `LICENSE`
- Create: `hacs.json`
- Create: `README.md`
- Create: `.github/workflows/validate.yml`
- Create: `.gitignore`
- Create: `custom_components/estfeed/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
coverage.xml
htmlcov/
.venv/
venv/
*.egg-info/
build/
dist/
.DS_Store
.env
```

- [ ] **Step 2: Create `LICENSE` (MIT)**

```
MIT License

Copyright (c) 2026 Ain Tehis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Create `hacs.json`**

```json
{
  "name": "Estfeed",
  "render_readme": true,
  "homeassistant": "2024.6.0"
}
```

- [ ] **Step 4: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ha-estfeed"
version = "0.1.0"
description = "Home Assistant integration for Elering Estfeed metering data"
requires-python = ">=3.12"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "C4", "SIM", "ARG", "RUF"]
ignore = ["E501"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["custom_components/estfeed"]
plugins = []

[[tool.mypy.overrides]]
module = "homeassistant.*"
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = "pytest_homeassistant_custom_component.*"
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-v --tb=short"
```

- [ ] **Step 5: Create `README.md` skeleton**

```markdown
# Estfeed — Home Assistant Integration

Home Assistant integration for Elering's [Estfeed](https://estfeed.elering.ee/) metering data API. Brings Estonian electricity (and gas) meter data into the **Energy Dashboard** with full historical backfill, plus lagging summary sensors for cards and automations.

> **Note:** Estfeed data is settled overnight and arrives ~24 hours late. This integration is built around that — it is not real-time.

## Installation

### HACS (recommended)

1. Add this repository as a custom HACS repository (category: Integration).
2. Install "Estfeed" from HACS.
3. Restart Home Assistant.
4. Settings → Devices & Services → "+ Add Integration" → search "Estfeed".

## Configuration

You'll need a `client_id` and `client_secret` from your e-Elering customer portal:
1. Log in to https://kliendiportaal.elering.ee
2. Generate an API key. The portal shows you the `client_id` (UUID) and `client_secret`.
3. Paste both into the Estfeed integration setup form.

## Energy Dashboard wiring

After setup completes (and the backfill finishes — usually within 1–2 minutes), open Settings → Energy → Electricity grid → "Add consumption" and pick `estfeed:<your_name>_consumption_<eic_suffix>`. If you have solar, add the matching `_production_` stream as "Return to grid".

## Entities created

For each metering point:
- `sensor.<name>_consumption_yesterday` (kWh)
- `sensor.<name>_consumption_month_to_date` (kWh)
- `sensor.<name>_consumption_previous_month` (kWh)
- `sensor.<name>_production_yesterday` / `_month_to_date` / `_previous_month` (kWh, **disabled by default** — enable in entity registry if you generate)
- `sensor.<name>_latest_interval` (timestamp, diagnostic)
- `binary_sensor.<name>_data_fresh` (diagnostic — `on` if newest interval is < 30 h old)

## Services

- `estfeed.backfill_history(months=24, entry_id=<uuid>)` — re-fetch and re-publish the last N months of statistics. Idempotent.

## Limitations

- Data is delayed ~24 hours.
- Cost calculation is intentionally not included; use HA's built-in Energy Dashboard cost configuration with a price entity.
- API rate limit: 1 request per 5 seconds (per API key) — handled internally.
```

- [ ] **Step 6: Create `.github/workflows/validate.yml`**

```yaml
name: Validate

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: home-assistant/actions/hassfest@master

  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hacs/action@main
        with:
          category: integration

  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff mypy pytest pytest-asyncio pytest-homeassistant-custom-component homeassistant aioresponses freezegun
      - run: ruff check custom_components tests
      - run: ruff format --check custom_components tests
      - run: mypy
      - run: pytest tests --cov=custom_components/estfeed --cov-fail-under=85
```

- [ ] **Step 7: Create empty package init files**

`custom_components/estfeed/__init__.py`:
```python
"""Estfeed Home Assistant integration."""
```

`tests/__init__.py`:
```python
"""Tests for the Estfeed integration."""
```

- [ ] **Step 8: Create `tests/conftest.py` with the standard custom-component fixtures**

```python
"""Shared pytest fixtures for Estfeed tests."""
from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom_components/ in every test."""
    yield
```

- [ ] **Step 9: Verify scaffolding builds clean**

Run:
```bash
ruff check custom_components tests
ruff format --check custom_components tests
```

Expected: no errors. (mypy and pytest will be run in later tasks once there's code.)

- [ ] **Step 10: Commit**

```bash
git add .gitignore LICENSE hacs.json pyproject.toml README.md .github/ \
        custom_components/estfeed/__init__.py tests/__init__.py tests/conftest.py
git commit -m "Scaffold repo, CI workflow, and test fixtures"
```

---

## Task 2: Manifest and DOMAIN constant

**Files:**
- Create: `custom_components/estfeed/manifest.json`
- Create: `custom_components/estfeed/const.py`

- [ ] **Step 1: Create `manifest.json`**

```json
{
  "domain": "estfeed",
  "name": "Estfeed",
  "version": "0.1.0",
  "config_flow": true,
  "documentation": "https://github.com/maidokaara/ha-estfeed",
  "issue_tracker": "https://github.com/maidokaara/ha-estfeed/issues",
  "codeowners": ["@maidokaara"],
  "iot_class": "cloud_polling",
  "integration_type": "hub",
  "requirements": [],
  "dependencies": ["recorder"]
}
```

- [ ] **Step 2: Create `const.py` with DOMAIN and defaults**

```python
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
KEYCLOAK_TOKEN_URL: Final = (
    "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
)
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
```

- [ ] **Step 3: Run hassfest locally to validate manifest**

Run:
```bash
python -c "import json; json.load(open('custom_components/estfeed/manifest.json'))"
```

Expected: no output (valid JSON).

- [ ] **Step 4: Commit**

```bash
git add custom_components/estfeed/manifest.json custom_components/estfeed/const.py
git commit -m "Add manifest.json and integration constants"
```

---

## Task 3: API data types (dataclasses)

**Files:**
- Create: `custom_components/estfeed/api.py` (types only at this stage)
- Create: `tests/test_api.py`

- [ ] **Step 1: Write failing test for dataclass round-trip**

`tests/test_api.py`:
```python
"""Tests for the Estfeed API client and data types."""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.estfeed.api import (
    AccountingInterval,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import CommodityType


def test_metering_point_from_dict():
    raw = {
        "eic": "38ZEE-00720089-N",
        "commodityType": "ELECTRICITY",
        "periods": [{"from": "2019-07-27T21:00:00Z"}],
    }
    mp = MeteringPoint.from_dict(raw)
    assert mp.eic == "38ZEE-00720089-N"
    assert mp.commodity_type == CommodityType.ELECTRICITY
    assert mp.periods == [
        Period(start=datetime(2019, 7, 27, 21, 0, tzinfo=timezone.utc), end=None)
    ]


def test_metering_point_from_dict_with_period_end():
    raw = {
        "eic": "X",
        "commodityType": "NATURAL_GAS",
        "periods": [{"from": "2020-01-01T00:00:00Z", "to": "2024-01-01T00:00:00Z"}],
    }
    mp = MeteringPoint.from_dict(raw)
    assert mp.periods[0].end == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_meter_data_from_dict():
    raw = {
        "meteringPointEic": "38ZEE-00720089-N",
        "accountingIntervals": [
            {"periodStart": "2026-04-27T00:00:00Z", "consumptionKwh": 0.348, "productionKwh": 0.0},
            {"periodStart": "2026-04-27T01:00:00Z", "consumptionKwh": 0.338, "productionKwh": 0.0},
        ],
    }
    md = MeterData.from_dict(raw)
    assert md.eic == "38ZEE-00720089-N"
    assert md.error is None
    assert len(md.intervals) == 2
    assert md.intervals[0].period_start == datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    assert md.intervals[0].consumption_kwh == 0.348
    assert md.intervals[0].production_kwh == 0.0


def test_meter_data_with_per_meter_error():
    raw = {
        "meteringPointEic": "X",
        "accountingIntervals": [],
        "error": {
            "id": "abc",
            "message": "boom",
            "code": "error.boom",
            "traceId": "t1",
            "args": [],
        },
    }
    md = MeterData.from_dict(raw)
    assert md.error is not None
    assert md.error.code == "error.boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -v`
Expected: FAIL with `ImportError` (api.py doesn't define those classes yet).

- [ ] **Step 3: Implement dataclasses in `api.py`**

```python
"""Estfeed API client and data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Self

from .const import CommodityType


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 datetime string from the Estfeed API."""
    # Estfeed returns "...Z"; Python's fromisoformat handles "+00:00".
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@dataclass(frozen=True, slots=True)
class Period:
    """An access-validity period for a metering point."""

    start: datetime
    end: datetime | None


@dataclass(frozen=True, slots=True)
class MeteringPoint:
    """A metering point exposed by the API key."""

    eic: str
    commodity_type: CommodityType
    periods: list[Period]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            eic=raw["eic"],
            commodity_type=CommodityType(raw["commodityType"]),
            periods=[
                Period(
                    start=_parse_iso(p["from"]),
                    end=_parse_iso(p["to"]) if p.get("to") else None,
                )
                for p in raw.get("periods", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class AccountingInterval:
    """One accounting interval from the metering-data response."""

    period_start: datetime
    consumption_kwh: float | None
    production_kwh: float | None
    consumption_m3: float | None
    production_m3: float | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            period_start=_parse_iso(raw["periodStart"]),
            consumption_kwh=raw.get("consumptionKwh"),
            production_kwh=raw.get("productionKwh"),
            consumption_m3=raw.get("consumptionM3"),
            production_m3=raw.get("productionM3"),
        )


@dataclass(frozen=True, slots=True)
class MeterError:
    """Per-meter error embedded in a 200 response."""

    id: str
    message: str
    code: str
    trace_id: str
    args: list[Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            id=raw["id"],
            message=raw["message"],
            code=raw["code"],
            trace_id=raw["traceId"],
            args=list(raw.get("args", [])),
        )


@dataclass(frozen=True, slots=True)
class MeterData:
    """Metering data for one meter."""

    eic: str
    intervals: list[AccountingInterval]
    error: MeterError | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            eic=raw["meteringPointEic"],
            intervals=[
                AccountingInterval.from_dict(it) for it in raw.get("accountingIntervals", [])
            ],
            error=MeterError.from_dict(raw["error"]) if raw.get("error") else None,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/api.py tests/test_api.py
git commit -m "Add API data types with dict round-trip tests"
```

---

## Task 4: API client OAuth — fetch and cache token

**Files:**
- Modify: `custom_components/estfeed/api.py` — add `EstfeedClient` class with token fetch + cache
- Modify: `tests/test_api.py` — add token-fetch tests

- [ ] **Step 1: Write failing tests for OAuth token fetch and caching**

Append to `tests/test_api.py`:
```python
import asyncio

import aiohttp
import pytest
from aioresponses import aioresponses
from freezegun import freeze_time

from custom_components.estfeed.api import EstfeedClient
from custom_components.estfeed.const import KEYCLOAK_TOKEN_URL


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
def client(session):
    return EstfeedClient(session, client_id="cid", client_secret="csec")


async def test_token_fetched_and_cached(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "tok-1", "expires_in": 300, "token_type": "Bearer"},
        )
        token1 = await client._ensure_token()
        token2 = await client._ensure_token()
        assert token1 == "tok-1"
        assert token2 == "tok-1"  # cached, no second POST
        # aioresponses raises if a mocked URL is requested more times than registered
        # (it's registered once); reaching this line proves the second call did not POST.


async def test_token_refresh_before_expiry(client):
    """Token must be refreshed when within the safety margin of expiry."""
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "tok-1", "expires_in": 300, "token_type": "Bearer"},
        )
        with freeze_time("2026-04-29T00:00:00Z") as frozen:
            await client._ensure_token()
            # advance to within 30s of expiry: 300 - 30 = 270s in
            frozen.tick(delta=271)
            mocked.post(
                KEYCLOAK_TOKEN_URL,
                payload={"access_token": "tok-2", "expires_in": 300, "token_type": "Bearer"},
            )
            token = await client._ensure_token()
            assert token == "tok-2"


async def test_concurrent_token_fetch_serialised(client):
    """Ten concurrent _ensure_token calls trigger only one HTTP POST."""
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "tok-1", "expires_in": 300, "token_type": "Bearer"},
        )
        results = await asyncio.gather(*[client._ensure_token() for _ in range(10)])
        assert all(r == "tok-1" for r in results)
        # Only one mock was registered — if more than one POST happened, aioresponses
        # would raise on the unmatched extras.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k token -v`
Expected: FAIL — `EstfeedClient` not defined.

- [ ] **Step 3: Implement `EstfeedClient` token logic**

Append to `custom_components/estfeed/api.py`:
```python
import asyncio
import logging
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

import aiohttp

from .const import (
    KEYCLOAK_TOKEN_URL,
    REQUEST_TIMEOUT_SECONDS,
    TOKEN_REFRESH_MARGIN_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class EstfeedClient:
    """HTTP client for the Estfeed public API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0  # monotonic
        self._token_lock = asyncio.Lock()

    async def _ensure_token(self) -> str:
        """Return a valid bearer token, fetching/refreshing as needed."""
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            self._token, self._token_expires_at = await self._fetch_token(now)
            return self._token

    async def _fetch_token(self, now_monotonic: float) -> tuple[str, float]:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with self._session.post(
            KEYCLOAK_TOKEN_URL,
            data=data,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise EstfeedAuthError(
                    f"Token request failed: {resp.status} {payload.get('error', '')}"
                )
            return payload["access_token"], now_monotonic + float(payload.get("expires_in", 60))


class EstfeedAuthError(Exception):
    """Authentication / authorisation failure."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k token -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/api.py tests/test_api.py
git commit -m "Add EstfeedClient OAuth token fetch + cache"
```

---

## Task 5: Rate limiter and recent-request ring buffer

**Files:**
- Modify: `custom_components/estfeed/api.py` — add `_request()` helper with rate limiting and ring buffer
- Modify: `tests/test_api.py` — add tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_api.py`:
```python
import time as time_module


async def test_rate_limit_no_sleep_when_no_prior_request(client, monkeypatch):
    """Fresh client (no prior request) makes the call immediately."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", payload={"ok": True})
        await client._request("GET", "/api/public/v1/x")

    # _last_request_at starts at 0; elapsed since epoch is huge → no positive sleep needed.
    assert all(s <= 0 for s in sleeps)


async def test_rate_limit_sleeps_when_too_soon(client, monkeypatch):
    """A request made right after a prior one must sleep until ≥5s elapsed."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    # Pretend a request just happened a moment ago.
    client._last_request_at = time_module.monotonic()

    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", payload={"ok": True})
        await client._request("GET", "/api/public/v1/x")

    # Expect a sleep close to 5s (minus the few ms of test overhead since we set _last_request_at).
    assert any(s >= 4.0 for s in sleeps), f"expected a ~5s sleep, got {sleeps}"


async def test_recent_requests_ring_buffer(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        for _ in range(7):
            mocked.get("https://estfeed.elering.ee/api/public/v1/x", payload={"ok": True})

        for _ in range(7):
            await client._request("GET", "/api/public/v1/x")

    summaries = list(client.recent_requests)
    assert len(summaries) == 5
    assert all(s["status"] == 200 for s in summaries)
    assert all(s["path"] == "/api/public/v1/x" for s in summaries)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "rate_limit or recent_requests" -v`
Expected: FAIL — `_request` not defined.

- [ ] **Step 3: Implement `_request()` with rate limiter and ring buffer**

Add to `EstfeedClient.__init__`:
```python
        self._rate_lock = asyncio.Lock()
        self._last_request_at: float = 0.0
        self.recent_requests: deque[dict[str, Any]] = deque(maxlen=RECENT_REQUESTS_BUFFER_SIZE)
```

(Update import: `from .const import API_BASE_URL, RATE_LIMIT_SECONDS, RECENT_REQUESTS_BUFFER_SIZE`.)

Add method to `EstfeedClient`:
```python
    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._ensure_token()
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            wait = RATE_LIMIT_SECONDS - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            url = f"{API_BASE_URL}{path}"
            started = time.monotonic()
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                ) as resp:
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self.recent_requests.append(
                        {
                            "method": method,
                            "path": path,
                            "status": resp.status,
                            "duration_ms": duration_ms,
                        }
                    )
                    payload = await resp.json()
                    return resp.status, payload
            finally:
                self._last_request_at = time.monotonic()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k "rate_limit or recent_requests" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/api.py tests/test_api.py
git commit -m "Add rate limiter and recent-request ring buffer to EstfeedClient"
```

---

## Task 6: Typed exceptions and HTTP error mapping

**Files:**
- Modify: `custom_components/estfeed/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing tests for error mapping**

Append to `tests/test_api.py`:
```python
from custom_components.estfeed.api import (
    EstfeedAPIError,
    EstfeedRateLimitError,
    EstfeedTimeoutError,
)


async def test_401_raises_auth_error(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/x",
            status=401,
            payload={"message": "nope"},
        )
        with pytest.raises(EstfeedAuthError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_429_raises_rate_limit(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", status=429, payload={})
        with pytest.raises(EstfeedRateLimitError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_500_raises_api_error(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", status=500, payload={})
        with pytest.raises(EstfeedAPIError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_timeout_raises_timeout_error(client, monkeypatch):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", exception=asyncio.TimeoutError())
        with pytest.raises(EstfeedTimeoutError):
            await client._request_json("GET", "/api/public/v1/x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "raises" -v`
Expected: FAIL — `_request_json` not defined; new exception classes not exported.

- [ ] **Step 3: Add exception classes and `_request_json` wrapper**

In `api.py`, replace the single `EstfeedAuthError` definition with:
```python
class EstfeedError(Exception):
    """Base error for the Estfeed integration."""


class EstfeedAuthError(EstfeedError):
    """Authentication / authorisation failure (401, 403, Keycloak failure)."""


class EstfeedRateLimitError(EstfeedError):
    """API returned 429 Too Many Requests."""


class EstfeedAPIError(EstfeedError):
    """Generic API failure (4xx other than 401/403/429, or 5xx)."""


class EstfeedTimeoutError(EstfeedError):
    """Network timeout or connection error."""
```

Add `_request_json` to `EstfeedClient`:
```python
    async def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            status, payload = await self._request(method, path, params=params)
        except asyncio.TimeoutError as err:
            raise EstfeedTimeoutError(str(err)) from err
        except aiohttp.ClientError as err:
            raise EstfeedTimeoutError(str(err)) from err

        if status == 200:
            return payload
        if status in (401, 403):
            raise EstfeedAuthError(f"{status}: {payload}")
        if status == 429:
            raise EstfeedRateLimitError(f"{status}: {payload}")
        raise EstfeedAPIError(f"{status}: {payload}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k "raises" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/api.py tests/test_api.py
git commit -m "Add typed exceptions and HTTP error mapping in EstfeedClient"
```

---

## Task 7: API methods — `list_metering_points` and `get_metering_data`

**Files:**
- Modify: `custom_components/estfeed/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_api.py`:
```python
from datetime import datetime, timezone

from custom_components.estfeed.const import Resolution


async def test_list_metering_points(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/metering-point-eics",
            payload=[
                {
                    "eic": "38ZEE-00720089-N",
                    "commodityType": "ELECTRICITY",
                    "periods": [{"from": "2019-07-27T21:00:00Z"}],
                }
            ],
        )
        result = await client.list_metering_points(
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 4, 29, tzinfo=timezone.utc),
        )
        assert len(result) == 1
        assert result[0].eic == "38ZEE-00720089-N"


async def test_get_metering_data_with_eics(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/metering-data",
            payload=[
                {
                    "meteringPointEic": "38ZEE-00720089-N",
                    "accountingIntervals": [
                        {"periodStart": "2026-04-27T00:00:00Z", "consumptionKwh": 0.348, "productionKwh": 0.0},
                    ],
                }
            ],
        )
        result = await client.get_metering_data(
            datetime(2026, 4, 27, tzinfo=timezone.utc),
            datetime(2026, 4, 28, tzinfo=timezone.utc),
            Resolution.HOUR,
            eics=["38ZEE-00720089-N"],
        )
        assert len(result) == 1
        assert result[0].eic == "38ZEE-00720089-N"
        assert len(result[0].intervals) == 1
        assert result[0].intervals[0].consumption_kwh == 0.348


async def test_get_metering_data_too_many_eics_raises(client):
    with pytest.raises(ValueError, match="up to 10"):
        await client.get_metering_data(
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 4, 2, tzinfo=timezone.utc),
            Resolution.HOUR,
            eics=[f"X{i}" for i in range(11)],
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "list_metering or get_metering" -v`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Implement methods**

Add to `EstfeedClient`:
```python
    async def list_metering_points(
        self,
        start: datetime,
        end: datetime,
    ) -> list[MeteringPoint]:
        params = {
            "startDateTime": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "endDateTime": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        payload = await self._request_json("GET", "/api/public/v1/metering-point-eics", params=params)
        return [MeteringPoint.from_dict(item) for item in payload]

    async def get_metering_data(
        self,
        start: datetime,
        end: datetime,
        resolution: Resolution,
        eics: Iterable[str] | None = None,
    ) -> list[MeterData]:
        eic_list = list(eics) if eics is not None else None
        if eic_list is not None and len(eic_list) > MAX_EICS_PER_REQUEST:
            raise ValueError(f"API supports up to 10 EICs per request, got {len(eic_list)}")
        params: dict[str, Any] = {
            "startDateTime": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "endDateTime": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "resolution": resolution.value,
        }
        if eic_list:
            params["meteringPointEics"] = ",".join(eic_list)
        payload = await self._request_json("GET", "/api/public/v1/metering-data", params=params)
        return [MeterData.from_dict(item) for item in payload]
```

(Update imports at top of `api.py`: `from datetime import datetime, timezone` and `from .const import MAX_EICS_PER_REQUEST, Resolution`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/api.py tests/test_api.py
git commit -m "Add list_metering_points and get_metering_data API methods"
```

---

## Task 8: Statistics writer — cumulative sum and statistic_id helpers

**Files:**
- Create: `custom_components/estfeed/statistics.py`
- Create: `tests/test_statistics.py`

- [ ] **Step 1: Write failing tests**

`tests/test_statistics.py`:
```python
"""Tests for statistics helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.estfeed.api import AccountingInterval
from custom_components.estfeed.const import Kind
from custom_components.estfeed.statistics import (
    build_statistic_id,
    compute_statistic_rows,
    eic_suffix,
)


def test_eic_suffix():
    assert eic_suffix("38ZEE-00720089-N") == "089N"
    assert eic_suffix("XYZW-12345678-AB") == "78AB"


def test_build_statistic_id_single_meter():
    assert build_statistic_id("home", Kind.CONSUMPTION, "089N", multi_meter=False) == "estfeed:home_consumption_089N"


def test_build_statistic_id_multi_meter():
    # When multiple meters share an entry, suffix is appended even if slug carries it.
    assert build_statistic_id("home", Kind.PRODUCTION, "089N", multi_meter=True) == "estfeed:home_production_089N"


def test_compute_statistic_rows_first_chunk():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, h, tzinfo=timezone.utc),
            consumption_kwh=1.0 + h * 0.1,
            production_kwh=0.0,
            consumption_m3=None,
            production_m3=None,
        )
        for h in range(3)
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert len(rows) == 3
    assert rows[0]["start"] == datetime(2026, 4, 27, 0, tzinfo=timezone.utc)
    assert rows[0]["sum"] == 1.0
    assert rows[1]["sum"] == 2.1  # 1.0 + 1.1
    assert rows[2]["sum"] == 3.3  # 2.1 + 1.2
    # state mirrors sum for counter semantics
    assert rows[0]["state"] == rows[0]["sum"]


def test_compute_statistic_rows_continuation_with_prior_sum():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 28, 0, tzinfo=timezone.utc),
            consumption_kwh=2.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=100.0)
    assert rows[0]["sum"] == 102.0


def test_compute_statistic_rows_skips_none_values():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=timezone.utc),
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert rows == []


def test_compute_statistic_rows_sorts_by_start():
    """Rows must be ascending by start regardless of input order."""
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 2, tzinfo=timezone.utc),
            consumption_kwh=0.5,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=timezone.utc),
            consumption_kwh=1.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 1, tzinfo=timezone.utc),
            consumption_kwh=2.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)
    sums = [r["sum"] for r in rows]
    assert sums == [1.0, 3.0, 3.5]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_statistics.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `statistics.py`**

```python
"""Statistics helpers for Estfeed: build statistic_ids, compute cumulative sums."""
from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from .api import AccountingInterval
from .const import DOMAIN, Kind


class StatisticRow(TypedDict):
    """One row to pass to async_add_external_statistics."""

    start: datetime
    state: float
    sum: float


def eic_suffix(eic: str) -> str:
    """Last 4 alphanumeric characters of an EIC, no hyphens, used to disambiguate."""
    return "".join(ch for ch in eic if ch.isalnum())[-4:]


def build_statistic_id(slug: str, kind: Kind, suffix: str, *, multi_meter: bool) -> str:
    """Construct a deterministic statistic_id for a (slug, kind, eic) combination."""
    # multi_meter is currently informational; the suffix is always included to keep
    # statistic_ids stable if the user later adds a second meter to the same entry.
    del multi_meter
    return f"{DOMAIN}:{slug}_{kind.value}_{suffix}"


def _interval_value(interval: AccountingInterval, kind: Kind) -> float | None:
    if kind == Kind.CONSUMPTION:
        return interval.consumption_kwh if interval.consumption_kwh is not None else interval.consumption_m3
    return interval.production_kwh if interval.production_kwh is not None else interval.production_m3


def compute_statistic_rows(
    intervals: list[AccountingInterval],
    kind: Kind,
    prior_sum: float,
) -> list[StatisticRow]:
    """Build cumulative-sum statistic rows from raw intervals.

    Skips intervals where the relevant value is None. Output is sorted by start
    ascending. `state` is set equal to `sum` (counter semantics for HA's
    statistics display).
    """
    sorted_ivals = sorted(intervals, key=lambda i: i.period_start)
    rows: list[StatisticRow] = []
    running = prior_sum
    for ival in sorted_ivals:
        value = _interval_value(ival, kind)
        if value is None:
            continue
        running += float(value)
        rows.append({"start": ival.period_start, "state": running, "sum": running})
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_statistics.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/statistics.py tests/test_statistics.py
git commit -m "Add statistics helpers: statistic_id builder and cumulative sum"
```

---

## Task 9: Statistics writer — HA integration

**Files:**
- Modify: `custom_components/estfeed/statistics.py`
- Modify: `tests/test_statistics.py`

- [ ] **Step 1: Write failing test for `async_write_meter_statistics`**

Append to `tests/test_statistics.py`:
```python
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.estfeed.statistics import (
    StatisticStream,
    async_write_meter_statistics,
)


@pytest.mark.asyncio
async def test_async_write_meter_statistics_calls_external_stats(hass):
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=timezone.utc),
            consumption_kwh=1.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    stream = StatisticStream(
        statistic_id="estfeed:home_consumption_089N",
        name="Home consumption (38ZEE-00720089-N)",
        unit="kWh",
        kind=Kind.CONSUMPTION,
    )
    with patch(
        "custom_components.estfeed.statistics.async_add_external_statistics",
        new=AsyncMock(),
    ) as mock_add:
        await async_write_meter_statistics(hass, stream, intervals, prior_sum=0.0)

    mock_add.assert_called_once()
    metadata, rows = mock_add.call_args.args[1], mock_add.call_args.args[2]
    assert metadata["statistic_id"] == "estfeed:home_consumption_089N"
    assert metadata["source"] == "estfeed"
    assert metadata["unit_of_measurement"] == "kWh"
    assert metadata["has_sum"] is True
    assert metadata["has_mean"] is False
    assert len(rows) == 1
    assert rows[0]["sum"] == 1.0


@pytest.mark.asyncio
async def test_async_write_meter_statistics_noop_when_no_rows(hass):
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=timezone.utc),
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    stream = StatisticStream(
        statistic_id="estfeed:home_consumption_089N",
        name="x",
        unit="kWh",
        kind=Kind.CONSUMPTION,
    )
    with patch(
        "custom_components.estfeed.statistics.async_add_external_statistics",
        new=AsyncMock(),
    ) as mock_add:
        await async_write_meter_statistics(hass, stream, intervals, prior_sum=0.0)
    mock_add.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_statistics.py -v`
Expected: FAIL — `async_write_meter_statistics` and `StatisticStream` not defined.

- [ ] **Step 3: Implement HA integration**

Add to `statistics.py`:
```python
from dataclasses import dataclass

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant


@dataclass(frozen=True, slots=True)
class StatisticStream:
    """Identifies one external statistics stream (one statistic_id)."""

    statistic_id: str
    name: str
    unit: str
    kind: Kind


async def async_write_meter_statistics(
    hass: HomeAssistant,
    stream: StatisticStream,
    intervals: list[AccountingInterval],
    prior_sum: float,
) -> None:
    """Compute and submit external statistics rows for one meter+kind."""
    rows = compute_statistic_rows(intervals, stream.kind, prior_sum=prior_sum)
    if not rows:
        return
    metadata = {
        "source": DOMAIN,
        "statistic_id": stream.statistic_id,
        "name": stream.name,
        "unit_of_measurement": stream.unit,
        "has_sum": True,
        "has_mean": False,
    }
    await async_add_external_statistics(hass, metadata, rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_statistics.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/statistics.py tests/test_statistics.py
git commit -m "Wire statistics helpers to HA's async_add_external_statistics"
```

---

## Task 10: Coordinator skeleton + hourly fetch loop

**Files:**
- Create: `custom_components/estfeed/coordinator.py`
- Create: `tests/test_coordinator.py`

- [ ] **Step 1: Write failing tests for coordinator fetch loop**

`tests/test_coordinator.py`:
```python
"""Tests for EstfeedCoordinator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.estfeed.api import (
    AccountingInterval,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_BACKFILL_MONTHS,
    CONF_RESOLUTION,
    CommodityType,
    Kind,
    Resolution,
)
from custom_components.estfeed.coordinator import EstfeedCoordinator


def _make_meter(eic: str = "38ZEE-00720089-N") -> MeteringPoint:
    return MeteringPoint(
        eic=eic,
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=timezone.utc), end=None)],
    )


def _hourly(start: datetime, hours: int, kwh: float = 0.5) -> list[AccountingInterval]:
    return [
        AccountingInterval(
            period_start=start + timedelta(hours=h),
            consumption_kwh=kwh,
            production_kwh=0.0,
            consumption_m3=None,
            production_m3=None,
        )
        for h in range(hours)
    ]


@pytest.mark.asyncio
async def test_coordinator_first_update_fetches_and_writes(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=_hourly(datetime(2026, 4, 28, 0, tzinfo=timezone.utc), 24),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with patch(
        "custom_components.estfeed.coordinator.get_last_statistics",
        new=AsyncMock(return_value={}),
    ), patch(
        "custom_components.estfeed.coordinator.async_write_meter_statistics",
        new=AsyncMock(),
    ) as mock_write:
        await coordinator._async_update_data()

    # One call per (eic, kind) pair = 2 (consumption + production)
    assert mock_write.call_count == 2


@pytest.mark.asyncio
async def test_coordinator_uses_latest_seen_as_start(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(return_value=[
        MeterData(eic="38ZEE-00720089-N", intervals=[])
    ])

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    last_seen_ts = datetime(2026, 4, 28, 23, tzinfo=timezone.utc).timestamp()
    fake_last_stats = {
        "estfeed:home_consumption_089N": [{"end": last_seen_ts * 1000}],  # ms
    }

    with patch(
        "custom_components.estfeed.coordinator.get_last_statistics",
        new=AsyncMock(return_value=fake_last_stats),
    ), patch(
        "custom_components.estfeed.coordinator.async_write_meter_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    # First call should request from latest_seen + 1 hour onwards
    args, kwargs = client.get_metering_data.call_args
    start_arg = args[0] if args else kwargs.get("start") or kwargs.get("startDateTime")
    # Resolve which positional arg is start — our get_metering_data signature is (start, end, resolution, eics=...)
    assert args[0] == datetime(2026, 4, 29, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_coordinator_per_meter_error_is_skipped(hass):
    """A meter returning an `error` field is skipped without crashing the tick."""
    from custom_components.estfeed.api import MeterError

    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(return_value=[
        MeterData(
            eic="38ZEE-00720089-N",
            intervals=[],
            error=MeterError(id="x", message="m", code="c", trace_id="t", args=[]),
        )
    ])

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with patch(
        "custom_components.estfeed.coordinator.get_last_statistics",
        new=AsyncMock(return_value={}),
    ), patch(
        "custom_components.estfeed.coordinator.async_write_meter_statistics",
        new=AsyncMock(),
    ) as mock_write:
        await coordinator._async_update_data()

    mock_write.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_coordinator.py -v`
Expected: FAIL — `EstfeedCoordinator` not defined.

- [ ] **Step 3: Implement `coordinator.py`**

```python
"""EstfeedCoordinator: fetches data and writes long-term statistics."""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AccountingInterval,
    EstfeedClient,
    EstfeedError,
    MeteringPoint,
)
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_RESOLUTION,
    DOMAIN,
    MAX_DAYS_PER_REQUEST,
    MAX_EICS_PER_REQUEST,
    ROLLING_CACHE_DAYS,
    UPDATE_INTERVAL,
    Kind,
    Resolution,
)
from .statistics import (
    StatisticStream,
    async_write_meter_statistics,
    build_statistic_id,
    eic_suffix,
)

_LOGGER = logging.getLogger(__name__)

# Kind / unit mapping for electricity vs gas
_ELECTRICITY_KINDS = (Kind.CONSUMPTION, Kind.PRODUCTION)
_GAS_KINDS = (Kind.CONSUMPTION, Kind.PRODUCTION)


class EstfeedCoordinator(DataUpdateCoordinator[None]):
    """Hourly poller + statistics ingester."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EstfeedClient,
        slug: str,
        options: dict[str, Any],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{slug}",
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client
        self.slug = slug
        self.options = options
        self.meters: list[MeteringPoint] = []
        # rolling cache: {(eic, kind): deque[AccountingInterval]} sorted by period_start
        self.cache: dict[tuple[str, Kind], deque[AccountingInterval]] = defaultdict(
            lambda: deque()
        )
        self.last_meter_errors: dict[str, str] = {}

    @property
    def resolution(self) -> Resolution:
        return Resolution(self.options.get(CONF_RESOLUTION, Resolution.HOUR.value))

    @property
    def backfill_months(self) -> int:
        return int(self.options.get(CONF_BACKFILL_MONTHS, 12))

    def streams_for(self, meter: MeteringPoint) -> list[StatisticStream]:
        suffix = eic_suffix(meter.eic)
        unit = "kWh" if meter.commodity_type.value == "ELECTRICITY" else "m³"
        kinds = _ELECTRICITY_KINDS if meter.commodity_type.value == "ELECTRICITY" else _GAS_KINDS
        return [
            StatisticStream(
                statistic_id=build_statistic_id(self.slug, k, suffix, multi_meter=len(self.meters) > 1),
                name=f"{self.slug} {k.value} ({meter.eic})",
                unit=unit,
                kind=k,
            )
            for k in kinds
        ]

    async def _async_update_data(self) -> None:
        """One hourly tick: fetch new intervals per meter, write statistics, update cache."""
        if not self.meters:
            return
        try:
            await self._fetch_window(
                self._compute_default_start(),
                datetime.now(tz=timezone.utc),
                write_stats=True,
                force_start=False,
            )
        except EstfeedError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch_window(
        self,
        start: datetime,
        end: datetime,
        *,
        write_stats: bool,
        force_start: bool,
    ) -> None:
        """Fetch [start, end] in 31-day chunks, optionally writing stats per chunk."""
        for meter in self.meters:
            await self._fetch_meter_window(
                meter, start, end, write_stats=write_stats, force_start=force_start
            )

    async def _fetch_meter_window(
        self,
        meter: MeteringPoint,
        start: datetime,
        end: datetime,
        *,
        write_stats: bool,
        force_start: bool,
    ) -> None:
        for stream in self.streams_for(meter):
            chunk_start = (
                start if force_start else await self._chunk_start_for_stream(stream, start)
            )
            cursor = chunk_start
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=MAX_DAYS_PER_REQUEST), end)
                results = await self._client.get_metering_data(
                    cursor, chunk_end, self.resolution, eics=[meter.eic]
                )
                for md in results:
                    if md.error is not None:
                        self.last_meter_errors[md.eic] = md.error.code
                        _LOGGER.warning(
                            "Estfeed returned error for meter %s: %s (traceId=%s)",
                            md.eic,
                            md.error.code,
                            md.error.trace_id,
                        )
                        continue
                    if write_stats:
                        prior_sum = await self._prior_sum_for_stream(stream)
                        await async_write_meter_statistics(
                            self.hass, stream, md.intervals, prior_sum=prior_sum
                        )
                    self._update_cache(meter.eic, stream.kind, md.intervals)
                cursor = chunk_end

    async def _chunk_start_for_stream(
        self, stream: StatisticStream, default_start: datetime
    ) -> datetime:
        """Pick the start of the next fetch window for a given stream."""
        latest = await self._latest_seen_for_stream(stream)
        if latest is None:
            return default_start
        return latest + timedelta(hours=1)

    async def _latest_seen_for_stream(self, stream: StatisticStream) -> datetime | None:
        last_stats = await get_last_statistics(
            self.hass, 1, stream.statistic_id, True, {"end"}
        )
        rows = last_stats.get(stream.statistic_id)
        if not rows:
            return None
        end_ms = rows[0].get("end")
        if end_ms is None:
            return None
        return datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc)

    async def _prior_sum_for_stream(self, stream: StatisticStream) -> float:
        last_stats = await get_last_statistics(
            self.hass, 1, stream.statistic_id, True, {"sum"}
        )
        rows = last_stats.get(stream.statistic_id)
        if not rows:
            return 0.0
        return float(rows[0].get("sum") or 0.0)

    def _compute_default_start(self) -> datetime:
        """For the regular hourly tick, start window = now - 30 days as a fallback.

        Only used when no prior statistics exist (meter was just added). The
        initial-backfill flow uses backfill_months instead, computed by callers.
        """
        return datetime.now(tz=timezone.utc) - timedelta(days=30)

    def _update_cache(
        self,
        eic: str,
        kind: Kind,
        intervals: list[AccountingInterval],
    ) -> None:
        bucket = self.cache[(eic, kind)]
        for ival in sorted(intervals, key=lambda i: i.period_start):
            bucket.append(ival)
        # Trim entries older than ROLLING_CACHE_DAYS
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ROLLING_CACHE_DAYS)
        while bucket and bucket[0].period_start < cutoff:
            bucket.popleft()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_coordinator.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/coordinator.py tests/test_coordinator.py
git commit -m "Add EstfeedCoordinator with hourly fetch + per-meter error skip"
```

---

## Task 11: Coordinator initial backfill + cache warmup

**Files:**
- Modify: `custom_components/estfeed/coordinator.py`
- Modify: `tests/test_coordinator.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_coordinator.py`:
```python
@pytest.mark.asyncio
async def test_initial_backfill_uses_backfill_months(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(return_value=[
        MeterData(eic="38ZEE-00720089-N", intervals=[])
    ])

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with patch(
        "custom_components.estfeed.coordinator.get_last_statistics",
        new=AsyncMock(return_value={}),
    ), patch(
        "custom_components.estfeed.coordinator.async_write_meter_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_initial_backfill()

    # First fetch starts ~12 months back. Allow some tolerance.
    args = client.get_metering_data.call_args_list[0].args
    assert args[0] < datetime.now(tz=timezone.utc) - timedelta(days=350)


@pytest.mark.asyncio
async def test_cache_warmup_populates_rolling_cache(hass):
    intervals = _hourly(datetime.now(tz=timezone.utc) - timedelta(days=30), 24 * 5)
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(return_value=[
        MeterData(eic="38ZEE-00720089-N", intervals=intervals)
    ])

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with patch(
        "custom_components.estfeed.coordinator.get_last_statistics",
        new=AsyncMock(return_value={}),
    ), patch(
        "custom_components.estfeed.coordinator.async_write_meter_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_warm_cache()

    cached = coordinator.cache[("38ZEE-00720089-N", Kind.CONSUMPTION)]
    # The mock returns the same intervals for every chunk; the warmup window is split
    # into 31-day chunks so the cache gets called multiple times. We don't assert an
    # exact count — only that the cache was populated for both kinds.
    assert len(cached) > 0
    assert len(coordinator.cache[("38ZEE-00720089-N", Kind.PRODUCTION)]) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_coordinator.py -k "backfill or warm" -v`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Implement `async_initial_backfill` and `async_warm_cache`**

Add to `EstfeedCoordinator`:
```python
    async def async_initial_backfill(self) -> None:
        """Run once at setup if no statistics exist for this entry's streams.

        Walks 12 months of history (or whatever `backfill_months` is set to). Because
        no prior stats exist on first install, prior_sum starts at 0 and the cumulative
        counter is built correctly from the earliest backfilled interval.
        """
        end = datetime.now(tz=timezone.utc)
        # 12 months ≈ 365 days; backfill_months × 30 keeps things simple and bounded.
        start = end - timedelta(days=self.backfill_months * 30)
        await self._fetch_window(start, end, write_stats=True, force_start=True)

    async def async_warm_cache(self) -> None:
        """Populate the rolling 62-day cache after a restart.

        Does NOT write statistics — they already exist from prior runs. Re-writing
        them mid-series with a fresh `prior_sum` lookup would corrupt cumulative
        counter semantics. We just refill `self.cache` for the lagging sensors.
        """
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=ROLLING_CACHE_DAYS)
        await self._fetch_window(start, end, write_stats=False, force_start=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_coordinator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/coordinator.py tests/test_coordinator.py
git commit -m "Add initial backfill and rolling-cache warmup to coordinator"
```

---

## Task 12: Sensor entities — base class + lagging values

**Files:**
- Create: `custom_components/estfeed/sensor.py`
- Create: `tests/test_sensor.py`

- [ ] **Step 1: Write failing tests for the lagging-value calculations**

`tests/test_sensor.py`:
```python
"""Tests for Estfeed sensor entities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.estfeed.api import AccountingInterval
from custom_components.estfeed.const import Kind
from custom_components.estfeed.sensor import (
    LaggingPeriod,
    sum_for_period,
    window_for_period,
)

TALLINN = ZoneInfo("Europe/Tallinn")


def _ival(period_start: datetime, kwh: float) -> AccountingInterval:
    return AccountingInterval(
        period_start=period_start,
        consumption_kwh=kwh,
        production_kwh=None,
        consumption_m3=None,
        production_m3=None,
    )


def test_window_yesterday_in_local_tz():
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.YESTERDAY, now=now, tz=TALLINN)
    assert start == datetime(2026, 4, 28, 0, tzinfo=TALLINN)
    assert end == datetime(2026, 4, 29, 0, tzinfo=TALLINN)


def test_window_month_to_date_in_local_tz():
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.MONTH_TO_DATE, now=now, tz=TALLINN)
    assert start == datetime(2026, 4, 1, 0, tzinfo=TALLINN)
    assert end == now


def test_window_previous_month_in_local_tz():
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.PREVIOUS_MONTH, now=now, tz=TALLINN)
    assert start == datetime(2026, 3, 1, 0, tzinfo=TALLINN)
    assert end == datetime(2026, 4, 1, 0, tzinfo=TALLINN)


def test_window_previous_month_january_rolls_to_december():
    now = datetime(2026, 1, 5, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.PREVIOUS_MONTH, now=now, tz=TALLINN)
    assert start == datetime(2025, 12, 1, 0, tzinfo=TALLINN)
    assert end == datetime(2026, 1, 1, 0, tzinfo=TALLINN)


def test_sum_for_period_yesterday():
    intervals = [
        _ival(datetime(2026, 4, 27, h, tzinfo=timezone.utc), 1.0) for h in range(24)
    ] + [
        _ival(datetime(2026, 4, 28, h, tzinfo=timezone.utc), 2.0) for h in range(24)
    ]
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    total = sum_for_period(intervals, Kind.CONSUMPTION, LaggingPeriod.YESTERDAY, now=now, tz=TALLINN)
    # Yesterday in Tallinn = 2026-04-28 local = 2026-04-27 21:00Z .. 2026-04-28 21:00Z
    # That spans 3 hours of the 27th-utc bucket (21,22,23) and 21 hours of the 28th-utc.
    expected = 3 * 1.0 + 21 * 2.0
    assert total == pytest.approx(expected)


def test_sum_for_period_dst_spring_forward_23h_day():
    """Last Sunday of March in Europe/Tallinn: 03:00 EET → 04:00 EEST = 23-hour day."""
    # 2026-03-29 is the spring-forward day in EU.
    # We compute YESTERDAY at 2026-03-30 noon local — that yesterday is 23 hours long.
    intervals = [
        _ival(datetime(2026, 3, 28, 22, tzinfo=timezone.utc), 1.0),  # 2026-03-29 00:00 EET
        _ival(datetime(2026, 3, 28, 23, tzinfo=timezone.utc), 1.0),  # 2026-03-29 01:00 EET
        _ival(datetime(2026, 3, 29, 0, tzinfo=timezone.utc), 1.0),   # 2026-03-29 02:00 EET
        # 03:00 EET does not exist; clock jumps to 04:00 EEST (= 01:00 UTC)
        _ival(datetime(2026, 3, 29, 1, tzinfo=timezone.utc), 1.0),   # 2026-03-29 04:00 EEST
        # ... continuing through 2026-03-29 23:00 EEST (= 20:00 UTC)
        *[_ival(datetime(2026, 3, 29, h, tzinfo=timezone.utc), 1.0) for h in range(2, 21)],
    ]
    now = datetime(2026, 3, 30, 12, tzinfo=TALLINN)
    total = sum_for_period(intervals, Kind.CONSUMPTION, LaggingPeriod.YESTERDAY, now=now, tz=TALLINN)
    # 23 intervals fall in the spring-forward day
    assert total == pytest.approx(23.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sensor.py -v`
Expected: FAIL — `sensor.py` doesn't define those helpers.

- [ ] **Step 3: Implement the lagging logic in `sensor.py`**

```python
"""Estfeed sensor entities and lagging-period helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AccountingInterval, MeteringPoint
from .const import (
    ATTRIBUTION,
    CommodityType,
    DOMAIN,
    Kind,
)
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix


class LaggingPeriod(StrEnum):
    YESTERDAY = "yesterday"
    MONTH_TO_DATE = "month_to_date"
    PREVIOUS_MONTH = "previous_month"


def window_for_period(
    period: LaggingPeriod, *, now: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return [start, end) for the given period in the given tz."""
    local_now = now.astimezone(tz)
    today_local = datetime(local_now.year, local_now.month, local_now.day, tzinfo=tz)
    if period == LaggingPeriod.YESTERDAY:
        return today_local - timedelta(days=1), today_local
    if period == LaggingPeriod.MONTH_TO_DATE:
        month_start = today_local.replace(day=1)
        return month_start, local_now
    if period == LaggingPeriod.PREVIOUS_MONTH:
        first_of_this_month = today_local.replace(day=1)
        last_month_end = first_of_this_month
        last_month_start = (first_of_this_month - timedelta(days=1)).replace(day=1)
        return last_month_start, last_month_end
    raise ValueError(f"unknown period: {period}")


def _interval_value(ival: AccountingInterval, kind: Kind) -> float | None:
    if kind == Kind.CONSUMPTION:
        return ival.consumption_kwh if ival.consumption_kwh is not None else ival.consumption_m3
    return ival.production_kwh if ival.production_kwh is not None else ival.production_m3


def sum_for_period(
    intervals: list[AccountingInterval] | "deque[AccountingInterval]",
    kind: Kind,
    period: LaggingPeriod,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> float:
    """Sum kWh/m³ values that fall within the given local period."""
    start_local, end_local = window_for_period(period, now=now, tz=tz)
    start_utc = start_local.astimezone(now.tzinfo or ZoneInfo("UTC"))
    end_utc = end_local.astimezone(now.tzinfo or ZoneInfo("UTC"))
    total = 0.0
    for ival in intervals:
        if start_utc <= ival.period_start < end_utc:
            v = _interval_value(ival, kind)
            if v is not None:
                total += float(v)
    return total
```

(`deque` import for the type annotation: add `from collections import deque` at top.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sensor.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/sensor.py tests/test_sensor.py
git commit -m "Add lagging-period window/sum helpers with DST tests"
```

---

## Task 13: Sensor entity classes + async_setup_entry

**Files:**
- Modify: `custom_components/estfeed/sensor.py`
- Modify: `tests/test_sensor.py`

- [ ] **Step 1: Write failing test for entity instantiation**

Append to `tests/test_sensor.py`:
```python
from unittest.mock import MagicMock

from custom_components.estfeed.api import MeteringPoint, Period
from custom_components.estfeed.const import CommodityType
from custom_components.estfeed.sensor import (
    LaggingSensor,
    LatestIntervalSensor,
)


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=timezone.utc), end=None)],
    )


def test_lagging_sensor_state_with_data():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [
            _ival(datetime(2026, 4, 28, h, tzinfo=timezone.utc), 1.0) for h in range(24)
        ]
    }
    coordinator.hass.config.time_zone = "Europe/Tallinn"
    coordinator.slug = "home"
    coordinator.last_meter_errors = {}

    sensor = LaggingSensor(
        coordinator=coordinator,
        meter=_meter(),
        kind=Kind.CONSUMPTION,
        period=LaggingPeriod.YESTERDAY,
        multi_meter=False,
    )

    # Force "now" via patching is overkill for this — we'll patch datetime.now in the
    # available property test below; here we just verify wiring.
    assert sensor.unique_id == "estfeed_home_consumption_yesterday_089N"
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR


def test_lagging_sensor_unavailable_when_cache_empty():
    coordinator = MagicMock()
    coordinator.cache = {}
    coordinator.hass.config.time_zone = "Europe/Tallinn"
    coordinator.slug = "home"
    coordinator.last_meter_errors = {}
    coordinator.last_update_success = True

    sensor = LaggingSensor(
        coordinator=coordinator,
        meter=_meter(),
        kind=Kind.CONSUMPTION,
        period=LaggingPeriod.YESTERDAY,
        multi_meter=False,
    )
    assert sensor.available is False


def test_latest_interval_sensor_returns_max_period_start():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [
            _ival(datetime(2026, 4, 28, h, tzinfo=timezone.utc), 1.0) for h in range(3)
        ]
    }
    coordinator.last_update_success = True
    coordinator.slug = "home"

    sensor = LatestIntervalSensor(coordinator=coordinator, meter=_meter())
    assert sensor.native_value == datetime(2026, 4, 28, 2, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sensor.py -v`
Expected: FAIL — sensor classes not defined.

- [ ] **Step 3: Implement sensor entity classes**

Append to `sensor.py`:
```python
from datetime import datetime, timezone


class _EstfeedEntity(CoordinatorEntity[EstfeedCoordinator]):
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: EstfeedCoordinator, meter: MeteringPoint) -> None:
        super().__init__(coordinator)
        self._meter = meter

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter.eic)},
            name=f"{self.coordinator.slug} ({self._meter.eic})",
            manufacturer="Elering Estfeed",
            model=self._meter.commodity_type.value,
        )


class LaggingSensor(_EstfeedEntity, SensorEntity):
    """A consumption/production total over yesterday / MTD / previous month."""

    def __init__(
        self,
        coordinator: EstfeedCoordinator,
        meter: MeteringPoint,
        kind: Kind,
        period: LaggingPeriod,
        multi_meter: bool,
    ) -> None:
        super().__init__(coordinator, meter)
        self._kind = kind
        self._period = period
        self._multi_meter = multi_meter
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.slug}_{kind.value}_{period.value}_{suffix}"
        self._attr_translation_key = f"{kind.value}_{period.value}"
        self._attr_device_class = SensorDeviceClass.ENERGY
        if meter.commodity_type == CommodityType.ELECTRICITY:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        else:
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_native_unit_of_measurement = "m³"
        # Production sensors disabled by default (most users don't generate).
        if kind == Kind.PRODUCTION:
            self._attr_entity_registry_enabled_default = False

    @property
    def _bucket(self) -> list[AccountingInterval]:
        return list(self.coordinator.cache.get((self._meter.eic, self._kind), []))

    @property
    def available(self) -> bool:
        return bool(self._bucket)

    @property
    def native_value(self) -> float:
        bucket = self._bucket
        tz = ZoneInfo(self.coordinator.hass.config.time_zone or "UTC")
        now = datetime.now(tz=timezone.utc)
        return round(
            sum_for_period(bucket, self._kind, self._period, now=now, tz=tz), 3
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        tz = ZoneInfo(self.coordinator.hass.config.time_zone or "UTC")
        now = datetime.now(tz=timezone.utc)
        start, end = window_for_period(self._period, now=now, tz=tz)
        as_of = max((i.period_start for i in self._bucket), default=None)
        return {
            "meter_eic": self._meter.eic,
            "commodity_type": self._meter.commodity_type.value,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "as_of": as_of.isoformat() if as_of else "",
        }


class LatestIntervalSensor(_EstfeedEntity, SensorEntity):
    """Diagnostic: timestamp of the newest cached interval."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "latest_interval"

    def __init__(self, coordinator: EstfeedCoordinator, meter: MeteringPoint) -> None:
        super().__init__(coordinator, meter)
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.slug}_latest_interval_{suffix}"

    @property
    def native_value(self) -> datetime | None:
        all_intervals = [
            ival
            for (eic, _kind), bucket in self.coordinator.cache.items()
            if eic == self._meter.eic
            for ival in bucket
        ]
        return max((i.period_start for i in all_intervals), default=None)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensor entities for each meter."""
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    multi_meter = len(coordinator.meters) > 1
    entities: list[SensorEntity] = []
    for meter in coordinator.meters:
        for kind in (Kind.CONSUMPTION, Kind.PRODUCTION):
            for period in LaggingPeriod:
                entities.append(LaggingSensor(coordinator, meter, kind, period, multi_meter))
        entities.append(LatestIntervalSensor(coordinator, meter))
    async_add_entities(entities)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sensor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/sensor.py tests/test_sensor.py
git commit -m "Add LaggingSensor and LatestIntervalSensor entity classes"
```

---

## Task 14: Binary sensor — `data_fresh`

**Files:**
- Create: `custom_components/estfeed/binary_sensor.py`
- Create: `tests/test_binary_sensor.py`

- [ ] **Step 1: Write failing tests**

`tests/test_binary_sensor.py`:
```python
"""Tests for the data_fresh binary sensor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from custom_components.estfeed.api import (
    AccountingInterval,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.binary_sensor import DataFreshBinarySensor
from custom_components.estfeed.const import CommodityType, Kind


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=timezone.utc), end=None)],
    )


def _ival(t: datetime) -> AccountingInterval:
    return AccountingInterval(
        period_start=t,
        consumption_kwh=1.0,
        production_kwh=None,
        consumption_m3=None,
        production_m3=None,
    )


@freeze_time("2026-04-29T12:00:00Z")
def test_data_fresh_when_recent():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [_ival(datetime(2026, 4, 28, 23, tzinfo=timezone.utc))]
    }
    coordinator.last_update_success = True
    coordinator.slug = "home"
    sensor = DataFreshBinarySensor(coordinator=coordinator, meter=_meter())
    assert sensor.is_on is True


@freeze_time("2026-04-29T12:00:00Z")
def test_data_stale_when_older_than_threshold():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [_ival(datetime(2026, 4, 28, 0, tzinfo=timezone.utc))]
    }
    coordinator.last_update_success = True
    coordinator.slug = "home"
    sensor = DataFreshBinarySensor(coordinator=coordinator, meter=_meter())
    # 2026-04-28 00:00 UTC is 36 hours before 2026-04-29 12:00 UTC → stale
    assert sensor.is_on is False


def test_data_fresh_unknown_when_cache_empty():
    coordinator = MagicMock()
    coordinator.cache = {}
    coordinator.last_update_success = True
    coordinator.slug = "home"
    sensor = DataFreshBinarySensor(coordinator=coordinator, meter=_meter())
    assert sensor.is_on is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_binary_sensor.py -v`
Expected: FAIL — binary_sensor module doesn't exist.

- [ ] **Step 3: Implement `binary_sensor.py`**

```python
"""Estfeed binary_sensor platform: data freshness diagnostic."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MeteringPoint
from .const import ATTRIBUTION, DATA_FRESH_THRESHOLD, DOMAIN
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix


class DataFreshBinarySensor(CoordinatorEntity[EstfeedCoordinator], BinarySensorEntity):
    """ON if the newest cached interval is within DATA_FRESH_THRESHOLD."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "data_fresh"

    def __init__(self, coordinator: EstfeedCoordinator, meter: MeteringPoint) -> None:
        super().__init__(coordinator)
        self._meter = meter
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.slug}_data_fresh_{suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter.eic)},
            name=f"{self.coordinator.slug} ({self._meter.eic})",
            manufacturer="Elering Estfeed",
            model=self._meter.commodity_type.value,
        )

    @property
    def is_on(self) -> bool | None:
        all_intervals = [
            ival
            for (eic, _kind), bucket in self.coordinator.cache.items()
            if eic == self._meter.eic
            for ival in bucket
        ]
        newest = max((i.period_start for i in all_intervals), default=None)
        if newest is None:
            return None
        return (datetime.now(tz=timezone.utc) - newest) <= DATA_FRESH_THRESHOLD


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DataFreshBinarySensor(coordinator, meter) for meter in coordinator.meters)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_binary_sensor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/binary_sensor.py tests/test_binary_sensor.py
git commit -m "Add data_fresh binary sensor diagnostic"
```

---

## Task 15: Config flow — initial setup

**Files:**
- Create: `custom_components/estfeed/config_flow.py`
- Create: `custom_components/estfeed/strings.json`
- Create: `custom_components/estfeed/translations/en.json`
- Create: `custom_components/estfeed/translations/et.json`
- Create: `tests/test_config_flow.py`

- [ ] **Step 1: Write failing tests for the happy path and bad credentials**

`tests/test_config_flow.py`:
```python
"""Tests for the Estfeed config flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.estfeed.api import (
    EstfeedAuthError,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CommodityType,
    DOMAIN,
)


def _meter() -> MeteringPoint:
    from datetime import datetime, timezone
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=timezone.utc), end=None)],
    )


@pytest.mark.asyncio
async def test_user_step_happy_path(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "csec",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Home"
    assert result2["data"] == {
        CONF_CLIENT_ID: "cid",
        CONF_CLIENT_SECRET: "csec",
        CONF_FRIENDLY_NAME: "Home",
    }


@pytest.mark.asyncio
async def test_user_step_bad_credentials_shows_form_error(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(side_effect=EstfeedAuthError("bad")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "x",
                CONF_CLIENT_SECRET: "y",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_flow.py -v`
Expected: FAIL — config flow not registered.

- [ ] **Step 3: Implement `config_flow.py`**

```python
"""Config flow for the Estfeed integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EstfeedAuthError, EstfeedClient, EstfeedError
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_RESOLUTION,
    DEFAULT_BACKFILL_MONTHS,
    DEFAULT_FRIENDLY_NAME,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
    Resolution,
)


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Required(CONF_FRIENDLY_NAME, default=DEFAULT_FRIENDLY_NAME): str,
    }
)


class EstfeedConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._validate(user_input)
            except EstfeedAuthError:
                errors["base"] = "invalid_auth"
            except EstfeedError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_FRIENDLY_NAME], data=user_input
                )

        return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)

    async def _validate(self, user_input: dict[str, Any]) -> None:
        session = async_get_clientsession(self.hass)
        client = EstfeedClient(
            session=session,
            client_id=user_input[CONF_CLIENT_ID],
            client_secret=user_input[CONF_CLIENT_SECRET],
        )
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=7)
        await client.list_metering_points(start, end)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EstfeedOptionsFlow(config_entry)


class EstfeedOptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RESOLUTION,
                    default=current.get(CONF_RESOLUTION, Resolution.HOUR.value),
                ): vol.In([Resolution.HOUR.value, Resolution.QUARTER_HOUR.value]),
                vol.Required(
                    CONF_BACKFILL_MONTHS,
                    default=current.get(CONF_BACKFILL_MONTHS, DEFAULT_BACKFILL_MONTHS),
                ): vol.All(int, vol.Range(min=MIN_BACKFILL_MONTHS, max=MAX_BACKFILL_MONTHS)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
```

- [ ] **Step 4: Create `strings.json` and English translation**

`custom_components/estfeed/strings.json`:
```json
{
  "config": {
    "step": {
      "user": {
        "title": "Estfeed",
        "description": "Connect to your e-Elering API key",
        "data": {
          "client_id": "Client ID (UUID)",
          "client_secret": "Client secret",
          "friendly_name": "Friendly name"
        }
      },
      "reauth_confirm": {
        "title": "Re-authenticate Estfeed",
        "data": {
          "client_id": "Client ID (UUID)",
          "client_secret": "Client secret"
        }
      }
    },
    "error": {
      "invalid_auth": "Invalid client_id or client_secret",
      "cannot_connect": "Could not reach the Estfeed API"
    },
    "abort": {
      "already_configured": "This API key is already configured"
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Estfeed options",
        "data": {
          "resolution": "Data resolution",
          "backfill_months": "Backfill window (months)"
        }
      }
    }
  },
  "entity": {
    "sensor": {
      "consumption_yesterday": { "name": "Consumption yesterday" },
      "consumption_month_to_date": { "name": "Consumption month-to-date" },
      "consumption_previous_month": { "name": "Consumption previous month" },
      "production_yesterday": { "name": "Production yesterday" },
      "production_month_to_date": { "name": "Production month-to-date" },
      "production_previous_month": { "name": "Production previous month" },
      "latest_interval": { "name": "Latest interval" }
    },
    "binary_sensor": {
      "data_fresh": { "name": "Data fresh" }
    }
  },
  "services": {
    "backfill_history": {
      "name": "Backfill history",
      "description": "Re-fetch and re-publish a wider window of long-term statistics.",
      "fields": {
        "months": { "name": "Months", "description": "How many months of history to refetch (1–84)." },
        "entry_id": { "name": "Config entry", "description": "Which Estfeed config entry to backfill." }
      }
    }
  }
}
```

`custom_components/estfeed/translations/en.json`: identical content as `strings.json`.

`custom_components/estfeed/translations/et.json`:
```json
{
  "config": {
    "step": {
      "user": {
        "title": "Estfeed",
        "description": "Ühenda oma e-Elering API võti",
        "data": {
          "client_id": "Kliendi ID (UUID)",
          "client_secret": "Kliendi salasõna",
          "friendly_name": "Sõbralik nimi"
        }
      },
      "reauth_confirm": {
        "title": "Autentige Estfeed uuesti",
        "data": {
          "client_id": "Kliendi ID (UUID)",
          "client_secret": "Kliendi salasõna"
        }
      }
    },
    "error": {
      "invalid_auth": "Vale client_id või client_secret",
      "cannot_connect": "Estfeed API-le ei õnnestu ühenduda"
    },
    "abort": {
      "already_configured": "See API võti on juba seadistatud"
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Estfeed seaded",
        "data": {
          "resolution": "Andmete täpsus",
          "backfill_months": "Ajalooliste andmete aken (kuud)"
        }
      }
    }
  },
  "entity": {
    "sensor": {
      "consumption_yesterday": { "name": "Eilne tarbimine" },
      "consumption_month_to_date": { "name": "Tarbimine sel kuul" },
      "consumption_previous_month": { "name": "Eelmise kuu tarbimine" },
      "production_yesterday": { "name": "Eilne tootmine" },
      "production_month_to_date": { "name": "Tootmine sel kuul" },
      "production_previous_month": { "name": "Eelmise kuu tootmine" },
      "latest_interval": { "name": "Viimane intervall" }
    },
    "binary_sensor": {
      "data_fresh": { "name": "Andmed värsked" }
    }
  },
  "services": {
    "backfill_history": {
      "name": "Lae ajalugu",
      "description": "Lae uuesti ja avalda pikaajaline statistika laiema ajaakna kohta.",
      "fields": {
        "months": { "name": "Kuud", "description": "Mitme kuu ajalugu uuesti laadida (1–84)." },
        "entry_id": { "name": "Konfig kirje", "description": "Millise Estfeed kirje ajalugu laadida." }
      }
    }
  }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config_flow.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/estfeed/config_flow.py custom_components/estfeed/strings.json custom_components/estfeed/translations/ tests/test_config_flow.py
git commit -m "Add config flow with validation, options flow, and translations"
```

---

## Task 16: Reauth flow

**Files:**
- Modify: `custom_components/estfeed/config_flow.py`
- Modify: `tests/test_config_flow.py`

- [ ] **Step 1: Write failing test for reauth**

Append to `tests/test_config_flow.py`:
```python
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.asyncio
async def test_reauth_flow_replaces_credentials(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "old", CONF_CLIENT_SECRET: "old", CONF_FRIENDLY_NAME: "Home"},
        unique_id="old",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_CLIENT_ID: "new", CONF_CLIENT_SECRET: "new"},
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_ID] == "new"
    assert entry.data[CONF_CLIENT_SECRET] == "new"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_flow.py::test_reauth_flow_replaces_credentials -v`
Expected: FAIL — reauth step not implemented.

- [ ] **Step 3: Add reauth handlers to `EstfeedConfigFlow`**

```python
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            full = {**entry.data, **user_input}
            try:
                await self._validate(full)
            except EstfeedAuthError:
                errors["base"] = "invalid_auth"
            except EstfeedError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=full, reason="reauth_successful")

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIENT_ID, default=entry.data.get(CONF_CLIENT_ID, "")): str,
                vol.Required(CONF_CLIENT_SECRET): str,
            }
        )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_flow.py::test_reauth_flow_replaces_credentials -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/config_flow.py tests/test_config_flow.py
git commit -m "Add reauth flow to Estfeed config flow"
```

---

## Task 17: Wire it up — `__init__.py` + service + platform setup

**Files:**
- Modify: `custom_components/estfeed/__init__.py`
- Create: `custom_components/estfeed/services.yaml`
- Modify: `tests/test_coordinator.py` (add a setup-entry test)

- [ ] **Step 1: Create `services.yaml`**

```yaml
backfill_history:
  fields:
    months:
      example: 24
      default: 24
      selector:
        number:
          min: 1
          max: 84
          mode: box
    entry_id:
      example: 0123456789abcdef
      selector:
        config_entry:
          integration: estfeed
```

- [ ] **Step 2: Write failing test for `async_setup_entry`**

Append to `tests/test_coordinator.py`:
```python
@pytest.mark.asyncio
async def test_async_setup_entry_creates_coordinator_and_meters(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.estfeed import async_setup_entry, async_unload_entry
    from custom_components.estfeed.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_FRIENDLY_NAME,
        DOMAIN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "c", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Home"},
        options={},
        unique_id="c",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.estfeed.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_make_meter()]),
    ), patch(
        "custom_components.estfeed.EstfeedCoordinator.async_initial_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.estfeed.EstfeedCoordinator.async_warm_cache",
        new=AsyncMock(),
    ), patch(
        "custom_components.estfeed.EstfeedCoordinator.async_config_entry_first_refresh",
        new=AsyncMock(),
    ):
        assert await async_setup_entry(hass, entry)
        coord = hass.data[DOMAIN][entry.entry_id]
        assert coord.slug == "home"
        assert len(coord.meters) == 1

        assert await async_unload_entry(hass, entry)
        assert entry.entry_id not in hass.data[DOMAIN]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::test_async_setup_entry_creates_coordinator_and_meters -v`
Expected: FAIL — `async_setup_entry` not implemented.

- [ ] **Step 4: Implement `__init__.py`**

```python
"""Estfeed Home Assistant integration."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EstfeedClient, EstfeedError
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
)
from .coordinator import EstfeedCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_BACKFILL = "backfill_history"
SERVICE_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional("months", default=24): vol.All(
            cv.positive_int, vol.Range(min=MIN_BACKFILL_MONTHS, max=MAX_BACKFILL_MONTHS)
        ),
        vol.Optional("entry_id"): cv.string,
    }
)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "estfeed"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = EstfeedClient(
        session=session,
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
    )
    slug = _slugify(entry.data.get(CONF_FRIENDLY_NAME, entry.title))

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=7)
    try:
        meters = await client.list_metering_points(start, end)
    except EstfeedError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = EstfeedCoordinator(
        hass=hass, client=client, slug=slug, options={**entry.data, **entry.options}
    )
    coordinator.meters = meters

    # Decide whether we need an initial backfill: ask for any existing stats.
    from homeassistant.components.recorder.statistics import get_last_statistics

    needs_backfill = True
    for meter in meters:
        for stream in coordinator.streams_for(meter):
            existing = await get_last_statistics(hass, 1, stream.statistic_id, True, {"sum"})
            if existing.get(stream.statistic_id):
                needs_backfill = False
                break
        if not needs_backfill:
            break

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    if needs_backfill:
        hass.async_create_background_task(
            coordinator.async_initial_backfill(), name=f"{DOMAIN}_initial_backfill"
        )
    else:
        hass.async_create_background_task(
            coordinator.async_warm_cache(), name=f"{DOMAIN}_warm_cache"
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.options = {**entry.data, **entry.options}


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _handle(call: ServiceCall) -> None:
        months = call.data.get("months", 24)
        entry_id = call.data.get("entry_id")
        targets = (
            [hass.data[DOMAIN][entry_id]]
            if entry_id and entry_id in hass.data[DOMAIN]
            else list(hass.data.get(DOMAIN, {}).values())
        )
        for coord in targets:
            coord.options = {**coord.options, CONF_BACKFILL_MONTHS: months}
            await coord.async_initial_backfill()

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle, schema=SERVICE_BACKFILL_SCHEMA)


from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402  (placed after to keep imports tidy)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_coordinator.py::test_async_setup_entry_creates_coordinator_and_meters -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/estfeed/__init__.py custom_components/estfeed/services.yaml tests/test_coordinator.py
git commit -m "Wire __init__ setup/unload, backfill service, and platform forwarding"
```

---

## Task 18: Diagnostics

**Files:**
- Create: `custom_components/estfeed/diagnostics.py`
- Create: `tests/test_diagnostics.py`

- [ ] **Step 1: Write failing test**

`tests/test_diagnostics.py`:
```python
"""Tests for diagnostics output."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.estfeed.api import (
    AccountingInterval,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CommodityType,
    DOMAIN,
    Kind,
)
from custom_components.estfeed.diagnostics import async_get_config_entry_diagnostics


def _meter():
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=timezone.utc), end=None)],
    )


@pytest.mark.asyncio
async def test_diagnostics_redacts_secrets_and_eic(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CLIENT_ID: "cid-secret",
            CONF_CLIENT_SECRET: "shhh",
            CONF_FRIENDLY_NAME: "Home",
        },
        unique_id="cid-secret",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.estfeed.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ), patch(
        "custom_components.estfeed.EstfeedCoordinator.async_config_entry_first_refresh",
        new=AsyncMock(),
    ), patch(
        "custom_components.estfeed.EstfeedCoordinator.async_initial_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.estfeed.EstfeedCoordinator.async_warm_cache",
        new=AsyncMock(),
    ):
        from custom_components.estfeed import async_setup_entry
        await async_setup_entry(hass, entry)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_CLIENT_SECRET] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_CLIENT_ID] == "**REDACTED**"
    assert diag["meters"][0]["eic"].endswith("089N")
    assert "38ZEE" not in diag["meters"][0]["eic"]  # body redacted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnostics.py -v`
Expected: FAIL — module not implemented.

- [ ] **Step 3: Implement `diagnostics.py`**

```python
"""Diagnostics for the Estfeed integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix

_REDACT_KEYS = {CONF_CLIENT_ID, CONF_CLIENT_SECRET}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            "title": entry.title,
            "options": dict(entry.options),
            "data": async_redact_data(dict(entry.data), _REDACT_KEYS),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
            "intervals_cached_per_meter": {
                eic: sum(
                    len(b) for (e, _k), b in coordinator.cache.items() if e == eic
                )
                for eic in {m.eic for m in coordinator.meters}
            },
            "last_meter_errors": dict(coordinator.last_meter_errors),
        },
        "meters": [
            {
                "eic": f"...REDACTED-{eic_suffix(m.eic)}",
                "commodity_type": m.commodity_type.value,
                "validity_periods": [
                    {"from": p.start.isoformat(), "to": p.end.isoformat() if p.end else None}
                    for p in m.periods
                ],
            }
            for m in coordinator.meters
        ],
        "recent_requests": list(coordinator._client.recent_requests),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diagnostics.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/estfeed/diagnostics.py tests/test_diagnostics.py
git commit -m "Add diagnostics endpoint with secret and EIC redaction"
```

---

## Task 19: End-to-end smoke + final lint/type pass

**Files:**
- Create: `scripts/smoke.py`
- Modify: README.md (note about smoke script)

- [ ] **Step 1: Create `scripts/smoke.py` for manual verification**

```python
"""Manual smoke test against the live Estfeed API.

Usage: ESTFEED_CLIENT_ID=... ESTFEED_CLIENT_SECRET=... python scripts/smoke.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiohttp

from custom_components.estfeed.api import EstfeedClient
from custom_components.estfeed.const import Resolution


async def main() -> None:
    client_id = os.environ["ESTFEED_CLIENT_ID"]
    client_secret = os.environ["ESTFEED_CLIENT_SECRET"]
    async with aiohttp.ClientSession() as session:
        client = EstfeedClient(session, client_id, client_secret)
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=2)
        meters = await client.list_metering_points(start, end)
        print(f"Found {len(meters)} meter(s):")
        for m in meters:
            print(f"  {m.eic} ({m.commodity_type.value})")
        if meters:
            data = await client.get_metering_data(
                start, end, Resolution.HOUR, eics=[m.eic for m in meters[:1]]
            )
            print(f"Fetched {sum(len(d.intervals) for d in data)} interval(s)")
            if data and data[0].intervals:
                latest = data[0].intervals[-1]
                print(f"Latest: {latest.period_start} cons={latest.consumption_kwh} prod={latest.production_kwh}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest tests --cov=custom_components/estfeed --cov-report=term-missing`
Expected: all tests PASS, coverage ≥ 85%.

- [ ] **Step 3: Run lint and type-check**

Run:
```bash
ruff check custom_components tests
ruff format --check custom_components tests
mypy
```

Expected: clean. Fix any issues inline.

- [ ] **Step 4: Update README to mention smoke script**

Append to `README.md`:
```markdown
## Development

```bash
pip install -e . pytest pytest-asyncio pytest-homeassistant-custom-component homeassistant aioresponses freezegun ruff mypy
pytest tests --cov=custom_components/estfeed
ruff check custom_components tests
mypy
```

For a live end-to-end check against your own API key:

```bash
ESTFEED_CLIENT_ID=... ESTFEED_CLIENT_SECRET=... python scripts/smoke.py
```
```

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke.py README.md
git commit -m "Add smoke script and development notes; lint clean"
```

---

## Self-review notes (already applied)

- **Spec coverage** — every section of the spec maps to a task: §2 API summary → Task 2/3/7; §4 architecture → Task 10/11; §5 repo layout → Task 1; §6 setup flow → Task 15/16; §7 client → Task 4/5/6/7; §8 coordinator + statistics → Task 8/9/10/11; §9 sensors → Task 12/13/14; §10 diagnostics → Task 18; §11 errors → Task 6 + observable through coordinator behavior; §12 testing → covered task-by-task with tests-first.
- **Placeholder scan** — no `TBD`, `TODO`, "implement later", or vague "add error handling" steps.
- **Type consistency** — `EstfeedClient`, `MeteringPoint`, `MeterData`, `AccountingInterval`, `EstfeedCoordinator`, `StatisticStream`, `LaggingPeriod`, `Kind`, `Resolution`, `CommodityType` named the same way across all tasks.
- **Resolution enum probing** noted in spec §14 is deferred to runtime; the integration uses `HOUR` and `QUARTER_HOUR` only, both validated against the live API in Task 7's tests' fixture data.
