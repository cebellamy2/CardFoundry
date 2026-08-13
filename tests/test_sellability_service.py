import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from clean_rebuild_service import build_clean_rebuild_preview
from inventory_mirror_service import build_inventory_mirror_preview
from models import (
    Base, Batch, InventoryCard, InventoryChangeLog, OrderItem,
    PickAllocation, SalesOrder,
)
from sellability_service import (
    SellabilityError, correct_removal_metadata, disposition_identity_hash,
    removal_metadata_state_hash, sellable_remote_product_ids,
    transition_manual_disposition, transition_sellability,
    transition_inventory_removal,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'sellability.db'}", connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        batch = Batch(batch_code="PROD", is_archived=False)
        session.add(batch); session.flush()
        for card_id, status in ((1,"available"),(2,"available"),(3,"available")):
            session.add(InventoryCard(
                id=card_id, batch_id=batch.id, name=f"Card {card_id}", set_code="SET",
                collector_number=str(card_id), scryfall_id=f"sf-{card_id}",
                mtgjson_id=f"mtg-{card_id}", language_id="EN", condition_id="LP",
                finish_id="NF", condition="LP", finish="normal", status=status,
            ))
        session.commit()
    return engine


def test_available_to_unsellable_preserves_batch_and_audits_reason_note(db):
    with Session(db) as session, session.begin():
        card=session.get(InventoryCard,1); original_batch=card.batch_id
        transition_sellability(session,1,"available","unsellable","personal_use","Atraxa Superfriends")
        assert card.status=="unsellable" and card.batch_id==original_batch
        assert (card.unsellable_reason,card.unsellable_note)==("personal_use","Atraxa Superfriends")
    with Session(db) as session:
        evidence=json.loads(session.query(InventoryChangeLog).one().change_summary)
        assert evidence["action_type"]=="mark_unsellable"
        assert evidence["batch"]["batch_code"]=="PROD"
        assert evidence["previous_status"]=="available" and evidence["new_status"]=="unsellable"


def test_damaged_card_can_return_and_history_retains_reason(db):
    with Session(db) as session, session.begin():
        transition_sellability(session,1,"available","unsellable","damaged","Bent corner")
    with Session(db) as session, session.begin():
        card=transition_sellability(session,1,"unsellable","available")
        assert card.status=="available" and card.unsellable_reason is None
    with Session(db) as session:
        rows=[json.loads(x.change_summary) for x in session.query(InventoryChangeLog).order_by(InventoryChangeLog.id)]
        assert rows[-1]["action_type"]=="return_to_sellable"
        assert (rows[-1]["reason"],rows[-1]["note"])==("damaged","Bent corner")


@pytest.mark.parametrize("status",["reserved","sold"])
def test_reserved_and_sold_cannot_be_overridden(db,status):
    with Session(db) as session:
        session.get(InventoryCard,1).status=status; session.commit()
    with Session(db) as session, pytest.raises(SellabilityError):
        transition_sellability(session,1,status,"unsellable","hold",None)


def test_active_allocation_and_stale_state_fail_closed(db):
    with Session(db) as session:
        order=SalesOrder(external_order_id="order",status="needs_review")
        session.add(order); session.flush()
        item=OrderItem(order_id=order.id,name="Card 1",quantity=1)
        session.add(item); session.flush()
        session.add(PickAllocation(
            inventory_card_id=1, order_item_id=item.id, batch_id=1, status="allocated",
        )); session.commit()
    with Session(db) as session, pytest.raises(SellabilityError,match="active allocation"):
        transition_sellability(session,1,"available","unsellable","hold",None)
    with Session(db) as session, pytest.raises(SellabilityError,match="status changed"):
        transition_sellability(session,2,"unsellable","available")


def test_archived_batch_blocks_return(db):
    with Session(db) as session, session.begin():
        transition_sellability(session,1,"available","unsellable","trade",None)
    with Session(db) as session:
        session.query(Batch).one().is_archived=True; session.commit()
    with Session(db) as session, pytest.raises(SellabilityError,match="Archived"):
        transition_sellability(session,1,"unsellable","available")


