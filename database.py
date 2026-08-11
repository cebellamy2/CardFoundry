from sqlalchemy import create_engine, inspect

DATABASE_URL = "sqlite:///./cardfoundry.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def upgrade_existing_database():
    inspector = inspect(engine)

    if "inventory_cards" in inspector.get_table_names():
        existing_columns = {
            column["name"]
            for column in inspector.get_columns("inventory_cards")
        }

        inventory_columns = {
            "source_location": "VARCHAR",
            "finish": "VARCHAR",
            "scryfall_id": "VARCHAR",
            "condition": "VARCHAR",
        }

        with engine.begin() as connection:
            for column_name, column_type in inventory_columns.items():
                if column_name not in existing_columns:
                    connection.exec_driver_sql(
                        f"""
                        ALTER TABLE inventory_cards
                        ADD COLUMN {column_name} {column_type}
                        """
                    )

    inspector = inspect(engine)

    if "sales_orders" in inspector.get_table_names():
        existing_columns = {
            column["name"]
            for column in inspector.get_columns("sales_orders")
        }

        order_columns = {
            "tracking_number": "VARCHAR",
            "picked_at": "DATETIME",
            "packed_at": "DATETIME",
            "shipped_at": "DATETIME",
        }

        with engine.begin() as connection:
            for column_name, column_type in order_columns.items():
                if column_name not in existing_columns:
                    connection.exec_driver_sql(
                        f"""
                        ALTER TABLE sales_orders
                        ADD COLUMN {column_name} {column_type}
                        """
                    )