from sqlalchemy import create_engine, inspect


DATABASE_URL = "sqlite:///./cardfoundry.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


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


def upgrade_existing_database():
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
            "condition": "VARCHAR",
            "bought_in_price": "FLOAT",
            "current_price": "FLOAT",
            "sold_price": "FLOAT",
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
        },
    )

    # New tables are created by SQLAlchemy metadata during application import;
    # no destructive migration is required for manual pricing evidence.

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
        },
    )

    add_missing_columns(
        "order_items",
        {
            "scryfall_id": "VARCHAR",
            "mtgjson_id": "VARCHAR",
            "language_id": "VARCHAR",
            "condition_id": "VARCHAR",
            "finish_id": "VARCHAR",
            "tcgsku": "VARCHAR",
        },
    )

    add_missing_columns(
        "batches",
        {
            "is_archived": "BOOLEAN NOT NULL DEFAULT 0",
        },
    )
