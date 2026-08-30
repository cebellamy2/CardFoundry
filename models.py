from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_consignment: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    consignor_id: Mapped[int | None] = mapped_column(
        ForeignKey("consignors.id"), nullable=True, index=True,
    )


class Consignor(Base):
    __tablename__ = "consignors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    contact_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    payout_method: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    portal_username: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True, index=True,
    )
    portal_password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    portal_password_salt: Mapped[str | None] = mapped_column(String, nullable=True)


class ConsignorSession(Base):
    __tablename__ = "consignor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consignor_id: Mapped[int] = mapped_column(ForeignKey("consignors.id"), index=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ConsignorPayout(Base):
    __tablename__ = "consignor_payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consignor_id: Mapped[int] = mapped_column(ForeignKey("consignors.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ConsignorPayoutChangeLog(Base):
    __tablename__ = "consignor_payout_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consignor_payout_id: Mapped[int] = mapped_column(
        ForeignKey("consignor_payouts.id"), index=True,
    )
    change_summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ConsignorCredentialChangeLog(Base):
    """UX epic item 19 (Section 22.5 exception to the epic's general
    "no new audit trails" rule) -- narrowly scoped to portal credential
    changes only. Never stores a password or password hash, only what
    changed structurally (username, session-invalidation count)."""
    __tablename__ = "consignor_credential_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consignor_id: Mapped[int] = mapped_column(ForeignKey("consignors.id"), index=True)
    change_summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


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
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("batches.id"), nullable=True, index=True,
    )
    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String)
    csv_text: Mapped[str] = mapped_column(Text)
    card_count: Mapped[int] = mapped_column(Integer)
    price_column: Mapped[str | None] = mapped_column(String, nullable=True)
    bought_price_column: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_batch_code: Mapped[str | None] = mapped_column(String, nullable=True)
    source_location: Mapped[str | None] = mapped_column(String, nullable=True)
    physical_card_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    workflow_version: Mapped[str | None] = mapped_column(String, nullable=True)
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
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    condition: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mtgjson_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    language_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    finish_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    inventory_exception_state: Mapped[str] = mapped_column(
        String, nullable=False, default="none", server_default="none", index=True,
    )
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    bought_in_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sold_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    consignment_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    consignment_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    consignment_amount_owed: Mapped[float | None] = mapped_column(Float, nullable=True)
    consignment_payout_status: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True,
    )
    consignment_payout_id: Mapped[int | None] = mapped_column(
        ForeignKey("consignor_payouts.id"), nullable=True, index=True,
    )
    disposition_type: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    disposition_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    disposition_received_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removal_reason: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    removal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    removal_related_inventory_card_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_cards.id"), nullable=True, index=True,
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_order: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="available", index=True)
    unsellable_reason: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    unsellable_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    unsellable_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class InventoryListingStatus(Base):
    """A per-card cache of whether the card's canonical identity currently
    has a confirmed live Mana Pool listing -- Mana Pool listings are
    per-identity, not per-card (see inventory_mirror_service.py), so every
    sellable card sharing an identity gets the same value. Populated
    only by the existing mirror-preview reconciliation (Perform Sync,
    the exceptions review page, batch-scoped "Get This Batch Live"), not
    a live per-request lookup -- see listing_status_updates_from_rows()."""
    __tablename__ = "inventory_listing_status"

    inventory_card_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_cards.id"), primary_key=True,
    )
    listing_status: Mapped[str] = mapped_column(String, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


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
    shipping_method: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_name: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_line1: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_line2: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_city: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_state: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_postal_code: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_country: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    picked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    packed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mana_pool_shipment_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    mana_pool_shipment_released_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    mana_pool_shipment_release_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    mana_pool_shipment_failure_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    mana_pool_processing_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    mana_pool_processing_failure_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    review_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    set_code: Mapped[str | None] = mapped_column(String, nullable=True)
    collector_number: Mapped[str | None] = mapped_column(String, nullable=True)
    finish: Mapped[str | None] = mapped_column(String, nullable=True)
    scryfall_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    mtgjson_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    language_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True)
    finish_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tcgsku: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)


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


