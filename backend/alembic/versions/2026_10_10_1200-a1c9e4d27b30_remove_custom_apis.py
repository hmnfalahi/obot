"""remove custom apis

Revision ID: a1c9e4d27b30
Revises: 7ea1bd98118b
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c9e4d27b30'
down_revision: Union[str, Sequence[str], None] = '7ea1bd98118b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # usage_events.kind is a plain VARCHAR; UsageKind no longer knows 'api'.
    op.execute("DELETE FROM usage_events WHERE kind = 'api'")
    op.drop_index(op.f('ix_api_key_values_bot_id'), table_name='api_key_values')
    op.drop_table('api_key_values')
    op.drop_index(op.f('ix_api_endpoints_bot_id'), table_name='api_endpoints')
    op.drop_table('api_endpoints')


def downgrade() -> None:
    """Downgrade schema (the deleted 'api' usage rows are not restored)."""
    op.create_table('api_endpoints',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('bot_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('method', sa.String(length=8), nullable=False),
    sa.Column('url', sa.String(length=2048), nullable=False),
    sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], name=op.f('fk_api_endpoints_bot_id_bots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_endpoints')),
    sa.UniqueConstraint('bot_id', 'name', name=op.f('uq_api_endpoints_bot_id'))
    )
    op.create_index(op.f('ix_api_endpoints_bot_id'), 'api_endpoints', ['bot_id'], unique=False)
    op.create_table('api_key_values',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('bot_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('value_encrypted', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], name=op.f('fk_api_key_values_bot_id_bots'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_key_values_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_key_values')),
    sa.UniqueConstraint('user_id', 'bot_id', 'name', name=op.f('uq_api_key_values_user_id'))
    )
    op.create_index(op.f('ix_api_key_values_bot_id'), 'api_key_values', ['bot_id'], unique=False)