def test_unsellable_counts_zero_in_mirror_and_clean_rebuild(db):
    with Session(db) as session, session.begin():
        transition_sellability(session,3,"available","unsellable","display",None)
    with Session(db) as session:
        cards=session.query(InventoryCard).all(); batches={b.id:b for b in session.query(Batch)}
        remote=[]
        for card in cards:
            remote.append({"id":f"i-{card.id}","product_id":f"p-{card.id}","product_type":"mtg_single","quantity":1,"price_cents":100,"product":{"single":{"name":card.name,"set":"SET","number":str(card.id),"mtgjson_id":card.mtgjson_id,"language_id":"EN","condition_id":"LP","finish_id":"NF"}}})
        mirror=build_inventory_mirror_preview(cards,batches,[],remote)
        assert sum(r.get("desired_quantity",0) for r in mirror["rows"] if r.get("category")!="remote_only_unmanaged")==2
        rebuild=build_clean_rebuild_preview(cards,batches,[],remote,[],{"results":[]})
        assert rebuild["summary"]["eligible_local_copies"]==2
        assert rebuild["summary"]["copies_to_republish"]==2
        assert rebuild["summary"]["owned_not_for_sale"]==1
        assert rebuild["not_sellable_rows"][0]["inventory_card_id"]==3


def test_unsellable_product_is_excluded_from_pricing_apply_eligibility(db):
    with Session(db) as session, session.begin():
        transition_sellability(session,1,"available","unsellable","hold",None)
    remote=[]
    for card_id in (1,2):
        remote.append({"product_id":f"p-{card_id}","product":{"single":{
            "mtgjson_id":f"mtg-{card_id}","language_id":"EN",
            "condition_id":"LP","finish_id":"NF",
        }}})
    with Session(db) as session:
        assert sellable_remote_product_ids(session,remote)=={"p-2"}


def test_inventory_filter_and_dashboard_counts(db,monkeypatch):
    with Session(db) as session, session.begin():
        transition_sellability(session,1,"available","unsellable","personal_use","Deck")
    monkeypatch.setattr(main,"engine",db)
    search=main.inventory_search(show_all=True,status="unsellable")
    assert "NOT FOR SALE" in search and "Card 1" in search and "Card 2" not in search
    dashboard=main.home()
    assert "Not For Sale" in dashboard and "Total Owned" in dashboard


def test_ui_preview_shows_exact_card_and_requires_confirmation(db,monkeypatch):
    monkeypatch.setattr(main,"engine",db)
    response=TestClient(main.app).post(
        "/inventory/1/sellability/preview",
        data={"target_status":"unsellable","reason":"personal_use","note":"Deck"},
    )
    assert response.status_code==200
    assert "Confirm Mark Not For Sale" in response.text
    assert "Card 1" in response.text and "PROD" in response.text and "personal_use" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard,1).status=="available"


def test_manual_disposition_ui_preview_is_nonmutating_and_requires_note(db,monkeypatch):
    monkeypatch.setattr(main,"engine",db)
    client=TestClient(main.app)
    refused=client.post("/inventory/1/disposition/preview",data={
        "disposition_type":"local_sale","transaction_note":"   ","value":"10",
    })
    assert refused.status_code==400
    reviewed=client.post("/inventory/1/disposition/preview",data={
        "disposition_type":"trade","transaction_note":"Local trade",
        "value":"25","received_description":"Cards received",
    })
    assert reviewed.status_code==200
    assert "Confirm Mark Sold / Traded Locally" in reviewed.text
    assert "Local trade" in reviewed.text and "Cards received" in reviewed.text
    with Session(db) as session:
        assert session.get(InventoryCard,1).status=="available"


def test_transition_service_has_no_manapool_dependency():
    import sellability_service
    assert "manapool_service" not in sellability_service.__dict__


def test_local_sale_marks_sold_persists_value_batch_and_audit(db):
    with Session(db) as session, session.begin():
        card=session.get(InventoryCard,1); batch_id=card.batch_id
        transition_manual_disposition(
            session,1,"available",disposition_identity_hash(card),
            "local_sale","Sold at local event",42.50,
        )
        assert card.status=="sold" and card.sold_price==42.50 and card.batch_id==batch_id
        assert card.disposition_type=="local_sale" and card.disposition_note=="Sold at local event"
    with Session(db) as session:
        evidence=json.loads(session.query(InventoryChangeLog).one().change_summary)
        assert evidence["action_type"]=="manual_disposition"
        assert evidence["previous_status"]=="available" and evidence["new_status"]=="sold"
        assert evidence["value"]==42.50 and evidence["batch"]["batch_code"]=="PROD"


