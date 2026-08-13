from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from models import Base
from production_reset_service import (
    RESET_CONFIRMATION, ProductionResetError, create_timestamped_backup,
    execute_production_reset, inspect_reset_plan, verify_reset_integrity,
)


@pytest.fixture
def reset_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cardfoundry.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO app_settings (id,key,value,updated_at) "
            "VALUES (1,'pricing_floor_cents','65',:now),"
            "(2,'pricing_undercut_cents','5',:now),"
            "(3,'manapool_go_live_at','2026-08-12T00:00:00Z',:now)"
        ), {"now": datetime.now()})
        connection.execute(text(
            "INSERT INTO batches (id,batch_code,created_at,is_archived) "
            "VALUES (1,'TEST',:now,0)"
        ), {"now": datetime.now()})
        connection.execute(text(
            "INSERT INTO import_records "
            "(id,batch_id,filename,file_hash,card_count,imported_at,status) "
            "VALUES (1,1,'test.csv','hash',1,:now,'active')"
        ), {"now": datetime.now()})
        connection.execute(text(
            "INSERT INTO inventory_cards "
            "(id,batch_id,import_id,name,status,imported_at) "
            "VALUES (1,1,1,'Alpha','available',:now)"
        ), {"now": datetime.now()})
        connection.execute(text(
            "INSERT INTO pricing_jobs "
            "(id,action,status,request_json,created_at) "
            "VALUES (1,'preview','completed','{}',:now)"
        ), {"now": datetime.now()})
        connection.execute(text(
            "INSERT INTO inventory_sync_jobs "
            "(id,status,mode,snapshot_json,created_at) "
            "VALUES (1,'completed','preview','{}',:now)"
        ), {"now": datetime.now()})
    return engine


def test_dry_run_reports_every_table_and_actions(reset_engine):
    plan = inspect_reset_plan(reset_engine)
    assert plan["ready"] is True
    assert len(plan["tables"]) == len(Base.metadata.tables)
    by_table = {row["table"]: row for row in plan["tables"]}
    assert by_table["app_settings"]["action"] == "KEEP"
    assert by_table["app_settings"]["expected_post_reset_rows"] == 3
    assert by_table["inventory_cards"]["action"] == "REBUILD"
    assert by_table["inventory_cards"]["expected_post_reset_rows"] == 0
    assert by_table["pricing_jobs"]["action"] == "CLEAR"


def test_confirmation_is_exact_and_backup_not_attempted(reset_engine, tmp_path):
    called = []
    with pytest.raises(ProductionResetError, match="confirmation"):
        execute_production_reset(
            reset_engine, "reset", tmp_path,
            backup_function=lambda *args: called.append(True),
        )
    assert called == []


def test_backup_failure_leaves_database_untouched(reset_engine, tmp_path):
    def fail(*args):
        raise ProductionResetError("Database backup failed")
    with pytest.raises(ProductionResetError, match="backup failed"):
        execute_production_reset(reset_engine, RESET_CONFIRMATION, tmp_path, fail)
    assert inspect_reset_plan(reset_engine)["tables"][0]  # schema still readable
    with reset_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM inventory_cards")).scalar_one() == 1


def test_unknown_populated_table_blocks_reset(reset_engine, tmp_path):
    with reset_engine.begin() as connection:
        connection.execute(text("CREATE TABLE surprise_data (id INTEGER)"))
        connection.execute(text("INSERT INTO surprise_data VALUES (1)"))
    plan = inspect_reset_plan(reset_engine)
    assert plan["ready"] is False
    assert "surprise_data" in " ".join(plan["blockers"])
    with pytest.raises(ProductionResetError, match="preflight"):
        execute_production_reset(reset_engine, RESET_CONFIRMATION, tmp_path)


def test_reset_preserves_settings_clears_transactional_data_and_is_idempotent(reset_engine, tmp_path):
    first = execute_production_reset(reset_engine, RESET_CONFIRMATION, tmp_path)
    assert Path(first["backup_path"]).is_file()
    assert first["integrity"]["passed"] is True
    with reset_engine.connect() as connection:
        settings = dict(connection.execute(text(
            "SELECT key,value FROM app_settings"
        )).all())
        assert settings["pricing_floor_cents"] == "65"
        assert settings["pricing_undercut_cents"] == "5"
        assert settings["manapool_go_live_at"] == "2026-08-12T00:00:00Z"
        assert connection.execute(text("SELECT COUNT(*) FROM inventory_cards")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM batches")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM pricing_jobs")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM inventory_sync_jobs")).scalar_one() == 0
    second = execute_production_reset(reset_engine, RESET_CONFIRMATION, tmp_path)
    assert Path(second["backup_path"]).is_file()
    assert second["backup_path"] != first["backup_path"]
    assert verify_reset_integrity(reset_engine)["passed"] is True


def test_real_backup_is_valid_copy(reset_engine, tmp_path):
    backup = create_timestamped_backup(reset_engine, tmp_path / "backups")
    assert backup.is_file()
    copy_engine = create_engine(f"sqlite:///{backup}")
    with copy_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM inventory_cards")).scalar_one() == 1


def test_active_lease_blocks_reset(reset_engine, tmp_path):
    with reset_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO inventory_sync_leases "
            "(name,owner_token,acquired_at,expires_at) VALUES "
            "('inventory','owner',datetime('now'),datetime('now','+1 hour'))"
        ))
    plan = inspect_reset_plan(reset_engine)
    assert plan["active_leases"] == 1
    assert plan["ready"] is False
    with pytest.raises(ProductionResetError, match="active inventory"):
        execute_production_reset(reset_engine, RESET_CONFIRMATION, tmp_path)
