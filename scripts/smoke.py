"""Manual smoke test against the live Estfeed API.

Usage: ESTFEED_CLIENT_ID=... ESTFEED_CLIENT_SECRET=... python scripts/smoke.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import aiohttp

from custom_components.estfeed.api import EstfeedClient
from custom_components.estfeed.const import Resolution


async def main() -> None:
    client_id = os.environ["ESTFEED_CLIENT_ID"]
    client_secret = os.environ["ESTFEED_CLIENT_SECRET"]
    async with aiohttp.ClientSession() as session:
        client = EstfeedClient(session, client_id, client_secret)
        end = datetime.now(tz=UTC)
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
                print(
                    f"Latest: {latest.period_start} "
                    f"cons={latest.consumption_kwh} prod={latest.production_kwh}"
                )


if __name__ == "__main__":
    asyncio.run(main())
