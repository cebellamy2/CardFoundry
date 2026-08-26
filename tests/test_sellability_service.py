import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from clean_rebuild_service import build_clean_rebuild_preview
from consignment_service import apply_consignment_payout_if_consigned
from inventory_mirror_service import build_inventory_mirror_preview
from models import (
    Base, Batch, Consignor, InventoryCard, InventoryChangeLog, OrderItem,
    PickAllocation, RemoteProductBinding, SalesOrder,
)
from sellability_service import (
    SellabilityError, correct_card_sold_price, correct_removal_metadata,
    correct_sold_price, disposition_identity_hash,
    removal_metadata_state_hash, sellable_remote_product_ids,
    sold_price_state_hash, transition_manual_disposition, transition_sellability,
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


def test_validated_binding_cannot_return_null_mtgjson_card_to_sellable(db):
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        card.status = "unsellable"
        card.mtgjson_id = None
        session.add(RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id="bound-product",
            local_card_ids_json="[1]", requested_identity_json="{}", scryfall_id="sf-1",
            language_id="EN", condition_id="LP", finish_id="NF", set_code="SET",
            collector_number="1", binding_status="validated", validated_at=datetime.now(),
            evidence_hash="binding-evidence", evidence_json="{}",
        ))
        session.commit()
    with Session(db) as session:
        with pytest.raises(SellabilityError, match="MTGJSON|canonical"):
            transition_sellability(session, 1, "unsellable", "available")
        assert session.get(InventoryCard, 1).status == "unsellable"


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
    dashboard=main.admin_batches_page()
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


def test_ui_preview_shows_card_view_button_not_escaped_away(db,monkeypatch):
    monkeypatch.setattr(main,"engine",db)
    response=TestClient(main.app).post(
        "/inventory/1/sellability/preview",
        data={"target_status":"unsellable","reason":"personal_use","note":"<b>Deck</b>"},
    )
    assert response.status_code==200
    assert '<a href="https://api.scryfall.com/cards/sf-1?format=image&version=large" ' \
        'target="_blank" rel="noopener" class="card-view-link">View Card</a>' in response.text
    # Every other cell must still be escaped -- the raw-HTML bypass is
    # scoped to the "Card" label only, not the whole table.
    assert "&lt;b&gt;Deck&lt;/b&gt;" in response.text
    assert "<b>Deck</b>" not in response.text


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


def test_manual_disposition_queues_consignor_payout_for_consigned_card(db):
    with Session(db) as session, session.begin():
        consignor = Consignor(name="Jane")
        session.add(consignor); session.flush()
        batch = Batch(batch_code="CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
        session.add(batch); session.flush()
        card = InventoryCard(
            id=101, batch_id=batch.id, name="Lightning Bolt", set_code="SET",
            collector_number="101", scryfall_id="sf-101", mtgjson_id="mtg-101",
            language_id="EN", condition_id="LP", finish_id="NF", condition="LP",
            finish="normal", status="available",
        )
        session.add(card); session.flush()
        reviewed = disposition_identity_hash(card)
        transition_manual_disposition(
            session, 101, "available", reviewed, "local_sale", "Sold at local event", 10.00,
        )
    with Session(db) as session:
        card = session.get(InventoryCard, 101)
        assert card.status == "sold" and card.sold_price == 10.00
        assert card.consignment_amount_owed == 8.00
        assert card.consignment_payout_status == "owed"
        log = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == 101,
        ).one()
        evidence = json.loads(log.change_summary)
        assert evidence["consignment_after"]["consignment_amount_owed"] == 8.00
        assert evidence["consignment_after"]["consignment_payout_status"] == "owed"


def test_manual_disposition_leaves_consignment_fields_null_for_non_consignment_batch(db):
    with Session(db) as session, session.begin():
        card = session.get(InventoryCard, 1)
        transition_manual_disposition(
            session, 1, "available", disposition_identity_hash(card),
            "local_sale", "Sold at local event", 10.00,
        )
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        assert card.consignment_amount_owed is None
        assert card.consignment_payout_status is None
        log = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == 1,
        ).one()
        assert json.loads(log.change_summary)["consignment_after"] is None


