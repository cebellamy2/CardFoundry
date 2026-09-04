import os

from sqlalchemy import create_engine, inspect


def _default_database_url() -> str:
    """Prefer Railway's mounted volume when present; local dev is unchanged.

    RAILWAY_VOLUME_MOUNT_PATH is set automatically by Railway at container
    runtime once a volume is attached to this service -- no manual wiring
    needed beyond attaching the volume. Falls back to the original relative
    path when it's absent, which is the case for every local/test run.
    """
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return f"sqlite:///{volume_path.rstrip('/')}/cardfoundry.db"
    return "sqlite:///./cardfoundry.db"


DATABASE_URL = os.getenv("DATABASE_URL") or _default_database_url()

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def initialize_database(bind=None):
    """Explicitly create the schema and upgrade the production database.

    Importing models must remain side-effect free.  Callers that need a
    temporary database can provide an explicit SQLAlchemy bind; production
    startup uses the module engine and then applies additive upgrades.
    """
    from models import Base

    target = bind or engine
    Base.metadata.create_all(target)
    if target is engine:
        upgrade_existing_database()


def add_missing_columns(
    table_name: str,
    columns: dict[str, str],
    bind=None,
):
    target = bind or engine
    inspector = inspect(target)

    if table_name not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"]
        for column in inspector.get_columns(table_name)
    }

    with target.begin() as connection:

        for column_name, column_type in columns.items():

            if column_name in existing_columns:
                continue

            connection.exec_driver_sql(
                f"""
                ALTER TABLE {table_name}
                ADD COLUMN {column_name} {column_type}
                """
            )


def _rename_color_identity_to_color():
    """color_identity briefly stored MTG's Commander-legality "color identity"
    (e.g. a colorless artifact with a five-color activated ability read as
    WUBRG). Renamed to color, storing the card's actual printed colors
    instead -- existing values under the old name/meaning are invalidated
    (set NULL) rather than carried over, since they mean something different
    now; a fresh backfill_color.py run repopulates them correctly.
    """
    inspector = inspect(engine)
    for table_name in ("inventory_cards", "order_items"):
        if table_name not in inspector.get_table_names():
            continue
        existing_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        if "color_identity" in existing_columns and "color" not in existing_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} RENAME COLUMN color_identity TO color"
                )
                connection.exec_driver_sql(f"UPDATE {table_name} SET color = NULL")