def test_trade_persists_estimated_value_and_received_description(db):
    with Session(db) as session, session.begin():
        card=session.get(InventoryCard,1)
        transition_manual_disposition(
            session,1,"available",disposition_identity_hash(card),
            "trade","Traded locally",30,"Two commander staples",
        )
        assert card.sold_price==30
        assert card.disposition_received_description=="Two commander staples"


@pytest.mark.parametrize("note",["","   "])
def test_manual_disposition_requires_nonblank_note(db,note):
    with Session(db) as session:
        card=session.get(InventoryCard,1); reviewed=disposition_identity_hash(card)
        with pytest.raises(SellabilityError,match="note is required"):
            transition_manual_disposition(session,1,"available",reviewed,"gift",note)


@pytest.mark.parametrize("status",["reserved","sold","unsellable"])
def test_nonavailable_card_cannot_be_manually_disposed(db,status):
    with Session(db) as session:
        card=session.get(InventoryCard,1); card.status=status; session.commit()
        reviewed=disposition_identity_hash(card)
    with Session(db) as session, pytest.raises(SellabilityError,match="Only an available"):
        transition_manual_disposition(session,1,status,reviewed,"other","Disposition")


def test_disposition_rejects_active_allocation_and_stale_identity(db):
    with Session(db) as session:
        card=session.get(InventoryCard,1); reviewed=disposition_identity_hash(card)
        card.collector_number="changed"; session.commit()
    with Session(db) as session, pytest.raises(SellabilityError,match="identity or batch changed"):
        transition_manual_disposition(session,1,"available",reviewed,"gift","Gifted")

    with Session(db) as session:
        card=session.get(InventoryCard,2); reviewed=disposition_identity_hash(card)
        order=SalesOrder(external_order_id="disposition-order",status="needs_review")
        session.add(order); session.flush()
        item=OrderItem(order_id=order.id,name="Card 2",quantity=1)
        session.add(item); session.flush()
        session.add(PickAllocation(
            inventory_card_id=2,order_item_id=item.id,batch_id=1,status="allocated",
        )); session.commit()
    with Session(db) as session, pytest.raises(SellabilityError,match="active allocation"):
        transition_manual_disposition(session,2,"available",reviewed,"other","Local")


def test_manually_sold_card_is_excluded_from_quantities_rebuild_and_allocation(db):
    with Session(db) as session, session.begin():
        card=session.get(InventoryCard,1)
        transition_manual_disposition(
            session,1,"available",disposition_identity_hash(card),"gift","Birthday gift",
        )
    with Session(db) as session:
        cards=session.query(InventoryCard).all(); batches={b.id:b for b in session.query(Batch)}
        remote=[]
        for card in cards:
            remote.append({"id":f"i-{card.id}","product_id":f"p-{card.id}","product_type":"mtg_single","quantity":1,"price_cents":100,"product":{"single":{"name":card.name,"set":"SET","number":str(card.id),"mtgjson_id":card.mtgjson_id,"language_id":"EN","condition_id":"LP","finish_id":"NF"}}})
        mirror=build_inventory_mirror_preview(cards,batches,[],remote)
        assert sum(r.get("desired_quantity",0) for r in mirror["rows"] if r.get("category")!="remote_only_unmanaged")==2
        rebuild=build_clean_rebuild_preview(cards,batches,[],remote,[],{"results":[]})
        assert rebuild["summary"]["eligible_local_copies"]==2


@pytest.mark.parametrize("reason",["duplicate_record","reconciliation_error"])
def test_available_to_removed_preserves_batch_import_and_audits(db,reason):
    with Session(db) as session:
        card=session.get(InventoryCard,1); card.import_id=77; session.commit()
    with Session(db) as session, session.begin():
        card=session.get(InventoryCard,1); batch_id=card.batch_id; import_id=card.import_id
        transition_inventory_removal(
            session,1,"available",disposition_identity_hash(card),reason,
            "Duplicate from reconciliation",2,
        )
        assert card.status=="removed"
        assert (card.batch_id,card.import_id)==(batch_id,import_id)
        assert card.removal_related_inventory_card_id==2
    with Session(db) as session:
        evidence=json.loads(session.query(InventoryChangeLog).one().change_summary)
        assert evidence["action_type"]=="inventory_removal"
        assert evidence["removal_reason"]==reason
        assert evidence["related_inventory_card_id"]==2
        assert evidence["import_record_id"]==77