def test_manual_disposition_with_no_received_value_does_not_queue_payout_for_consigned_card(db):
    with Session(db) as session, session.begin():
        consignor = Consignor(name="Jane")
        session.add(consignor); session.flush()
        batch = Batch(batch_code="CONSIGN-2", is_consignment=True, consignor_id=consignor.id)
        session.add(batch); session.flush()
        card = InventoryCard(
            id=102, batch_id=batch.id, name="Giant Growth", set_code="SET",
            collector_number="102", scryfall_id="sf-102", mtgjson_id="mtg-102",
            language_id="EN", condition_id="LP", finish_id="NF", condition="LP",
            finish="normal", status="available",
        )
        session.add(card); session.flush()
        reviewed = disposition_identity_hash(card)
        transition_manual_disposition(
            session, 102, "available", reviewed, "gift", "Gifted, no value received",
        )
    with Session(db) as session:
        card = session.get(InventoryCard, 102)
        assert card.sold_price is None
        assert card.consignment_amount_owed is None
        assert card.consignment_payout_status is None


@pytest.mark.parametrize("reason",["duplicate_record","reconciliation_error","personal_use"])
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


def test_removal_correction_preview_shows_previous_related_card_name(db,monkeypatch):
    with Session(db) as session, session.begin(): removed_card(session,related=2)
    monkeypatch.setattr(main,"engine",db)
    response=TestClient(main.app).post("/inventory/1/removal-correction/preview",data={
        "removal_reason":"reconciliation_error","removal_note":"Original removal",
        "related_card_id":"3","correction_reason":"Fix mistaken reference",
    })
    assert response.status_code==200
    assert "Card 2 (#2)" in response.text
    assert "<td>2</td>" not in response.text


def test_edit_page_shows_card_name_in_header_and_removal_banner(db,monkeypatch):
    with Session(db) as session, session.begin(): removed_card(session,related=2)
    monkeypatch.setattr(main,"engine",db)
    response=TestClient(main.app).get("/inventory/1/edit")
    assert response.status_code==200
    assert "Edit Physical Card: Card 1" in response.text
    assert "Card 2 (#2)" in response.text


def sold_card(session, card_id=1, sold_price=42.50):
    card = session.get(InventoryCard, card_id)
    transition_manual_disposition(
        session, card_id, "available", disposition_identity_hash(card),
        "local_sale", "Sold at local event", sold_price,
    )
    session.flush()
    return card


def test_sold_price_correction_appends_audit_and_preserves_original(db):
    with Session(db) as session, session.begin():
        card = sold_card(session); original_batch = card.batch_id
    with Session(db) as session:
        original = session.query(InventoryChangeLog).one(); original_text = original.change_summary
        card = session.get(InventoryCard, 1); state = sold_price_state_hash(card)
        with session.begin_nested():
            correct_sold_price(session, 1, state, 30.00, "Partial refund issued after shipment.")
        session.commit()
    with Session(db) as session:
        card = session.get(InventoryCard, 1); logs = session.query(InventoryChangeLog).order_by(InventoryChangeLog.id).all()
        assert card.status == "sold" and card.batch_id == original_batch
        assert card.sold_price == 30.00
        assert len(logs) == 2 and logs[0].change_summary == original_text
        correction = json.loads(logs[1].change_summary)
        assert correction["action_type"] == "sold_price_correction"
        assert correction["original_sale_log_id"] == logs[0].id
        assert correction["before"]["sold_price"] == 42.50
        assert correction["after"]["sold_price"] == 30.00
        assert correction["correction_reason"] == "Partial refund issued after shipment."


def test_sold_price_correction_recomputes_consignment_amount_owed(db):
    with Session(db) as session, session.begin():
        consignor = Consignor(name="Jane")
        session.add(consignor); session.flush()
        batch = Batch(batch_code="CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
        session.add(batch); session.flush()
        card = InventoryCard(
            id=101, batch_id=batch.id, name="Lightning Bolt", set_code="SET",
            collector_number="101", scryfall_id="sf-101", mtgjson_id="mtg-101",
            language_id="EN", condition_id="LP", finish_id="NF", condition="LP",
            finish="normal", status="sold", sold_price=10.00,
        )
        session.add(card); session.flush()
        apply_consignment_payout_if_consigned(session, card)
        session.flush()
        assert card.consignment_amount_owed == 8.00

    with Session(db) as session:
        card = session.get(InventoryCard, 101); state = sold_price_state_hash(card)
        with session.begin_nested():
            correct_sold_price(session, 101, state, 7.00, "Partial refund issued after shipment.")
        session.commit()

    with Session(db) as session:
        card = session.get(InventoryCard, 101)
        assert card.sold_price == 7.00
        assert card.consignment_amount_owed == 5.60
        assert card.consignment_payout_status == "owed"
        logs = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == 101,
        ).order_by(InventoryChangeLog.id).all()
        correction = json.loads(logs[-1].change_summary)
        assert correction["consignment_before"]["consignment_amount_owed"] == 8.00
        assert correction["consignment_after"]["consignment_amount_owed"] == 5.60


