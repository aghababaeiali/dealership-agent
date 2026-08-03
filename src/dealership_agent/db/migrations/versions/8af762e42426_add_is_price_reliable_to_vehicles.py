"""add is_price_reliable to vehicles

Revision ID: 8af762e42426
Revises: 05d78e206134
Create Date: 2026-08-03 13:14:18.333189

See docs/DATA_PRICE_AUDIT.md: 961 rows have price/price_low/price_high
exactly $0.00 in the source data - a KBB "no valuation" sentinel, not a
real price. `server_default='true'` backfills all existing rows as
reliable; the follow-up data reload (clean_listings.py + load_listings.py)
sets the ~961 sentinel rows to false via the normal idempotent upsert.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8af762e42426"
down_revision: str | Sequence[str] | None = "05d78e206134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "vehicles",
        sa.Column(
            "is_price_reliable", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("vehicles", "is_price_reliable")