@pytest.mark.parametrize("reason,note",[("bad_reason","note"),("scan_error",""),("scan_error","  ")])
def test_removal_requires_valid_reason_and_nonblank_note(db,reason,note):
    with Session(db) as session:
        card=session.get(InventoryCard,1); reviewed=disposition_identity_hash(card)
        with pytest.raises(SellabilityError):
            transition_inventory_removal(session,1,"available",reviewed,reason,note)


def test_related_card_must_exist_and_differ(db):
    with Session(db) as session:
        card=session.get(InventoryCard,1); reviewed=disposition_identity_hash(card)
        with pytest.raises(SellabilityError,match="different"):
            transition_inventory_removal(session,1,"available",reviewed,"other","Correction",1)
        with pytest.raises(SellabilityError,match="not found"):
            transition_inventory_removal(session,1,"available",reviewed,"other","Correction",999)


@pytest.mark.parametrize("status",["reserved","sold","removed","unsellable"])
def test_nonavailable_card_cannot_be_removed(db,status):
    with Session(db) as session:
        card=session.get(InventoryCard,1); card.status=status; session.commit()
        reviewed=disposition_identity_hash(card)
    with Session(db) as session, pytest.raises(SellabilityError,match="Only an available"):
        transition_inventory_removal(session,1,status,reviewed,"other","Correction")


def test_removal_rejects_active_allocation_and_stale_identity(db):
    with Session(db) as session:
        card=session.get(InventoryCard,1); reviewed=disposition_identity_hash(card)
        card.name="Changed"; session.commit()
    with Session(db) as session, pytest.raises(SellabilityError,match="identity or batch changed"):
        transition_inventory_removal(session,1,"available",reviewed,"scan_error","Correction")
    with Session(db) as session:
        card=session.get(InventoryCard,2); reviewed=disposition_identity_hash(card)
        order=SalesOrder(external_order_id="remove-order",status="needs_review")
        session.add(order); session.flush(); item=OrderItem(order_id=order.id,name="Card 2",quantity=1)
        session.add(item); session.flush(); session.add(PickAllocation(
            inventory_card_id=2,order_item_id=item.id,batch_id=1,status="allocated",
        )); session.commit()
    with Session(db) as session, pytest.raises(SellabilityError,match="active allocation"):
        transition_inventory_removal(session,2,"available",reviewed,"other","Correction")


def test_removed_excluded_from_owned_quantity_rebuild_and_pricing_eligibility(db):
    with Session(db) as session, session.begin():
        card=session.get(InventoryCard,1)
        transition_inventory_removal(
            session,1,"available",disposition_identity_hash(card),"never_owned","Bad count",
        )
    remote=[]
    with Session(db) as session:
        cards=session.query(InventoryCard).all(); batches={b.id:b for b in session.query(Batch)}
        for card in cards:
            remote.append({"id":f"i-{card.id}","product_id":f"p-{card.id}","product_type":"mtg_single","quantity":1,"price_cents":100,"product":{"single":{"name":card.name,"set":"SET","number":str(card.id),"mtgjson_id":card.mtgjson_id,"language_id":"EN","condition_id":"LP","finish_id":"NF"}}})
        mirror=build_inventory_mirror_preview(cards,batches,[],remote)
        assert sum(r.get("desired_quantity",0) for r in mirror["rows"] if r.get("category")!="remote_only_unmanaged")==2
        rebuild=build_clean_rebuild_preview(cards,batches,[],remote,[],{"results":[]})
        assert rebuild["summary"]["eligible_local_copies"]==2
        assert rebuild["summary"]["removed_historical_records"]==1
        assert rebuild["removed_rows"][0]["inventory_card_id"]==1
        assert sellable_remote_product_ids(session,remote)=={"p-2","p-3"}


def test_removal_ui_preview_is_nonmutating_and_shows_warning(db,monkeypatch):
    monkeypatch.setattr(main,"engine",db); client=TestClient(main.app)
    refused=client.post("/inventory/1/removal/preview",data={
        "removal_reason":"scan_error","removal_note":"   ","related_card_id":"2",
    })
    assert refused.status_code==400
    reviewed=client.post("/inventory/1/removal/preview",data={
        "removal_reason":"reconciliation_error","removal_note":"Duplicate record",
        "related_card_id":"2",
    })
    assert reviewed.status_code==200
    assert "THIS CARD WILL NO LONGER COUNT AS PHYSICAL OWNED INVENTORY" in reviewed.text
    assert "Card 2" in reviewed.text and "PROD" in reviewed.text
    with Session(db) as session:
        assert session.get(InventoryCard,1).status=="available"


