"""ask bots is built in

Revision ID: 0f917f6b325b
Revises: 41ae5af5a857
Create Date: 2026-10-05 00:03:32.848617

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0f917f6b325b'
down_revision: Union[str, Sequence[str], None] = '41ae5af5a857'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Consulting other bots is always available now, not a tool chosen per bot.
    op.execute("update bots set tools = tools - 'ask_bots'")


def downgrade() -> None:
    """Downgrade schema."""
    # Which bots had the tool is not kept; they start without it.
