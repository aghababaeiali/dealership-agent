"""SQLAlchemy 2.0 ORM models.

`vehicles` and `vehicle_embeddings` hold the public catalog (real listings
data) and are NOT subject to Row-Level Security. Every other table here holds
customer-scoped data and has RLS enabled - see the RLS migration and
db/rls.py.
"""

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from dealership_agent.db.base import Base


def _enum_values(enum_cls: type[enum.StrEnum]) -> list[str]:
    """Store the enum's string values ("pending") as DB labels, not the
    Python member names ("PENDING") - `sa.Enum` uses names by default."""
    return [member.value for member in enum_cls]


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    IN_PREPARATION = "in_preparation"
    READY_FOR_DELIVERY = "ready_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class TestDriveStatus(enum.StrEnum):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_order_status_enum = SqlEnum(OrderStatus, name="order_status", values_callable=_enum_values)
_test_drive_status_enum = SqlEnum(
    TestDriveStatus, name="test_drive_status", values_callable=_enum_values
)


class Vehicle(Base):
    """Public catalog data, sourced from the real listings dataset. No RLS."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    year: Mapped[int]
    make: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    trim: Mapped[str | None] = mapped_column(String(128))
    body_style: Mapped[str | None] = mapped_column(String(32))
    fuel_type: Mapped[str | None] = mapped_column(String(32))
    mileage: Mapped[int]
    # Derived KBB fair-price midpoint. NULL for ~8% of rows where the source
    # KBB range is missing. Do NOT impute - exclude these rows explicitly
    # from price-filtered queries instead.
    price: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False))
    price_low: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False))
    price_high: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False))
    # False when price/price_low/price_high are the source data's $0.00
    # sentinel (KBB had no valuation) - see docs/DATA_PRICE_AUDIT.md. Never
    # cleared or clamped; downstream queries must exclude/mask these rows
    # explicitly instead, same policy as a NULL price.
    is_price_reliable: Mapped[bool] = mapped_column(default=True)
    seller_state: Mapped[str | None] = mapped_column(String(2))
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_clean: Mapped[str | None] = mapped_column(Text)
    is_available: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class VehicleEmbedding(Base):
    """Listing embeddings, kept separate from `vehicles` so re-embedding
    with a different model never requires touching the vehicles table.
    No RLS - embeddings are derived from public catalog data.
    """

    __tablename__ = "vehicle_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    # 384 dims: matches sentence-transformers/all-MiniLM-L6-v2 (and
    # BAAI/bge-small-en-v1.5) - our self-hosted embedding model per
    # CLAUDE.md. Both are common, cheap-to-run 384-dim sentence encoders;
    # 384 is the contract this table (and its HNSW index) is built for.
    embedding: Mapped[list[float]] = mapped_column(Vector(384))
    model_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index(
            "ix_vehicle_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Customer(Base):
    """Synthetic customer records. RLS-protected."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Order(Base):
    """A vehicle purchase/reservation. RLS-protected."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(
        _order_status_enum,
        default=OrderStatus.PENDING,
    )
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expected_delivery_date: Mapped[datetime | None]
    actual_delivery_date: Mapped[datetime | None]


class OrderStatusHistory(Base):
    """Audit trail of order status transitions. RLS-protected."""

    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[OrderStatus | None] = mapped_column(_order_status_enum)
    to_status: Mapped[OrderStatus] = mapped_column(_order_status_enum)
    changed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    note: Mapped[str | None] = mapped_column(Text)


class TestDriveBooking(Base):
    """A customer's test-drive booking. RLS-protected."""

    __tablename__ = "test_drive_bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    scheduled_at: Mapped[datetime]
    status: Mapped[TestDriveStatus] = mapped_column(
        _test_drive_status_enum,
        default=TestDriveStatus.REQUESTED,
    )


class Escalation(Base):
    """Human handoff queue. RLS-protected."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]