class FulfillmentException(Base):
    __tablename__ = "fulfillment_exceptions"
    __table_args__ = (
        CheckConstraint(
            "exception_type IN ('missing', 'inventory_mismatch')",
            name="ck_fulfillment_exception_type",
        ),
        CheckConstraint(
            "submission_state IN ('needs_submission', 'submitted')",
            name="ck_fulfillment_exception_submission_state",
        ),
        CheckConstraint(
            "remote_resolution_state IN ('awaiting', 'resolved_refunded', 'resolved_replaced', 'review_required')",
            name="ck_fulfillment_exception_remote_state",
        ),
        CheckConstraint(
            "inventory_resolution_state IN ('unresolved', 'resolved')",
            name="ck_fulfillment_exception_inventory_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), index=True)
    pick_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("pick_allocations.id"), unique=True, index=True,
    )
    inventory_card_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_cards.id"), unique=True, index=True,
    )
    exception_type: Mapped[str] = mapped_column(String, index=True)
    submission_state: Mapped[str] = mapped_column(
        String, default="needs_submission", index=True,
    )
    remote_resolution_state: Mapped[str] = mapped_column(
        String, default="awaiting", index=True,
    )
    inventory_resolution_state: Mapped[str] = mapped_column(
        String, default="unresolved", index=True,
    )
    note: Mapped[str] = mapped_column(Text)
    remote_order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    remote_line_identity_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    remote_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_evidence_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    inventory_resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FulfillmentExceptionEvent(Base):
    __tablename__ = "fulfillment_exception_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fulfillment_exception_id: Mapped[int] = mapped_column(
        ForeignKey("fulfillment_exceptions.id"), index=True,
    )
    event_type: Mapped[str] = mapped_column(String, index=True)
    previous_state: Mapped[str | None] = mapped_column(String, nullable=True)
    new_state: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    operator_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)


