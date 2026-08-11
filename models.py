from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from database import engine


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    batch_code: Mapped[str] = mapped_column(
        String,
        unique=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )


class ImportRecord(Base):
    __tablename__ = "import_records"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id"),
        index=True,
    )

    filename: Mapped[str] = mapped_column(
        String,
    )

    file_hash: Mapped[str] = mapped_column(
        String,
        index=True,
    )

    card_count: Mapped[int] = mapped_column(
        Integer,
    )

    price_column: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )

    status: Mapped[str] = mapped_column(
        String,
        default="active",
        index=True,
    )


class PendingImport(Base):
    __tablename__ = "pending_imports"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id"),
        index=True,
    )

    filename: Mapped[str] = mapped_column(
        String,
    )

    file_hash: Mapped[str] = mapped_column(
        String,
    )

    csv_text: Mapped[str] = mapped_column(
        Text,
    )

    card_count: Mapped[int] = mapped_column(
        Integer,
    )

    price_column: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )


class InventoryCard(Base):
    __tablename__ = "inventory_cards"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id"),
        index=True,
    )

    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_records.id"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String,
        index=True,
    )

    set_code: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    collector_number: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    # Original Location value supplied by TCGArchivist.
    # CardFoundry's batch_id is the real physical location.
    source_location: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    finish: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )

    scryfall_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )

    # TCGArchivist does not currently provide this.
    # We will populate it later inside CardFoundry.
    condition: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )

    price_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    scan_order: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String,
        default="available",
        index=True,
    )

    imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )


Base.metadata.create_all(engine)