from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from database import engine


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ImportRecord(Base):
    __tablename__ = "import_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String, index=True)
    card_count: Mapped[int] = mapped_column(Integer)
    price_column: Mapped[str | None] = mapped_column(String, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    status: Mapped[str] = mapped_column(String, default="active", index=True)


class PendingImport(Base):
    __tablename__ = "pending_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String)
    csv_text: Mapped[str] = mapped_column(Text)
    card_count: Mapped[int] = mapped_column(Integer)
    price_column: Mapped[str | None] = mapped_column(String, nullable=True)
    bought_price_column: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PendingLegacyImport(Base):
    __tablename__ = "pending_legacy_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String, index=True)
    plan_json: Mapped[str] = mapped_column(Text)
    source_physical_total: Mapped[int] = mapped_column(Integer)
    planned_import_total: Mapped[int] = mapped_column(Integer)
    already_represented_total: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class InventoryCard(Base):
    __tablename__ = "inventory_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_records.id"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, index=True)
    set_code: Mapped[str | None] = mapped_column(String, nullable=True)
    collector_number: Mapped[str | None] = mapped_column(String, nullable=True)
    source_location: Mapped[str | None] = mapped_column(String, nullable=True)
    finish: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    scryfall_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    condition: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    bought_in_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sold_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    scan_order: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="available", index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class InventoryPriceHistory(Base):
    __tablename__ = "inventory_price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inventory_card_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_cards.id"),
        index=True,
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    old_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String, default="manual")


class InventoryChangeLog(Base):
    __tablename__ = "inventory_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inventory_card_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_cards.id"),
        index=True,
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )
    change_summary: Mapped[str] = mapped_column(Text)


class SalesOrder(Base):
    __tablename__ = "sales_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_order_id: Mapped[str] = mapped_column(String, index=True)
    external_label: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default="simulation", index=True)
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    remote_fulfillment_status: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )
    tracking_number: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    picked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    packed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    set_code: Mapped[str | None] = mapped_column(String, nullable=True)
    collector_number: Mapped[str | None] = mapped_column(String, nullable=True)
    finish: Mapped[str | None] = mapped_column(String, nullable=True)
    scryfall_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tcgsku: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class PickAllocation(Base):
    __tablename__ = "pick_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), index=True)
    inventory_card_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_cards.id"),
        unique=True,
        index=True,
    )
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="allocated", index=True)
    allocated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PickWave(Base):
    __tablename__ = "pick_waves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PickWaveOrder(Base):
    __tablename__ = "pick_wave_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wave_id: Mapped[int] = mapped_column(ForeignKey("pick_waves.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PricingJob(Base):
    __tablename__ = "pricing_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_job_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="submitted", index=True)
    request_json: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


Base.metadata.create_all(engine)
