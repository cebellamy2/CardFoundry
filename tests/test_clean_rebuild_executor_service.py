import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from clean_rebuild_executor_service import (
    RECOVERY_CONFIRMATION, RebuildExecutionError, canonical_evidence,
    evidence_equal, prepare_execution, recovery_report, run_or_resume,
)
from clean_rebuild_service import REBUILD_CONFIRMATION, stable_hash
from models import Base, CleanRebuildCheckpoint, CleanRebuildExecution, InventorySyncJob
from inventory_sync_service import InventoryLeaseBusy, acquire_inventory_lease


def plan(blank_batches=2, republish_batches=2):
    blank = [[{"product_type":"mtg_single","product_id":f"p{i}","quantity":0,"price_cents":None}]
             for i in range(1,blank_batches+1)]
    republish = [[{"product_type":"mtg_single","product_id":f"p{i}","quantity":i,"price_cents":100+i}]
                 for i in range(1,republish_batches+1)]
    expected = [{"product_type":"mtg_single","product_id":f"p{i}","quantity":i,
                 "price_behavior":"set_verified_initial_price","expected_price_cents":100+i}
                for i in range(1,republish_batches+1)]
    remote = [{"inventory_id":f"i{i}","product_id":f"p{i}","quantity":9,
               "price_cents":50,"effective_as_of":"now"} for i in range(1,blank_batches+1)]
    return {"summary":{"ready":True},"local_snapshot_hash":"local","remote_snapshot_hash":stable_hash(remote),
            "binding_evidence_hashes":["binding"],"initial_price_evidence":[[1,"price","priced",101]],
            "binding_evidence_aggregate_hash":"bindings","pricing_evidence_aggregate_hash":"prices",
            "expected_resulting_seller_state":expected,
            "expected_resulting_seller_state_hash":stable_hash(expected),
            "blank_rows":[{"product_id":f"p{i}"} for i in range(1,blank_batches+1)],
            "blank_payloads":blank,"republish_payloads":republish,"local_evidence":{"status_counts":{"available":3}}}


class FakeManaPool:
    def __init__(self, count=2):
        self.state={f"p{i}":{"id":f"i{i}","product_id":f"p{i}","quantity":9,"price_cents":50,"effective_as_of":"now"}
                    for i in range(1,count+1)}
        self.live=False; self.calls=0; self.fail_at=None; self.fail_mode=None
    def account(self): return {"singles_live":self.live,"verified":True,"username":"test"}
    def inventory(self,min_quantity=0): return [dict(v) for v in self.state.values()]
    def writer(self,payload):
        self.calls+=1
        should_fail=self.calls in self.fail_at if isinstance(self.fail_at,set) else self.calls==self.fail_at
        if not should_fail or self.fail_mode in {"after","partial"}:
            rows=payload[:1] if should_fail and self.fail_mode=="partial" else payload
            for row in rows:
                item=self.state.setdefault(row["product_id"],{"id":"new-"+row["product_id"],"product_id":row["product_id"],"quantity":0,"price_cents":0,"effective_as_of":"now"})
                item["quantity"]=row["quantity"]
                if row.get("price_cents") is not None:item["price_cents"]=row["price_cents"]
        if should_fail: raise TimeoutError(self.fail_mode)
        return {"ok":True}


