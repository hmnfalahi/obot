"""Background work on bots' structured data."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obot.data.events import purge_events
from obot.data.repository import purge_deleted_records

log = logging.getLogger(__name__)


async def purge_records(
    sessionmaker: async_sessionmaker[AsyncSession], retention_days: int
) -> None:
    async with sessionmaker() as session:
        purged = await purge_deleted_records(session, retention_days)
        events = await purge_events(session, retention_days)
        await session.commit()
    if events:
        log.info("purged %d sent record events over %d days ago", events, retention_days)
    if purged:
        log.info("purged %d data records deleted over %d days ago", purged, retention_days)
