from types import SimpleNamespace

import pytest

from inventory_mirror_service import (
    MAINTENANCE_CONFIRMATION,
    build_inventory_mirror_preview,
    quantity_only_payload,
    validate_reviewed_snapshots,
)


def card(card_id, quantity_identity="A", status="available", archived=False, **overrides):
    values = {
        "id": card_id,
        "batch_id": 2 if archived else 1,
        "name": f"Card {quantity_identity}",
        "set_code": "SET",
        "collector_number": quantity_identity,
        "mtgjson_id": f"mtg-{quantity_identity}",
        "language_id": "EN",
        "condition_id": "LP",
        "finish_id": "NF",
        "status": status,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def remote(identity="A", quantity=1, price=100, inventory_id=None, **overrides):
    single = {
        "name": f"Card {identity}",
        "set": "SET",
        "number": identity,
        "mtgjson_id": f"mtg-{identity}",
        "language_id": "EN",
        "condition_id": "LP",
        "finish_id": "NF",
    }
    single.update(overrides)
    return {
        "id": inventory_id or f"inventory-{identity}",
        "product_id": f"product-{identity}-{single['language_id']}-{single['condition_id']}-{single['finish_id']}",
        "product_type": "mtg_single",
        "quantity": quantity,
        "price_cents": price,
        "effective_as_of": "2026-08-13T00:00:00Z",
        "product": {"single": single},
    }


def preview(cards, remote_rows, allocations=()):
    batches = {
        1: SimpleNamespace(id=1, is_archived=False),
        2: SimpleNamespace(id=2, is_archived=True),
    }
    return build_inventory_mirror_preview(cards, batches, list(allocations), remote_rows)


def categories(result):
    return {row["category"] for row in result["rows"]}


def test_available_reserved_and_archived_are_counted_once():
    result = preview([
        card(1), card(2), card(3, status="reserved"), card(4, archived=True),
    ], [remote(quantity=1)])
    row = next(row for row in result["rows"] if row["category"] == "increase_quantity")
    assert row["desired_quantity"] == 2
    assert row["local_contributing_card_ids"] == [1, 2]


def test_exact_multilingual_variants_do_not_merge():
    result = preview([
        card(1), card(2, language_id="JA"),
    ], [remote(), remote(inventory_id="ja", language_id="JA")])
    assert result["summary"]["categories"] == {"hold_equal": 2}


@pytest.mark.parametrize("language", ["JA", "CS", "CT", "RU", "PH"])
def test_publishing_identity_preserves_explicit_language(language):
    result = preview(
        [card(1, language_id=language)],
        [remote(language_id=language)],
    )
    row = result["rows"][0]
    assert row["canonical_identity"]["language_id"] == language
    assert row["category"] == "hold_equal"


def test_publishing_identity_defaults_missing_language_to_english():
    result = preview([card(1, language_id=None)], [remote(language_id="EN")])
    row = result["rows"][0]
    assert row["canonical_identity"]["language_id"] == "EN"
    assert row["category"] == "hold_equal"


def test_higher_lower_equal_and_zero_categories():
    cards = [
        card(1, "A"), card(2, "A"),
        card(3, "B"),
        card(4, "C"),
        card(5, "D", status="reserved"),
    ]
    result = preview(cards, [
        remote("A", quantity=1),
        remote("B", quantity=2),
        remote("C", quantity=1),
        remote("D", quantity=1),
    ])
    assert categories(result) == {
        "increase_quantity", "decrease_quantity", "hold_equal", "zero_candidate",
    }
    assert result["summary"]["exact_quantity_writes"] == 3


def test_historical_only_identity_with_no_remote_record_produces_no_row():
    # Every local card sharing this identity is sold -- nothing sellable to
    # list and no remote record to reconcile, so nothing actionable exists.
    result = preview([card(1, "A", status="sold")], [])
    assert result["rows"] == []
    assert result["summary"]["categories"] == {}


def test_local_only_and_remote_only_are_distinct_and_unmanaged_not_zeroed():
    result = preview([card(1, "A")], [remote("B", quantity=7)])
    assert categories(result) == {"local_only_requires_listing", "remote_only_unmanaged"}
    assert result["summary"]["exact_quantity_writes"] == 0
    assert quantity_only_payload(result["rows"]) == []


def test_missing_canonical_identity_hard_fails_with_card_id():
    with pytest.raises(ValueError, match="MTGJSON identity: 1"):
        preview(
        [card(1, mtgjson_id=None), card(2, "B")],
        [remote("B"), remote("B", inventory_id="duplicate")],
    )


def test_unresolved_identity_is_skipped_not_fail_closed_when_permissive():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None), card(2, "B")],
        batches, [], [remote("B")],
        fail_closed_on_unresolved=False,
    )
    assert result["unresolved_card_ids"] == [1]
    # Card 1 was never groupable by canonical_key() regardless of mode --
    # relaxing the fail-closed check doesn't change what gets synced for
    # everything else.
    assert categories(result) == {"hold_equal"}


