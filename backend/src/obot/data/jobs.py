"""Background work on bots' structured data."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obot.data.repository import purge_deleted_records

log = logging.getLogger(__name__)


async def purge_records(
    sessionmaker: async_sessionmaker[AsyncSession], retention_days: int
) -> None:
    async with sessionmaker() as session:
        purged = await purge_deleted_records(session, retention_days)
        await session.commit()
    if purged:
        log.info("purged %d data records deleted over %d days ago", purged, retention_days)
