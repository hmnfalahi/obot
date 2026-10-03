"""Run the factory bot and all active managed bots with long polling (development).

    cd backend && uv run python -m obot.telegram.run

Production runs obot.server instead, which receives updates by webhook.
"""

import asyncio
import logging

from obot.config import get_settings
from obot.telegram.bootstrap import start_services, stop_services


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    services = await start_services(get_settings(), "polling")
    try:
        await asyncio.Event().wait()
    finally:
        await stop_services(services)


if __name__ == "__main__":
    asyncio.run(main())