@pytest.fixture
def db(tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path/'exec.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(InventorySyncJob(id=1,status="completed",mode="clean_rebuild_preview",snapshot_json=json.dumps(plan())))
        s.commit()
    return engine


def prepare(db,mp):
    with Session(db) as s,s.begin():
        execution=prepare_execution(s,1,REBUILD_CONFIRMATION,mp.account,mp.inventory,lambda p:True)
        return execution.execution_id


def run(db,eid,mp,confirmation=REBUILD_CONFIRMATION,validator=lambda p:True):
    with Session(db) as s:
        return run_or_resume(s,eid,confirmation,mp.writer,mp.inventory,mp.account,validator)


def test_list_tuple_canonicalization_and_genuine_staleness():
    assert canonical_evidence([(1,"x")])==[[1,"x"]]
    assert evidence_equal([(1,"x")],[[1,"x"]])
    assert not evidence_equal([(1,1801)],[[1,1723]])


def test_clean_success_has_durable_reconciled_checkpoints(db):
    mp=FakeManaPool(); eid=prepare(db,mp); result=run(db,eid,mp)
    assert result["aggregate_quantity"]==3
    with Session(db) as s:
        execution=s.query(CleanRebuildExecution).one(); assert execution.status=="completed"
        assert {c.status for c in s.query(CleanRebuildCheckpoint)}=={"reconciled_complete"}


def test_duplicate_execution_and_recovery_block_new_execution(db):
    mp=FakeManaPool(); prepare(db,mp)
    with Session(db) as s,pytest.raises(RebuildExecutionError,match="Another"):
        prepare_execution(s,1,REBUILD_CONFIRMATION,mp.account,mp.inventory,lambda p:True)


def test_active_or_recovery_execution_blocks_normal_inventory_lease(db):
    mp=FakeManaPool(); prepare(db,mp)
    with Session(db) as s,pytest.raises(InventoryLeaseBusy,match="clean rebuild"):
        acquire_inventory_lease(s,"normal")
    with Session(db) as s:
        acquire_inventory_lease(s,"recovery",allow_active_cutover=True)


def test_failure_before_write_is_failed_before_writes(db):
    mp=FakeManaPool(); eid=prepare(db,mp); mp.live=True
    with pytest.raises(RebuildExecutionError):run(db,eid,mp)
    with Session(db) as s: assert s.query(CleanRebuildExecution).one().status=="failed_before_writes"
    assert mp.calls==0


def test_timeout_after_blank_write_is_reconciled_and_continues(db):
    mp=FakeManaPool(); eid=prepare(db,mp); mp.fail_at=1; mp.fail_mode="after"
    assert run(db,eid,mp)["aggregate_quantity"]==3
    with Session(db) as s:
        first=s.query(CleanRebuildCheckpoint).filter_by(phase="blank",sequence=1).one()
        assert first.reconciliation_status=="complete_after_uncertain_write"


def test_timeout_before_write_retries_safely(db):
    mp=FakeManaPool(); eid=prepare(db,mp); mp.fail_at=1; mp.fail_mode="before"
    assert run(db,eid,mp)["aggregate_quantity"]==3
    with Session(db) as s:
        assert s.query(CleanRebuildCheckpoint).filter_by(phase="blank",sequence=1).one().retry_count==1


def test_partial_blank_enters_recovery_and_resume_uses_remaining_state(tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path/'partial.db'}"); Base.metadata.create_all(engine)
    p=plan(blank_batches=2,republish_batches=2); p["blank_payloads"]=[[{"product_type":"mtg_single","product_id":"p1","quantity":0,"price_cents":None},{"product_type":"mtg_single","product_id":"p2","quantity":0,"price_cents":None}]]
    with Session(engine) as s:s.add(InventorySyncJob(id=1,status="completed",mode="clean_rebuild_preview",snapshot_json=json.dumps(p)));s.commit()
    mp=FakeManaPool(); eid=prepare(engine,mp); mp.fail_at=1;mp.fail_mode="partial"
    with pytest.raises(TimeoutError):run(engine,eid,mp)
    with Session(engine) as s:
        assert s.query(CleanRebuildExecution).one().status=="recovery_required"
        assert recovery_report(s,eid)["uncertain_requests"][0]["phase"]=="blank"
    mp.fail_at=None
    assert run(engine,eid,mp,RECOVERY_CONFIRMATION)["aggregate_quantity"]==3


def test_restart_after_first_blank_batch_resumes_checkpoint(db):
    mp=FakeManaPool(); eid=prepare(db,mp); mp.fail_at={2,3};mp.fail_mode="before"
    with pytest.raises(TimeoutError):run(db,eid,mp)
    assert mp.state["p1"]["quantity"]==0
    mp.fail_at=None; run(db,eid,mp,RECOVERY_CONFIRMATION)
    with Session(db) as s:
        assert s.query(CleanRebuildCheckpoint).filter_by(phase="blank",sequence=1).one().retry_count==0


def test_republish_failure_and_resume_does_not_reblank_completed_publish(db):
    mp=FakeManaPool(); eid=prepare(db,mp); mp.fail_at={4,5};mp.fail_mode="before"
    with pytest.raises(TimeoutError):run(db,eid,mp)
    assert mp.state["p1"]["quantity"]==1
    mp.fail_at=None; run(db,eid,mp,RECOVERY_CONFIRMATION)
    assert (mp.state["p1"]["quantity"],mp.state["p2"]["quantity"])==(1,2)


@pytest.mark.parametrize("mutation",["quantity","price","unexpected"])
def test_final_mismatch_enters_recovery(db,mutation):
    mp=FakeManaPool(); eid=prepare(db,mp)
    original=mp.inventory; reads=0
    def inventory(min_quantity=0):
        nonlocal reads
        reads+=1; rows=original(min_quantity)
        if reads>8:
            if mutation=="quantity":rows[0]["quantity"]+=1
            elif mutation=="price":rows[0]["price_cents"]+=1
            else:rows.append({"id":"ix","product_id":"unexpected","quantity":1,"price_cents":1})
        return rows
    mp.inventory=inventory
    with pytest.raises(RebuildExecutionError):run(db,eid,mp)
    with Session(db) as s: assert s.query(CleanRebuildExecution).one().status=="recovery_required"


def test_store_live_mid_execution_stops_before_next_write(db):
    mp=FakeManaPool(); eid=prepare(db,mp); original=mp.writer
    def writer(payload):
        result=original(payload); mp.live=True; return result
    mp.writer=writer
    with pytest.raises(RebuildExecutionError):run(db,eid,mp)
    assert mp.calls==1


def test_local_change_on_recovery_stops(db):
    mp=FakeManaPool(); eid=prepare(db,mp); mp.fail_at={2,3};mp.fail_mode="before"
    with pytest.raises(TimeoutError):run(db,eid,mp)
    mp.fail_at=None
    with pytest.raises(RuntimeError,match="local changed"):
        run(db,eid,mp,RECOVERY_CONFIRMATION,lambda p:(_ for _ in ()).throw(RuntimeError("local changed")))


def test_unexpected_positive_blocks_blank_verification(db):
    mp=FakeManaPool(); eid=prepare(db,mp); original=mp.writer
    def writer(payload):
        result=original(payload)
        if mp.calls==2:mp.state["extra"]={"id":"extra","product_id":"extra","quantity":1,"price_cents":1}
        return result
    mp.writer=writer
    with pytest.raises(RebuildExecutionError,match="Blank verification failed"):run(db,eid,mp)