class PickWave(Base):
    __tablename__ = "pick_waves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PickWaveOrder(Base):
    __tablename__ = "pick_wave_orders"
    __table_args__ = (
        # An order may only be an active member of one non-terminal pick
        # wave at a time. This is enforced at the database level (not just
        # in service code) because membership must fail closed.
        Index(
            "ux_pick_wave_orders_active_order",
            "order_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wave_id: Mapped[int] = mapped_column(ForeignKey("pick_waves.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PickWaveEvent(Base):
    """Immutable lifecycle record for a pick wave (currently: reopen)."""
    __tablename__ = "pick_wave_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pick_wave_id: Mapped[int] = mapped_column(ForeignKey("pick_waves.id"), index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    note: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class InventorySyncLease(Base):
    __tablename__ = "inventory_sync_leases"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    owner_token: Mapped[str] = mapped_column(String, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class InventorySyncJob(Base):
    __tablename__ = "inventory_sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String, default="completed", index=True)
    mode: Mapped[str] = mapped_column(String, default="maintenance_preview", index=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CleanRebuildExecution(Base):
    __tablename__ = "clean_rebuild_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    preview_job_id: Mapped[int] = mapped_column(ForeignKey("inventory_sync_jobs.id"), index=True)
    pricing_seal_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    pricing_seal_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="prepared", index=True)
    current_phase: Mapped[str] = mapped_column(String, default="prepared", index=True)
    preview_evidence_json: Mapped[str] = mapped_column(Text)
    expected_seller_state_hash: Mapped[str] = mapped_column(String)
    confirmation_hash: Mapped[str] = mapped_column(String)
    store_off_evidence_json: Mapped[str] = mapped_column(Text)
    local_snapshot_evidence_json: Mapped[str] = mapped_column(Text)
    remote_prewrite_snapshot_json: Mapped[str] = mapped_column(Text)
    recovery_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CleanRebuildCheckpoint(Base):
    __tablename__ = "clean_rebuild_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("clean_rebuild_executions.id"), index=True)
    phase: Mapped[str] = mapped_column(String, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    total_batches: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="not_attempted", index=True)
    request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    readback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    readback_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    reconciliation_status: Mapped[str | None] = mapped_column(String, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class ExecutionPricingSeal(Base):
    __tablename__ = "execution_pricing_seals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seal_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    preview_job_id: Mapped[int] = mapped_column(ForeignKey("inventory_sync_jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    pricing_policy_hash: Mapped[str] = mapped_column(String)
    inventory_plan_hash: Mapped[str] = mapped_column(String)
    pricing_evidence_hash: Mapped[str] = mapped_column(String)
    seal_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    sealed_plan_json: Mapped[str] = mapped_column(Text)
    pricing_rows_json: Mapped[str] = mapped_column(Text)
    guardrails_json: Mapped[str] = mapped_column(Text)
    movement_report_json: Mapped[str] = mapped_column(Text)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RemoteProductBinding(Base):
    __tablename__ = "remote_product_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String, default="manapool", index=True)
    product_type: Mapped[str] = mapped_column(String, index=True)
    product_id: Mapped[str] = mapped_column(String, index=True)
    local_card_ids_json: Mapped[str] = mapped_column(Text)
    requested_identity_json: Mapped[str] = mapped_column(Text)
    scryfall_id: Mapped[str] = mapped_column(String, index=True)
    mtgjson_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    language_id: Mapped[str] = mapped_column(String, index=True)
    condition_id: Mapped[str] = mapped_column(String, index=True)
    finish_id: Mapped[str] = mapped_column(String, index=True)
    set_code: Mapped[str] = mapped_column(String, index=True)
    collector_number: Mapped[str] = mapped_column(String, index=True)
    binding_status: Mapped[str] = mapped_column(String, default="validated", index=True)
    validated_at: Mapped[datetime] = mapped_column(DateTime)
    catalog_as_of: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    evidence_json: Mapped[str] = mapped_column(Text)
    remote_inventory_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mtgjson_override_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mtgjson_override_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ManualPriceOverride(Base):
    __tablename__ = "manual_price_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String, default="manapool", index=True)
    # Nullable: a scryfall_id-path new-listing candidate never gets a
    # RemoteProductBinding (that resolution step is skipped by design for
    # that path -- see new_listing_pricing_service.py), so it has no
    # product_id/binding_evidence_hash to anchor to either. Exactly one of
    # (remote_product_binding_id, identity_hash) is set, enforced in
    # manual_price_override_service.py rather than at the schema level.
    remote_product_binding_id: Mapped[int | None] = mapped_column(
        ForeignKey("remote_product_bindings.id"), nullable=True, index=True,
    )
    identity_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    source_inventory_sync_job_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_sync_jobs.id"), index=True,
    )
    product_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    identity_json: Mapped[str] = mapped_column(Text)
    manual_price_cents: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text)
    pricing_floor_cents: Mapped[int] = mapped_column(Integer)
    automatic_competitor_status: Mapped[str] = mapped_column(String)
    automatic_market_status: Mapped[str] = mapped_column(String)
    binding_evidence_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_pricing_evidence_hash: Mapped[str] = mapped_column(String)
    evidence_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


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


class FloorCorrectionExecution(Base):
    __tablename__ = "floor_correction_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    preview_job_id: Mapped[int] = mapped_column(ForeignKey("pricing_jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="prepared", index=True)
    current_phase: Mapped[str] = mapped_column(String, default="prepared")
    preview_hash: Mapped[str] = mapped_column(String)
    confirmation_hash: Mapped[str] = mapped_column(String)
    store_off_evidence_json: Mapped[str] = mapped_column(Text)
    remote_prewrite_snapshot_json: Mapped[str] = mapped_column(Text)
    recovery_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FloorCorrectionCheckpoint(Base):
    __tablename__ = "floor_correction_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("floor_correction_executions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    total_batches: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="not_attempted", index=True)
    request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    readback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    readback_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    reconciliation_status: Mapped[str | None] = mapped_column(String, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
