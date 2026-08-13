"""Local-only, backup-first production reset planning and execution."""

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text

from models import Base


RESET_CONFIRMATION = "RESET CARDFOUNDRY FOR PRODUCTION"
REQUIRED_SETTING_KEYS = {
    "manapool_go_live_at", "pricing_floor_cents", "pricing_undercut_cents",
}

TABLE_POLICY = {
    "app_settings": ("KEEP", "Preserve production configuration and pricing settings"),
    "inventory_cards": ("REBUILD", "Recreated by the final production inventory import"),
    "batches": ("REBUILD", "Recreated for production inventory organization"),
    "import_records": ("REBUILD", "Recreated from final production imports"),
    "pending_imports": ("CLEAR", "Discard staged development imports"),
    "pending_legacy_imports": ("CLEAR", "Discard completed/staged legacy import plans"),
    "inventory_price_history": ("CLEAR", "Discard test inventory price history"),
    "inventory_change_logs": ("CLEAR", "Discard test inventory audit events"),
    "sales_orders": ("CLEAR", "Discard simulated and development order state"),
    "order_items": ("CLEAR", "Discard transactional order lines"),
    "pick_allocations": ("CLEAR", "Discard reservations and allocation state"),
    "pick_waves": ("CLEAR", "Discard pick/pack workflow state"),
    "pick_wave_orders": ("CLEAR", "Discard pick-wave order membership"),
    "pricing_jobs": ("CLEAR", "Discard development pricing previews and jobs"),
    "inventory_sync_jobs": ("CLEAR", "Discard mirror/rebuild preview snapshots"),
    "inventory_sync_leases": ("CLEAR", "Remove stale leases; active leases block reset"),
    "remote_product_bindings": ("REBUILD", "Resolve bindings again from fresh production imports"),
    "manual_price_overrides": ("CLEAR", "Reviewed development/launch fallback price evidence"),
    "clean_rebuild_executions": ("CLEAR", "Maintenance cutover execution journals"),
    "clean_rebuild_checkpoints": ("CLEAR", "Maintenance cutover batch checkpoints"),
    "execution_pricing_seals": ("CLEAR", "Immutable maintenance execution pricing seals"),
    "floor_correction_executions": ("CLEAR", "Price-floor correction execution journals"),
    "floor_correction_checkpoints": ("CLEAR", "Price-floor correction batch checkpoints"),
}

DELETE_ORDER = [
    "pick_wave_orders", "pick_allocations", "order_items", "pick_waves",
    "sales_orders", "inventory_price_history", "inventory_change_logs",
    "inventory_cards", "pending_imports", "pending_legacy_imports",
    "import_records", "batches", "pricing_jobs", "inventory_sync_jobs",
    "clean_rebuild_checkpoints", "clean_rebuild_executions", "execution_pricing_seals",
    "floor_correction_checkpoints", "floor_correction_executions",
    "manual_price_overrides", "remote_product_bindings", "inventory_sync_leases",
]


class ProductionResetError(RuntimeError):
    pass


def _database_path(engine) -> Path:
    database = engine.url.database
    if not database or str(database) == ":memory:":
        raise ProductionResetError("Production reset requires a file-backed SQLite database")
    return Path(database).resolve()


def _expected_schema():
    return {
        name: {column.name for column in table.columns}
        for name, table in Base.metadata.tables.items()
    }


def inspect_reset_plan(engine) -> dict:
    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())
    expected = _expected_schema()
    missing = sorted(set(expected) - actual_tables)
    schema_mismatches = []
    rows = []
    with engine.connect() as connection:
        for table in sorted(actual_tables):
            count = int(connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())
            if table in TABLE_POLICY:
                action, reason = TABLE_POLICY[table]
                expected_after = count if action == "KEEP" else 0
            else:
                action, reason = "REVIEW", "Unknown table is not in the production-reset allowlist"
                expected_after = count
            rows.append({
                "table": table, "current_rows": count, "action": action,
                "expected_post_reset_rows": expected_after, "reason": reason,
            })
            if table in expected:
                columns = {column["name"] for column in inspector.get_columns(table)}
                if columns != expected[table]:
                    schema_mismatches.append({
                        "table": table, "missing_columns": sorted(expected[table] - columns),
                        "unexpected_columns": sorted(columns - expected[table]),
                    })
    unknown_with_data = [row["table"] for row in rows if row["action"] == "REVIEW" and row["current_rows"]]
    setting_keys = set()
    if "app_settings" in actual_tables:
        with engine.connect() as connection:
            setting_keys = set(connection.execute(text("SELECT key FROM app_settings")).scalars())
    active_leases = 0
    if "inventory_sync_leases" in actual_tables:
        with engine.connect() as connection:
            active_leases = int(connection.execute(text(
                "SELECT COUNT(*) FROM inventory_sync_leases WHERE expires_at > :now"
            ), {"now": datetime.now().replace(tzinfo=None)}).scalar_one())
    blockers = []
    if missing:
        blockers.append("Missing expected tables: " + ", ".join(missing))
    if schema_mismatches:
        blockers.append("Schema columns differ from the mapped model")
    if unknown_with_data:
        blockers.append("Unknown populated tables: " + ", ".join(unknown_with_data))
    if active_leases:
        blockers.append(f"{active_leases} active inventory synchronization lease(s)")
    missing_settings = sorted(REQUIRED_SETTING_KEYS - setting_keys)
    if missing_settings:
        blockers.append("Missing required settings: " + ", ".join(missing_settings))
    return {
        "dry_run": True, "database": str(_database_path(engine)), "tables": rows,
        "expected_table_count": len(expected), "actual_table_count": len(actual_tables),
        "schema_mismatches": schema_mismatches, "active_leases": active_leases,
        "required_settings": sorted(REQUIRED_SETTING_KEYS),
        "ready": not blockers, "blockers": blockers,
    }


