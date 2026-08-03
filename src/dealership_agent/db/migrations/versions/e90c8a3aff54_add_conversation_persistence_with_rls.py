"""add conversation persistence with rls

Revision ID: e90c8a3aff54
Revises: cb4ac97d0b71
Create Date: 2026-08-03 20:24:47.806939

Step 9, Part B3: multi-turn conversation state, scoped by Row-Level
Security exactly like every other customer table (see
05d78e206134_enable_row_level_security.py). `conversation_messages` has
no customer_id column of its own - it's scoped via its parent
conversation, same pattern as order_status_history via orders.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dealership_agent.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "e90c8a3aff54"
down_revision: str | Sequence[str] | None = "cb4ac97d0b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUSTOMER_ID_SCOPE = "NULLIF(current_setting('app.customer_id', true), '')::integer"


def _app_role_name() -> str:
    settings = get_settings()
    if not settings.app_db_user:
        raise RuntimeError("APP_DB_USER must be set to grant conversation table access.")
    return settings.app_db_user


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_ref", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversations_conversation_ref"),
        "conversations",
        ["conversation_ref"],
        unique=True,
    )
    op.create_index(
        op.f("ix_conversations_customer_id"), "conversations", ["customer_id"], unique=False
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversation_messages_conversation_id"),
        "conversation_messages",
        ["conversation_id"],
        unique=False,
    )

    app_role = _app_role_name()
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON conversations TO {app_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_messages TO {app_role}")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO " + app_role)

    for table in ("conversations", "conversation_messages"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        f"""
        CREATE POLICY conversations_isolation ON conversations
            USING (customer_id = {CUSTOMER_ID_SCOPE})
            WITH CHECK (customer_id = {CUSTOMER_ID_SCOPE})
        """
    )
    # No customer_id column of its own - scope via the parent conversation,
    # same pattern as order_status_history_isolation.
    op.execute(
        f"""
        CREATE POLICY conversation_messages_isolation ON conversation_messages
            USING (
                conversation_id IN (
                    SELECT id FROM conversations WHERE customer_id = {CUSTOMER_ID_SCOPE}
                )
            )
            WITH CHECK (
                conversation_id IN (
                    SELECT id FROM conversations WHERE customer_id = {CUSTOMER_ID_SCOPE}
                )
            )
        """  # noqa: S608 -- CUSTOMER_ID_SCOPE is a fixed constant, not user input
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS conversation_messages_isolation ON conversation_messages")
    op.execute("DROP POLICY IF EXISTS conversations_isolation ON conversations")

    for table in ("conversations", "conversation_messages"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    app_role = _app_role_name()
    op.execute(f"REVOKE ALL PRIVILEGES ON conversation_messages FROM {app_role}")
    op.execute(f"REVOKE ALL PRIVILEGES ON conversations FROM {app_role}")

    op.drop_index(
        op.f("ix_conversation_messages_conversation_id"), table_name="conversation_messages"
    )
    op.drop_table("conversation_messages")
    op.drop_index(op.f("ix_conversations_customer_id"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_conversation_ref"), table_name="conversations")
    op.drop_table("conversations")