def test_sold_price_correction_leaves_consignment_fields_null_for_non_consignment_batch(db):
    with Session(db) as session, session.begin():
        sold_card(session)
    with Session(db) as session:
        card = session.get(InventoryCard, 1); state = sold_price_state_hash(card)
        with session.begin_nested():
            correct_sold_price(session, 1, state, 30.00, "Reason")
        session.commit()
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        assert card.consignment_amount_owed is None
        logs = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == 1,
        ).order_by(InventoryChangeLog.id).all()
        correction = json.loads(logs[-1].change_summary)
        assert correction["consignment_before"] is None
        assert correction["consignment_after"] is None


def test_sold_price_correction_validates_status_reason_and_staleness(db):
    with Session(db) as session, session.begin(): sold_card(session)
    with Session(db) as session:
        card = session.get(InventoryCard, 1); state = sold_price_state_hash(card)
        with pytest.raises(SellabilityError, match="reason is required"):
            correct_sold_price(session, 1, state, 30.00, "")
        with pytest.raises(SellabilityError, match="cannot be negative"):
            correct_sold_price(session, 1, state, -5, "Reason")
        card.sold_price = 99.00; session.flush()
        with pytest.raises(SellabilityError, match="changed after review"):
            correct_sold_price(session, 1, state, 30.00, "Reason")
    with Session(db) as session:
        card = session.get(InventoryCard, 2); state = sold_price_state_hash(card)
        with pytest.raises(SellabilityError, match="Only a sold"):
            correct_sold_price(session, 2, state, 30.00, "Reason")


def test_sold_price_correction_ui_preview_is_nonmutating(db, monkeypatch):
    with Session(db) as session, session.begin(): sold_card(session)
    monkeypatch.setattr(main, "engine", db)
    response = TestClient(main.app).post("/inventory/1/sold-price-correction/preview", data={
        "new_sold_price": "30.00", "correction_reason": "Partial refund issued after shipment.",
    })
    assert response.status_code == 200
    assert "Confirm Sold Price Correction" in response.text
    assert "$42.50" in response.text and "$30.00" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard, 1).sold_price == 42.50


def test_sold_price_correction_confirm_updates_price_and_redirects(db, monkeypatch):
    with Session(db) as session, session.begin(): sold_card(session)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    client = TestClient(main.app)
    preview = client.post("/inventory/1/sold-price-correction/preview", data={
        "new_sold_price": "30.00", "correction_reason": "Partial refund issued after shipment.",
    })
    import re
    state_hash = re.search(r'name="expected_state_hash" value="([^"]+)"', preview.text).group(1)
    confirm = client.post(
        "/inventory/1/sold-price-correction/confirm",
        data={
            "expected_state_hash": state_hash, "new_sold_price": "30.00",
            "correction_reason": "Partial refund issued after shipment.",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    with Session(db) as session:
        assert session.get(InventoryCard, 1).sold_price == 30.00


def test_sold_price_correction_confirm_refuses_on_stale_state(db, monkeypatch):
    with Session(db) as session, session.begin(): sold_card(session)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    response = TestClient(main.app).post(
        "/inventory/1/sold-price-correction/confirm",
        data={
            "expected_state_hash": "stale-hash", "new_sold_price": "30.00",
            "correction_reason": "Reason",
        },
    )
    assert response.status_code == 200
    assert "Sold Price Correction Refused" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard, 1).sold_price == 42.50


def test_sold_price_correction_uses_inventory_lease(monkeypatch):
    import sellability_service
    from contextlib import contextmanager
    from inventory_sync_service import InventoryLeaseBusy
    @contextmanager
    def busy():
        raise InventoryLeaseBusy("busy")
        yield
    monkeypatch.setattr(sellability_service, "inventory_sync_lease", busy)
    with pytest.raises(InventoryLeaseBusy):
        correct_card_sold_price(1, "irrelevant", 30.00, "Reason")


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
