"""remove user daily credit cap

Revision ID: b7d3f1a85c42
Revises: a1c9e4d27b30
Create Date: 2026-10-10 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d3f1a85c42'
down_revision: Union[str, Sequence[str], None] = 'a1c9e4d27b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('user_bot_settings', 'daily_credit_cap')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('user_bot_settings', sa.Column('daily_credit_cap', sa.BigInteger(), nullable=True))