def test_unresolved_identity_list_is_empty_when_nothing_unresolved():
    result = preview([card(1, "A")], [remote("A")])
    assert result["unresolved_card_ids"] == []


def test_ambiguous_remote_identity_remains_fail_closed():
    result = preview(
        [card(2, "B")],
        [remote("B"), remote("B", inventory_id="duplicate")],
    )
    assert categories(result) == {"ambiguous_identity"}


# --- name (every row needs a human name, not just an mtgjson_id) ---

def test_row_carries_local_card_name():
    result = preview([card(1, "A")], [remote("A")])
    assert result["rows"][0]["name"] == "Card A"


def test_remote_only_unmanaged_row_falls_back_to_remote_listing_name():
    result = preview([], [remote("A", name="Remote-Only Card")])
    row = next(r for r in result["rows"] if r["category"] == "remote_only_unmanaged")
    assert row["name"] == "Remote-Only Card"


def test_ambiguous_identity_row_shows_every_distinct_conflicting_name():
    # The whole point of this row is that the names disagree -- joining
    # both, not picking one, is what makes the ambiguity visible.
    result = preview(
        [card(1, "A", name="Local Name")],
        [remote("A", name="Remote Name")],
    )
    row = next(r for r in result["rows"] if r["category"] == "ambiguous_identity")
    assert row["name"] == "Local Name / Remote Name"


def test_remote_missing_identity_row_carries_a_name():
    result = preview([], [remote("A", name="Incomplete Card", mtgjson_id="")])
    row = next(
        r for r in result["rows"]
        if r["category"] == "ambiguous_identity" and r["reason"].startswith("Remote inventory lacks")
    )
    assert row["name"] == "Incomplete Card"


def test_stale_local_or_remote_snapshot_aborts_entire_apply():
    reviewed = preview([card(1)], [remote()])
    fresh_local = preview([card(1), card(2)], [remote()])
    with pytest.raises(ValueError, match="Local inventory changed"):
        validate_reviewed_snapshots(reviewed, fresh_local, MAINTENANCE_CONFIRMATION)

    fresh_remote = preview([card(1)], [remote(quantity=2)])
    with pytest.raises(ValueError, match="Mana Pool inventory changed"):
        validate_reviewed_snapshots(reviewed, fresh_remote, MAINTENANCE_CONFIRMATION)


def test_apply_requires_exact_maintenance_confirmation():
    reviewed = preview([card(1)], [remote()])
    with pytest.raises(ValueError, match="confirmation"):
        validate_reviewed_snapshots(reviewed, reviewed, "STORE IS ON")
    assert validate_reviewed_snapshots(
        reviewed, reviewed, MAINTENANCE_CONFIRMATION,
    ) is True


def test_quantity_payload_preserves_price_with_null():
    result = preview([card(1), card(2)], [remote(quantity=1, price=345)])
    assert quantity_only_payload(result["rows"]) == [{
        "product_type": "mtg_single",
        "product_id": "product-A-EN-LP-NF",
        "price_cents": None,
        "quantity": 2,
    }]


def test_mtgjson_override_lists_a_card_with_no_documented_mtgjson_id():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None)], batches, [], [],
        mtgjson_override_product_ids={1: "product-override-A"},
    )
    assert result["unresolved_card_ids"] == []
    assert categories(result) == {"local_only_requires_listing"}
    row = result["rows"][0]
    assert row["local_contributing_card_ids"] == [1]
    assert row["canonical_identity"]["language_id"] == "EN"


def test_mtgjson_override_does_not_trip_fail_closed():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    # Would normally raise ValueError for lacking canonical MTGJSON identity.
    build_inventory_mirror_preview(
        [card(1, mtgjson_id=None)], batches, [], [],
        mtgjson_override_product_ids={1: "product-override-A"},
    )


