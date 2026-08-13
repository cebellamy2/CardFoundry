from pricing_diagnostic_service import (
    build_exact_request,
    map_optimizer_listings,
    run_two_card_proof,
)


def item(
    inventory_id,
    product_id,
    name,
    set_code,
    number,
    condition,
    finish,
    price,
    mtgjson_id=None,
):
    return {
        "id": inventory_id,
        "product_id": product_id,
        "price_cents": price,
        "product": {
            "single": {
                "name": name,
                "set": set_code,
                "number": number,
                "language_id": "EN",
                "condition_id": condition,
                "finish_id": finish,
                "mtgjson_id": mtgjson_id or f"mtgjson-{product_id}",
            }
        },
    }


def test_two_card_mapping_uses_listing_metadata_not_response_order():
    urza = item("ours-urza", "product-urza", "Urza's Ruinous Blast", "PDOM", "39s", "LP", "FO", 498)
    angelic = item("ours-angelic", "product-angelic", "Angelic Destiny", "PLST", "M12-3", "LP", "NF", 56)
    competitor_urza = item(
        "competitor-urza",
        "product-urza-nm",
        "Urza's Ruinous Blast",
        "PDOM",
        "39s",
        "NM",
        "FO",
        500,
        mtgjson_id="mtgjson-product-urza",
    )
    competitor_angelic = item("competitor-angelic", "product-angelic", "Angelic Destiny", "PLST", "M12-3", "LP", "NF", 80)
    calls = {"optimizer": 0, "listings": 0}

    def optimize(cart, seller_id):
        calls["optimizer"] += 1
        assert seller_id == "seller-id"
        assert cart[0]["condition_ids"] == ["NM", "LP"]
        return {
            "cart": [
                {"inventory_id": "competitor-angelic", "quantity_selected": 1},
                {"inventory_id": "competitor-urza", "quantity_selected": 1},
            ],
            "totals": {"subtotal_cents": 580},
        }

    def resolve(ids):
        calls["listings"] += 1
        assert ids == ["competitor-angelic", "competitor-urza"]
        return [competitor_urza, competitor_angelic]

    proof = run_two_card_proof([urza, angelic], "seller-id", optimize, resolve)

    assert calls == {"optimizer": 1, "listings": 1}
    assert proof["passed"] is True
    assert proof["results"][0]["inventory_id"] == "competitor-urza"
    assert proof["results"][0]["price_cents"] == 500
    assert proof["results"][0]["target_cents"] == 495
    assert proof["results"][1]["target_cents"] == 75


def test_worse_condition_is_held():
    requested = item("ours", "product", "Card", "SET", "1", "LP", "NF", 100)
    other = item("ours-2", "product-2", "Other", "TWO", "2", "NM", "NF", 100)
    requests = [build_exact_request(requested), build_exact_request(other)]
    bad = item("bad", "product", "Card", "SET", "1", "MP", "NF", 200)
    good = item("good", "product-2", "Other", "TWO", "2", "NM", "NF", 200)
    optimized = {"cart": [
        {"inventory_id": "bad", "quantity_selected": 1},
        {"inventory_id": "good", "quantity_selected": 1},
    ]}

    proof = map_optimizer_listings(requests, optimized, [bad, good])

    assert proof["passed"] is False
    assert proof["results"][0]["verdict"] == "HOLD"
    assert "worse" in proof["results"][0]["reason"]


def test_ambiguous_or_missing_mapping_holds_without_using_subtotal():
    urza = item("ours-urza", "product-urza", "Urza's Ruinous Blast", "PDOM", "39s", "LP", "FO", 498)
    other = item("ours-2", "product-2", "Other", "TWO", "2", "NM", "NF", 100)
    requests = [build_exact_request(urza), build_exact_request(other)]
    one = item("one", "product-urza", "Urza's Ruinous Blast", "PDOM", "39s", "NM", "FO", 500)
    two = item("two", "product-urza", "Urza's Ruinous Blast", "PDOM", "39s", "LP", "FO", 510)
    optimized = {
        "cart": [
            {"inventory_id": "one", "quantity_selected": 1},
            {"inventory_id": "two", "quantity_selected": 1},
        ],
        "totals": {"subtotal_cents": 1},
    }

    proof = map_optimizer_listings(requests, optimized, [one, two])

    assert proof["passed"] is False
    assert proof["results"][0]["reason"] == "Multiple resolved listings matched the request."
    assert proof["results"][0]["price_cents"] is None
    assert proof["results"][1]["verdict"] == "HOLD"


def test_exact_language_and_finish_are_required():
    requested = item("ours", "product", "Card", "SET", "1", "LP", "FO", 100)
    other = item("ours-2", "product-2", "Other", "TWO", "2", "NM", "NF", 100)
    requests = [build_exact_request(requested), build_exact_request(other)]
    wrong_finish = item("wrong", "product", "Card", "SET", "1", "NM", "NF", 200)
    good = item("good", "product-2", "Other", "TWO", "2", "NM", "NF", 200)
    optimized = {"cart": [
        {"inventory_id": "wrong", "quantity_selected": 1},
        {"inventory_id": "good", "quantity_selected": 1},
    ]}

    proof = map_optimizer_listings(requests, optimized, [wrong_finish, good])

    assert proof["passed"] is False
    assert proof["results"][0]["reason"] == "No exact resolved listing matched the request."
