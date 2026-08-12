from sqlalchemy import create_engine, inspect


DATABASE_URL = "sqlite:///./cardfoundry.db"


engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False
    },
)


def add_missing_columns(
    table_name: str,
    columns: dict[str, str],
):
    inspector = inspect(engine)

    if table_name not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"]
        for column
        in inspector.get_columns(table_name)
    }

    with engine.begin() as connection:
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
        "inventory_cards",
        {
            "source_location": "VARCHAR",
            "finish": "VARCHAR",
            "scryfall_id": "VARCHAR",
            "condition": "VARCHAR",
        },
    )

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
            "condition_id": "VARCHAR",
            "tcgsku": "VARCHAR",
        },
    )