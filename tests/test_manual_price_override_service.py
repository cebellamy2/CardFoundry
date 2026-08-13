import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from manual_price_override_service import (
    ManualPriceOverrideError, create_manual_price_override, identity_hash,
    valid_override_for_binding,
)
from models import (
    Base, Batch, InventoryCard, InventorySyncJob, ManualPriceOverride,
    RemoteProductBinding,
)


@pytest.fixture
def db(tmp_path):
    target = create_engine(f"sqlite:///{tmp_path/'manual-price.db'}")
    Base.metadata.create_all(target)
    identity = {"name":"Shadowspear","set_code":"PTHB","collector_number":"236P",
                "scryfall_id":"sf-shadow","language_id":"EN","condition_id":"LP","finish_id":"FO"}
    with Session(target) as session:
        batch=Batch(batch_code="PROD",is_archived=False); session.add(batch); session.flush()
        session.add(InventoryCard(id=1,batch_id=batch.id,name="Shadowspear",set_code="PTHB",
            collector_number="236P",scryfall_id="sf-shadow",language_id="EN",
            condition_id="LP",finish_id="FO",condition="LP",finish="foil",status="available"))
        binding=RemoteProductBinding(id=1,provider="manapool",product_type="mtg_single",
            product_id="product-shadow",local_card_ids_json="[1]",
            requested_identity_json=json.dumps(identity,sort_keys=True),scryfall_id="sf-shadow",
            language_id="EN",condition_id="LP",finish_id="FO",set_code="PTHB",
            collector_number="236P",binding_status="validated",validated_at=__import__("datetime").datetime.now(),
            evidence_hash="binding-hash",evidence_json="{}")
        session.add(binding)
        held={"binding_id":1,"product_id":"product-shadow","identity":identity,"status":"hold",
              "price_classification":"hold_no_price_evidence","reason":"No seller-excluded competitor satisfies this request",
              "competitor_inventory_id":None,"market_evidence":None,"evidence_hash":"held-hash"}
        session.add(InventorySyncJob(id=1,status="completed",mode="clean_rebuild_preview",
            snapshot_json=json.dumps({"initial_price_rows":[held]})))
        session.commit()
    return target, identity


def create(session, identity, **overrides):
    values=dict(binding_id=1,source_job_id=1,expected_binding_hash="binding-hash",
                expected_identity_hash=identity_hash(identity),manual_price_cents=125,
                note="No foil pricing exists; reviewed manually",floor_cents=65)
    values.update(overrides)
    return create_manual_price_override(session,**values)


def test_manual_override_requires_hold_and_is_auditable(db):
    target,identity=db
    with Session(target) as session,session.begin(): row=create(session,identity)
    with Session(target) as session:
        row=session.query(ManualPriceOverride).one(); evidence=json.loads(row.identity_json)
        assert row.manual_price_cents==125 and row.note
        assert row.automatic_competitor_status==row.automatic_market_status=="unavailable"
        assert evidence==identity and row.evidence_hash


@pytest.mark.parametrize("changes,match",[
    ({"manual_price_cents":64},"at least 65"),
    ({"note":"   "},"reason is required"),
    ({"expected_identity_hash":"stale"},"identity changed"),
])
def test_manual_override_validation(db,changes,match):
    target,identity=db
    with Session(target) as session,pytest.raises(ManualPriceOverrideError,match=match):
        create(session,identity,**changes)


def test_unresolved_or_ambiguous_binding_rejected(db):
    target,identity=db
    for status in ("unresolved","ambiguous"):
        with Session(target) as session:
            session.get(RemoteProductBinding,1).binding_status=status; session.commit()
        with Session(target) as session,pytest.raises(ManualPriceOverrideError,match="validated"):
            create(session,identity)
        with Session(target) as session:
            session.get(RemoteProductBinding,1).binding_status="validated"; session.commit()


def test_changed_binding_invalidates_existing_override(db):
    target,identity=db
    with Session(target) as session,session.begin(): create(session,identity)
    with Session(target) as session:
        binding=session.get(RemoteProductBinding,1); override=session.query(ManualPriceOverride).one()
        binding.evidence_hash="changed"
        assert valid_override_for_binding(binding,[override],65) is None


def test_manual_override_rejected_when_automatic_evidence_is_available(db):
    target,identity=db
    with Session(target) as session:
        job=session.get(InventorySyncJob,1); payload=json.loads(job.snapshot_json)
        payload["initial_price_rows"][0].update({
            "status":"priced", "price_classification":"competitor_undercut",
            "competitor_inventory_id":"competitor",
        })
        job.snapshot_json=json.dumps(payload); session.commit()
    with Session(target) as session,pytest.raises(ManualPriceOverrideError,match="must HOLD"):
        create(session,identity)