def create_timestamped_backup(engine, backup_dir=None) -> Path:
    source_path = _database_path(engine)
    if not source_path.is_file():
        raise ProductionResetError(f"Database file does not exist: {source_path}")
    target_dir = Path(backup_dir or source_path.parent / "backups").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = target_dir / f"{source_path.stem}-production-reset-{timestamp}.db"
    try:
        with sqlite3.connect(source_path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        if not target.is_file() or target.stat().st_size < 1:
            raise OSError("Backup file is empty")
        with sqlite3.connect(target) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise OSError(f"Backup integrity check failed: {result}")
    except Exception as exc:
        if target.exists():
            target.unlink()
        raise ProductionResetError(f"Database backup failed: {exc}") from exc
    return target


def _settings_snapshot(connection):
    rows = connection.execute(text(
        "SELECT key, value, updated_at FROM app_settings ORDER BY key"
    )).mappings().all()
    return [dict(row) for row in rows]


def _snapshot_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def execute_production_reset(
    engine, confirmation, backup_dir=None, backup_function=create_timestamped_backup,
) -> dict:
    if confirmation != RESET_CONFIRMATION:
        raise ProductionResetError("Production-reset confirmation did not match")
    initial = inspect_reset_plan(engine)
    if not initial["ready"]:
        raise ProductionResetError("Reset preflight failed: " + "; ".join(initial["blockers"]))
    backup = backup_function(engine, backup_dir)
    if not backup or not Path(backup).is_file():
        raise ProductionResetError("Database backup was not created")

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            # Revalidate after acquiring the SQLite write lock.
            active = int(connection.execute(text(
                "SELECT COUNT(*) FROM inventory_sync_leases WHERE expires_at > :now"
            ), {"now": datetime.now().replace(tzinfo=None)}).scalar_one())
            if active:
                raise ProductionResetError("An active inventory synchronization lease appeared")
            settings_before = _settings_snapshot(connection)
            for table in DELETE_ORDER:
                connection.execute(text(f'DELETE FROM "{table}"'))
            settings_after = _settings_snapshot(connection)
            if settings_after != settings_before:
                raise ProductionResetError("Required application settings changed during reset")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    integrity = verify_reset_integrity(engine, expected_settings=settings_before)
    if not integrity["passed"]:
        raise ProductionResetError("Post-reset integrity check failed: " + "; ".join(integrity["failures"]))
    return {
        "status": "completed", "backup_path": str(backup),
        "settings_hash": _snapshot_hash(settings_before), "integrity": integrity,
    }


def verify_reset_integrity(engine, expected_settings=None) -> dict:
    plan = inspect_reset_plan(engine)
    failures = list(plan["blockers"])
    counts = {row["table"]: row["current_rows"] for row in plan["tables"]}
    for table, (action, _) in TABLE_POLICY.items():
        if action != "KEEP" and counts.get(table) != 0:
            failures.append(f"{table} still contains rows")
    with engine.connect() as connection:
        settings = _settings_snapshot(connection)
        sqlite_integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
    if expected_settings is not None and settings != expected_settings:
        failures.append("Application settings were not preserved")
    if sqlite_integrity != "ok":
        failures.append(f"SQLite integrity_check returned {sqlite_integrity!r}")
    application_import = "not_checked_for_non_primary_database"
    if _database_path(engine) == Path("cardfoundry.db").resolve():
        try:
            __import__("main")
            application_import = "passed"
        except Exception as exc:
            application_import = f"failed: {type(exc).__name__}: {exc}"
            failures.append("Application import failed after reset")
    return {
        "passed": not failures, "failures": failures, "table_counts": counts,
        "settings_count": len(settings), "sqlite_integrity": sqlite_integrity,
        "application_import": application_import,
    }
