from sqlalchemy import create_engine, inspect

DATABASE_URL = "sqlite:///./cardfoundry.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def upgrade_existing_database():
    """
    Add newer CardFoundry inventory columns to an older
    SQLite database without deleting existing inventory.
    """

    inspector = inspect(engine)

    if "inventory_cards" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"]
        for column in inspector.get_columns("inventory_cards")
    }

    new_columns = {
        "source_location": "VARCHAR",
        "finish": "VARCHAR",
        "scryfall_id": "VARCHAR",
        "condition": "VARCHAR",
    }

    with engine.begin() as connection:
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                connection.exec_driver_sql(
                    f"""
                    ALTER TABLE inventory_cards
                    ADD COLUMN {column_name} {column_type}
                    """
                )