def test_mtgjson_override_reconciles_against_a_remote_listing_with_no_mtgjson_id():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    remote_row = remote(quantity=1)
    remote_row["product_id"] = "product-override-A"
    remote_row["product"]["single"]["mtgjson_id"] = None
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None), card(2, mtgjson_id=None)],
        batches, [], [remote_row],
        mtgjson_override_product_ids={1: "product-override-A", 2: "product-override-A"},
    )
    assert categories(result) == {"increase_quantity"}
    row = next(row for row in result["rows"] if row["category"] == "increase_quantity")
    assert row["desired_quantity"] == 2
    assert row["remote_product_id"] == "product-override-A"


def test_mtgjson_override_only_matches_its_own_confirmed_product_id():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    remote_row = remote(quantity=1)
    remote_row["product_id"] = "product-unrelated"
    remote_row["product"]["single"]["mtgjson_id"] = None
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None)], batches, [], [remote_row],
        mtgjson_override_product_ids={1: "product-override-A"},
    )
    # The remote row's product_id isn't the confirmed override, so it's
    # still an unmatched/ambiguous remote record, and card 1 still lists
    # under its own override identity rather than merging with it.
    assert categories(result) == {"local_only_requires_listing", "ambiguous_identity"}


# --- pending_first_listing_card_ids (no mtgjson_id, no binding at all) ---

def test_pending_first_listing_lists_a_card_with_no_binding_or_mtgjson():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None, scryfall_id="sf-a")], batches, [], [],
        pending_first_listing_card_ids={1},
    )
    assert result["unresolved_card_ids"] == []
    assert categories(result) == {"local_only_requires_listing"}
    row = result["rows"][0]
    assert row["local_contributing_card_ids"] == [1]
    assert row["name"] == "Card A"


def test_pending_first_listing_does_not_trip_fail_closed():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    # Would normally raise ValueError for lacking canonical MTGJSON identity.
    build_inventory_mirror_preview(
        [card(1, mtgjson_id=None, scryfall_id="sf-a")], batches, [], [],
        pending_first_listing_card_ids={1},
    )


def test_pending_first_listing_reconciles_once_the_first_listing_goes_live():
    # After publish, Mana Pool's own listing for this exact scryfall_id
    # still won't carry an mtgjson_id either (nobody documents one yet) --
    # the fallback has to match it on the remote side too, or the card
    # would show as "never published" forever even once it's live.
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    remote_row = remote(quantity=1)
    remote_row["product_id"] = "product-new"
    remote_row["product"]["single"]["scryfall_id"] = "sf-a"
    remote_row["product"]["single"]["mtgjson_id"] = None
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None, scryfall_id="sf-a")], batches, [], [remote_row],
        pending_first_listing_card_ids={1},
    )
    assert categories(result) == {"hold_equal"}


def test_pending_first_listing_without_a_scryfall_id_stays_unresolved():
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None, scryfall_id=None)], batches, [], [],
        pending_first_listing_card_ids={1},
        fail_closed_on_unresolved=False,
    )
    assert result["unresolved_card_ids"] == [1]
    assert result["rows"] == []


def test_pending_first_listing_never_reclassifies_an_unrelated_remote_row():
    # A remote listing with no mtgjson_id that has nothing to do with any
    # pending_first_listing card stays exactly where it already landed
    # (ambiguous_identity via remote_missing) -- the fallback key only
    # ever applies when it matches one of these specific cards' own.
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    remote_row = remote(quantity=1)
    remote_row["product"]["single"]["scryfall_id"] = "sf-unrelated"
    remote_row["product"]["single"]["mtgjson_id"] = None
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None, scryfall_id="sf-a")], batches, [], [remote_row],
        pending_first_listing_card_ids={1},
    )
    assert categories(result) == {"local_only_requires_listing", "ambiguous_identity"}


def test_pending_first_listing_card_id_alone_is_not_enough_without_the_flag():
    # Being unresolved doesn't get a free pass -- the caller must
    # explicitly mark a card as pending_first_listing (i.e. confirmed to
    # have zero existing bindings) for the fallback to apply at all.
    batches = {1: SimpleNamespace(id=1, is_archived=False)}
    result = build_inventory_mirror_preview(
        [card(1, mtgjson_id=None, scryfall_id="sf-a")], batches, [], [],
        fail_closed_on_unresolved=False,
    )
    assert result["unresolved_card_ids"] == [1]
    assert result["rows"] == []