def upgrade_existing_database():
    _rename_color_identity_to_color()
    add_missing_columns(
        "clean_rebuild_executions",
        {
            "pricing_seal_id": "VARCHAR",
            "pricing_seal_hash": "VARCHAR",
        },
    )
    add_missing_columns(
        "inventory_cards",
        {
            "source_location": "VARCHAR",
            "finish": "VARCHAR",
            "scryfall_id": "VARCHAR",
            "color": "VARCHAR",
            "flavor_name": "VARCHAR",
            "condition": "VARCHAR",
            "bought_in_price": "FLOAT",
            "current_price": "FLOAT",
            "sold_price": "FLOAT",
            "consignment_value": "FLOAT",
            "consignment_note": "TEXT",
            "consignment_amount_owed": "FLOAT",
            "consignment_payout_status": "VARCHAR",
            "consignment_payout_id": "INTEGER",
            "mtgjson_id": "VARCHAR",
            "language_id": "VARCHAR",
            "condition_id": "VARCHAR",
            "finish_id": "VARCHAR",
            "unsellable_reason": "VARCHAR",
            "unsellable_note": "TEXT",
            "unsellable_at": "DATETIME",
            "disposition_type": "VARCHAR",
            "disposition_note": "TEXT",
            "disposition_received_description": "TEXT",
            "disposed_at": "DATETIME",
            "removal_reason": "VARCHAR",
            "removal_note": "TEXT",
            "removal_related_inventory_card_id": "INTEGER",
            "removed_at": "DATETIME",
            "inventory_exception_state": "VARCHAR NOT NULL DEFAULT 'none'",
        },
    )

    # New tables are created by initialize_database() before this additive
    # upgrade runs; no destructive migration is required for manual pricing
    # evidence.

    add_missing_columns(
        "pending_imports",
        {
            "bought_price_column": "VARCHAR",
            "proposed_batch_code": "VARCHAR",
            "source_location": "VARCHAR",
            "physical_card_count": "INTEGER",
            "validation_json": "TEXT",
            "evidence_hash": "VARCHAR",
            "workflow_version": "VARCHAR",
        },
    )

    # Older databases made batch_id mandatory because a Batch was created
    # before preview. Production previews must be stageable without creating
    # inventory structure. SQLite requires a table rebuild to relax NOT NULL.
    inspector = inspect(engine)
    pending_columns = {
        column["name"]: column for column in inspector.get_columns("pending_imports")
    }
    if pending_columns.get("batch_id", {}).get("nullable") is False:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("""
                CREATE TABLE pending_imports_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    batch_id INTEGER REFERENCES batches (id),
                    filename VARCHAR NOT NULL,
                    file_hash VARCHAR NOT NULL,
                    csv_text TEXT NOT NULL,
                    card_count INTEGER NOT NULL,
                    price_column VARCHAR,
                    created_at DATETIME NOT NULL,
                    bought_price_column VARCHAR,
                    proposed_batch_code VARCHAR,
                    source_location VARCHAR,
                    physical_card_count INTEGER,
                    validation_json TEXT,
                    evidence_hash VARCHAR,
                    workflow_version VARCHAR
                )
            """)
            names = [
                "id", "batch_id", "filename", "file_hash", "csv_text",
                "card_count", "price_column", "created_at",
                "bought_price_column", "proposed_batch_code",
                "source_location", "physical_card_count", "validation_json",
                "evidence_hash", "workflow_version",
            ]
            joined = ", ".join(names)
            connection.exec_driver_sql(
                f"INSERT INTO pending_imports_new ({joined}) "
                f"SELECT {joined} FROM pending_imports"
            )
            connection.exec_driver_sql("DROP TABLE pending_imports")
            connection.exec_driver_sql(
                "ALTER TABLE pending_imports_new RENAME TO pending_imports"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_pending_imports_batch_id ON pending_imports (batch_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_pending_imports_evidence_hash ON pending_imports (evidence_hash)"
            )
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    add_missing_columns(
        "sales_orders",
        {
            "tracking_number": "VARCHAR",
            "picked_at": "DATETIME",
            "packed_at": "DATETIME",
            "shipped_at": "DATETIME",
            "external_label": "VARCHAR",
            "remote_fulfillment_status": "VARCHAR",
            "last_synced_at": "DATETIME",
            "mana_pool_shipment_synced_at": "DATETIME",
            "review_detail": "TEXT",
            "mana_pool_shipment_released_at": "DATETIME",
            "mana_pool_shipment_release_detail": "TEXT",
            "mana_pool_shipment_failure_detail": "TEXT",
            "mana_pool_processing_synced_at": "DATETIME",
            "mana_pool_processing_failure_detail": "TEXT",
            "shipping_method": "VARCHAR",
            "shipping_name": "VARCHAR",
            "shipping_line1": "VARCHAR",
            "shipping_line2": "VARCHAR",
            "shipping_city": "VARCHAR",
            "shipping_state": "VARCHAR",
            "shipping_postal_code": "VARCHAR",
            "shipping_country": "VARCHAR",
            "shipping_cents": "INTEGER",
        },
    )

    add_missing_columns(
        "order_items",
        {
            "scryfall_id": "VARCHAR",
            "color": "VARCHAR",
            "flavor_name": "VARCHAR",
            "mtgjson_id": "VARCHAR",
            "language_id": "VARCHAR",
            "condition_id": "VARCHAR",
            "finish_id": "VARCHAR",
            "tcgsku": "VARCHAR",
            "price_cents": "INTEGER",
        },
    )

    add_missing_columns(
        "batches",
        {
            "is_archived": "BOOLEAN NOT NULL DEFAULT 0",
            "is_consignment": "BOOLEAN NOT NULL DEFAULT 0",
            "consignor_id": "INTEGER",
        },
    )

    # Track active vs. closed/removed pick-wave membership so an order can be
    # enforced (at the DB level) to belong to at most one non-terminal wave.
    # Existing rows default to 'closed' and are backfilled to 'active' only
    # when their wave is still active, preserving the invariant the old
    # application-level check already relied on.
    inspector = inspect(engine)
    pick_wave_order_columns = (
        {column["name"] for column in inspector.get_columns("pick_wave_orders")}
        if "pick_wave_orders" in inspector.get_table_names()
        else set()
    )
    pick_wave_order_status_is_new = "status" not in pick_wave_order_columns
    add_missing_columns(
        "pick_wave_orders",
        {
            "status": "VARCHAR NOT NULL DEFAULT 'closed'",
        },
    )
    if pick_wave_order_status_is_new:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                UPDATE pick_wave_orders
                SET status = 'active'
                WHERE wave_id IN (
                    SELECT id FROM pick_waves WHERE status = 'active'
                )
                """
            )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_pick_wave_orders_active_order
            ON pick_wave_orders (order_id)
            WHERE status = 'active'
            """
        )

    add_missing_columns(
        "consignors",
        {
            "portal_username": "VARCHAR",
            "portal_password_hash": "VARCHAR",
            "portal_password_salt": "VARCHAR",
        },
    )
    # ADD COLUMN can't carry a UNIQUE constraint in SQLite -- a separate
    # partial unique index enforces it for databases that already existed
    # before this column did. Partial (WHERE ... IS NOT NULL) so multiple
    # consignors with no portal login yet don't collide on NULL.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_consignors_portal_username
            ON consignors (portal_username)
            WHERE portal_username IS NOT NULL
            """
        )

    add_missing_columns(
        "remote_product_bindings",
        {
            "mtgjson_override_confirmed_at": "DATETIME",
            "mtgjson_override_note": "TEXT",
            "last_quantity_push_attempted_at": "DATETIME",
            "last_quantity_push_failure_detail": "TEXT",
        },
    )

    add_missing_columns(
        "scan_recognition_trials",
        {
            "primary_exact_match": "BOOLEAN",
            "any_candidate_match": "BOOLEAN",
            "matching_candidate_position": "INTEGER",
            "name_mismatch": "BOOLEAN",
            "candidates_json": "TEXT",
            # Every trial recorded before this column existed was part of
            # CF-SCAN-004's original torture test -- DEFAULT 'torture'
            # backfills them correctly, not just new rows going forward.
            "trial_type": "VARCHAR NOT NULL DEFAULT 'torture'",
        },
    )
    # add_missing_columns only ALTERs; a column added to an
    # already-existing table never gets the index its model declaration
    # (index=True) implies unless created explicitly here.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_scan_recognition_trials_trial_type "
            "ON scan_recognition_trials (trial_type)"
        )
    # Must run after the ADD COLUMNs above: this queries the full
    # ScanRecognitionTrial ORM model, which now selects trial_type too.
    _rescore_scan_recognition_trials()

    _relax_manual_price_override_binding_requirement()
    _allow_not_required_submission_state()


def _rescore_scan_recognition_trials():
    """CF-SCAN-004's scoring rework (2026-09-04): primary-answer accuracy
    and any-candidate accuracy, reported separately, replaced the single
    exact_match column. Existing rows (production has trial #1, the real
    smoke-test capture that surfaced the suggestions[] finding) are
    re-scored here against their own already-stored raw_response_json and
    expected_* fields, so no real data is lost in the migration.

    score_against_expected lives in card_recognition_service.py (not
    main.py) precisely so this module can call it without a circular
    import -- card_recognition_service.py only imports cardsight_service.py,
    neither of which imports database.py.
    """
    import json

    inspector = inspect(engine)
    if "scan_recognition_trials" not in inspector.get_table_names():
        return
    existing_columns = {
        column["name"] for column in inspector.get_columns("scan_recognition_trials")
    }
    if "primary_exact_match" not in existing_columns:
        return

    from sqlalchemy.orm import Session

    from card_recognition_service import score_against_expected
    from cardsight_service import normalize_cardsight_result
    from models import ScanRecognitionTrial

    with Session(engine) as session:
        trials = (
            session.query(ScanRecognitionTrial)
            .filter(
                ScanRecognitionTrial.error.is_(None),
                ScanRecognitionTrial.raw_response_json.isnot(None),
                ScanRecognitionTrial.primary_exact_match.is_(None),
            )
            .all()
        )
        if not trials:
            return
        for trial in trials:
            raw = json.loads(trial.raw_response_json)
            result = normalize_cardsight_result(raw)
            scored = score_against_expected(
                result,
                expected_name=trial.expected_name,
                expected_set_code=trial.expected_set_code,
                expected_collector_number=trial.expected_collector_number,
            )
            trial.primary_exact_match = scored["primary_exact_match"]
            trial.any_candidate_match = scored["any_candidate_match"]
            trial.matching_candidate_position = scored["matching_candidate_position"]
            trial.name_mismatch = scored["name_mismatch"]
            trial.candidates_json = json.dumps(result.get("candidates"), default=str)
        session.commit()


def _allow_not_required_submission_state():
    """A local substitution (fulfillment_substitution_service.py) can
    fill an exception's order line without ever reporting it to Mana
    Pool -- "not_required" records that honestly, distinct from
    "submitted" (which asserts a real report happened). The CHECK
    constraint on submission_state is SQL-level, not just app-level
    Python validation, so SQLite requires a table rebuild to add the
    third allowed value -- same technique as
    _relax_manual_price_override_binding_requirement above.
    """
    inspector = inspect(engine)
    if "fulfillment_exceptions" not in inspector.get_table_names():
        return
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='fulfillment_exceptions'"
        ).fetchone()
    if row and row[0] and "not_required" in row[0]:
        return

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("""
            CREATE TABLE fulfillment_exceptions_new (
                id INTEGER NOT NULL PRIMARY KEY,
                sales_order_id INTEGER NOT NULL REFERENCES sales_orders (id),
                order_item_id INTEGER NOT NULL REFERENCES order_items (id),
                pick_allocation_id INTEGER NOT NULL REFERENCES pick_allocations (id),
                inventory_card_id INTEGER NOT NULL REFERENCES inventory_cards (id),
                exception_type VARCHAR NOT NULL,
                submission_state VARCHAR NOT NULL,
                remote_resolution_state VARCHAR NOT NULL,
                inventory_resolution_state VARCHAR NOT NULL,
                note TEXT NOT NULL,
                remote_order_id VARCHAR,
                remote_line_identity_hash VARCHAR,
                remote_evidence_json TEXT,
                remote_evidence_hash VARCHAR,
                created_at DATETIME NOT NULL,
                submitted_at DATETIME,
                inventory_resolved_at DATETIME,
                remote_resolved_at DATETIME,
                resolution_note TEXT,
                CONSTRAINT ck_fulfillment_exception_type CHECK (exception_type IN ('missing', 'inventory_mismatch')),
                CONSTRAINT ck_fulfillment_exception_submission_state CHECK (submission_state IN ('needs_submission', 'submitted', 'not_required')),
                CONSTRAINT ck_fulfillment_exception_remote_state CHECK (remote_resolution_state IN ('awaiting', 'resolved_refunded', 'resolved_replaced', 'review_required')),
                CONSTRAINT ck_fulfillment_exception_inventory_state CHECK (inventory_resolution_state IN ('unresolved', 'resolved'))
            )
        """)
        names = [
            "id", "sales_order_id", "order_item_id", "pick_allocation_id",
            "inventory_card_id", "exception_type", "submission_state",
            "remote_resolution_state", "inventory_resolution_state", "note",
            "remote_order_id", "remote_line_identity_hash", "remote_evidence_json",
            "remote_evidence_hash", "created_at", "submitted_at",
            "inventory_resolved_at", "remote_resolved_at", "resolution_note",
        ]
        joined = ", ".join(names)
        connection.exec_driver_sql(
            f"INSERT INTO fulfillment_exceptions_new ({joined}) "
            f"SELECT {joined} FROM fulfillment_exceptions"
        )
        connection.exec_driver_sql("DROP TABLE fulfillment_exceptions")
        connection.exec_driver_sql(
            "ALTER TABLE fulfillment_exceptions_new RENAME TO fulfillment_exceptions"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_fulfillment_exceptions_inventory_card_id "
            "ON fulfillment_exceptions (inventory_card_id)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_fulfillment_exceptions_pick_allocation_id "
            "ON fulfillment_exceptions (pick_allocation_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_remote_order_id "
            "ON fulfillment_exceptions (remote_order_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_submission_state "
            "ON fulfillment_exceptions (submission_state)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_sales_order_id "
            "ON fulfillment_exceptions (sales_order_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_order_item_id "
            "ON fulfillment_exceptions (order_item_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_inventory_resolution_state "
            "ON fulfillment_exceptions (inventory_resolution_state)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_exception_type "
            "ON fulfillment_exceptions (exception_type)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_fulfillment_exceptions_remote_resolution_state "
            "ON fulfillment_exceptions (remote_resolution_state)"
        )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _relax_manual_price_override_binding_requirement():
    """A scryfall_id-path new-listing candidate never gets a
    RemoteProductBinding (that resolution step is deliberately skipped for
    that path), so it has no product_id/binding_evidence_hash to anchor a
    manual price override to either -- manual overrides were previously
    reachable only from the clean-rebuild workflow's own binding-first
    pricing. remote_product_binding_id/product_id/binding_evidence_hash
    become nullable and a new identity_hash column anchors the no-binding
    case instead. SQLite requires a table rebuild to relax NOT NULL.
    """
    inspector = inspect(engine)
    if "manual_price_overrides" not in inspector.get_table_names():
        return
    existing_columns = {
        column["name"]: column for column in inspector.get_columns("manual_price_overrides")
    }
    already_nullable = existing_columns.get("remote_product_binding_id", {}).get("nullable") is True
    has_identity_hash = "identity_hash" in existing_columns
    if already_nullable and has_identity_hash:
        return

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("""
            CREATE TABLE manual_price_overrides_new (
                id INTEGER NOT NULL PRIMARY KEY,
                provider VARCHAR NOT NULL,
                remote_product_binding_id INTEGER REFERENCES remote_product_bindings (id),
                identity_hash VARCHAR,
                source_inventory_sync_job_id INTEGER NOT NULL REFERENCES inventory_sync_jobs (id),
                product_id VARCHAR,
                identity_json TEXT NOT NULL,
                manual_price_cents INTEGER NOT NULL,
                note TEXT NOT NULL,
                pricing_floor_cents INTEGER NOT NULL,
                automatic_competitor_status VARCHAR NOT NULL,
                automatic_market_status VARCHAR NOT NULL,
                binding_evidence_hash VARCHAR,
                source_pricing_evidence_hash VARCHAR NOT NULL,
                evidence_hash VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                created_at DATETIME NOT NULL
            )
        """)
        names = [
            "id", "provider", "remote_product_binding_id",
            "source_inventory_sync_job_id", "product_id", "identity_json",
            "manual_price_cents", "note", "pricing_floor_cents",
            "automatic_competitor_status", "automatic_market_status",
            "binding_evidence_hash", "source_pricing_evidence_hash",
            "evidence_hash", "status", "created_at",
        ]
        joined = ", ".join(names)
        connection.exec_driver_sql(
            f"INSERT INTO manual_price_overrides_new ({joined}) "
            f"SELECT {joined} FROM manual_price_overrides"
        )
        connection.exec_driver_sql("DROP TABLE manual_price_overrides")
        connection.exec_driver_sql(
            "ALTER TABLE manual_price_overrides_new RENAME TO manual_price_overrides"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_manual_price_overrides_provider ON manual_price_overrides (provider)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_manual_price_overrides_remote_product_binding_id "
            "ON manual_price_overrides (remote_product_binding_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_manual_price_overrides_identity_hash "
            "ON manual_price_overrides (identity_hash)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_manual_price_overrides_source_inventory_sync_job_id "
            "ON manual_price_overrides (source_inventory_sync_job_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_manual_price_overrides_product_id "
            "ON manual_price_overrides (product_id)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_manual_price_overrides_evidence_hash "
            "ON manual_price_overrides (evidence_hash)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_manual_price_overrides_status ON manual_price_overrides (status)"
        )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