def removed_card(session, card_id=1, related=None):
    card=session.get(InventoryCard,card_id)
    transition_inventory_removal(
        session,card_id,"available",disposition_identity_hash(card),
        "reconciliation_error","Original removal",related,
    )
    session.flush()
    return card


def test_removed_metadata_correction_appends_audit_and_preserves_original(db):
    with Session(db) as session, session.begin():
        card=removed_card(session); original_batch=card.batch_id
    with Session(db) as session:
        original=session.query(InventoryChangeLog).one(); original_text=original.change_summary
        card=session.get(InventoryCard,1); state=removal_metadata_state_hash(card)
        with session.begin_nested():
            correct_removal_metadata(
                session,1,state,"reconciliation_error","Original removal",2,
                "Added surviving-card reference omitted by original event.",
            )
        session.commit()
    with Session(db) as session:
        card=session.get(InventoryCard,1); logs=session.query(InventoryChangeLog).order_by(InventoryChangeLog.id).all()
        assert card.status=="removed" and card.batch_id==original_batch
        assert card.removal_related_inventory_card_id==2
        assert len(logs)==2 and logs[0].change_summary==original_text
        correction=json.loads(logs[1].change_summary)
        assert correction["action_type"]=="removal_metadata_correction"
        assert correction["original_inventory_removal_log_id"]==logs[0].id
        assert correction["before"]["related_inventory_card_id"] is None
        assert correction["after"]["related_inventory_card_id"]==2


def test_removal_correction_validates_related_self_status_and_staleness(db):
    with Session(db) as session, session.begin(): removed_card(session)
    with Session(db) as session:
        card=session.get(InventoryCard,1); state=removal_metadata_state_hash(card)
        with pytest.raises(SellabilityError,match="different"):
            correct_removal_metadata(session,1,state,"other","Note",1,"Reason")
        with pytest.raises(SellabilityError,match="not found"):
            correct_removal_metadata(session,1,state,"other","Note",999,"Reason")
        card.removal_note="changed"; session.flush()
        with pytest.raises(SellabilityError,match="changed after review"):
            correct_removal_metadata(session,1,state,"other","Note",2,"Reason")
    with Session(db) as session:
        card=session.get(InventoryCard,2); state=removal_metadata_state_hash(card)
        with pytest.raises(SellabilityError,match="Only a removed"):
            correct_removal_metadata(session,2,state,"other","Note",3,"Reason")


def test_duplicate_removal_without_related_card_shows_strong_warning(db,monkeypatch):
    monkeypatch.setattr(main,"engine",db)
    response=TestClient(main.app).post("/inventory/1/removal/preview",data={
        "removal_reason":"duplicate_record","removal_note":"Duplicate","related_card_id":"",
    })
    assert response.status_code==200
    assert "No surviving InventoryCard has been linked to this correction" in response.text


def test_removal_correction_ui_shows_both_cards_and_is_nonmutating(db,monkeypatch):
    with Session(db) as session, session.begin(): removed_card(session)
    monkeypatch.setattr(main,"engine",db)
    response=TestClient(main.app).post("/inventory/1/removal-correction/preview",data={
        "removal_reason":"reconciliation_error","removal_note":"Original removal",
        "related_card_id":"2","correction_reason":"Add surviving reference",
    })
    assert response.status_code==200
    assert "1: Card 1" in response.text and "2: Card 2" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard,1).removal_related_inventory_card_id is None


def test_removal_metadata_amendment_uses_inventory_lease(monkeypatch):
    import sellability_service
    from contextlib import contextmanager
    from inventory_sync_service import InventoryLeaseBusy
    @contextmanager
    def busy():
        raise InventoryLeaseBusy("busy")
        yield
    monkeypatch.setattr(sellability_service,"inventory_sync_lease",busy)
    with pytest.raises(InventoryLeaseBusy,match="busy"):
        sellability_service.amend_removal_metadata(
            1,"hash","other","note",None,"correction",
        )
