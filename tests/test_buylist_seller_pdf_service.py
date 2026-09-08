import io
from datetime import datetime
from types import SimpleNamespace

from pypdf import PdfReader

from buylist_seller_pdf_service import eligible_seller_pdf_lines, generate_buylist_seller_pdf

DEFAULT_TIERS = [
    {"max_price": 1.00, "type": "flat", "value": 0.10},
    {"max_price": 2.99, "type": "percent", "value": 0.60},
    {"max_price": 4.99, "type": "percent", "value": 0.65},
    {"max_price": 35.00, "type": "percent", "value": 0.80},
    {"max_price": None, "type": "percent", "value": 0.80, "deduction": 5.50},
]


def make_pile(**overrides):
    defaults = dict(
        code="PILE-2026-09-07", is_owned=False,
        created_at=datetime(2026, 9, 7, 16, 0, 0), finalized_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_line(**overrides):
    defaults = dict(
        name="Lightning Bolt", set_code="LEA", collector_number="161",
        condition="Near Mint", finish="nonfoil", language=None,
        line_status="pending", price_cents=1000, offer_cents=650,
        operator_override_cents=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_produces_a_valid_pdf():
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [make_line()], DEFAULT_TIERS)
    assert pdf_bytes.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 1


def test_chriss_cards_branding_not_cardfoundry():
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [make_line()], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Chris's Cards" in text
    assert "CardFoundry" not in text


def test_shows_card_printing_condition_and_amount():
    line = make_line(name="Sol Ring", set_code="LEA", collector_number="247", condition="Light Play")
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Sol Ring" in text
    assert "LEA" in text
    assert "247" in text
    assert "Light Play" in text
    assert "$6.50" in text


def test_no_tier_math_or_percentages_anywhere():
    line = make_line(price_cents=1000, offer_cents=650)
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "%" not in text
    assert "tier" not in text.lower()
    assert "60" not in text  # the tier rate, not present anywhere


def test_foil_and_non_english_are_flagged_inline():
    foil_line = make_line(name="Sol Ring", finish="foil")
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [foil_line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Sol Ring (Foil)" in text

    jp_line = make_line(name="Paradox Engine", finish="nonfoil", language="Japanese")
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [jp_line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Paradox Engine (Japanese)" in text


def test_normal_finish_and_english_omit_the_parenthetical():
    line = make_line(name="Lightning Bolt", finish="nonfoil", language="EN")
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Lightning Bolt (" not in text


def test_zero_dollar_lines_are_omitted_entirely():
    zero_line = make_line(name="Worthless Card", price_cents=50, offer_cents=0)
    priced_line = make_line(name="Sol Ring", price_cents=1000, offer_cents=650)
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [zero_line, priced_line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Worthless Card" not in text
    assert "Sol Ring" in text


def test_unpriced_lines_are_omitted_entirely():
    unpriced = make_line(name="No LP Plus Card", price_cents=None, offer_cents=None)
    priced = make_line(name="Sol Ring", price_cents=1000, offer_cents=650)
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [unpriced, priced], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "No LP Plus Card" not in text
    assert "Sol Ring" in text


def test_kept_by_seller_lines_are_omitted_entirely():
    kept = make_line(name="Keeper Card", line_status="kept_by_seller", offer_cents=500)
    bought = make_line(name="Sol Ring", line_status="pending", offer_cents=650)
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [kept, bought], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Keeper Card" not in text
    assert "Sol Ring" in text


def test_operator_override_takes_precedence_over_computed_offer():
    line = make_line(name="Sol Ring", offer_cents=650, operator_override_cents=999)
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "$9.99" in text
    assert "$6.50" not in text


def test_consignment_line_shown_as_estimate_and_totaled_separately():
    buy_line = make_line(name="Sol Ring", line_status="pending", price_cents=1000, offer_cents=650)
    # $10.00 at 80% (over $5, under $35 tier) = $8.00 estimate.
    consignment_line = make_line(
        name="Paradox Engine", line_status="consignment", price_cents=1000, offer_cents=700,
    )
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [buy_line, consignment_line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "$8.00 (est.)" in text
    assert "Buy Total" in text
    assert "$6.50" in text
    assert "Estimated Consignment Payout" in text
    assert "$8.00" in text
    assert "estimate" in text.lower()


def test_buy_total_shown_alone_when_no_consignment_lines():
    line = make_line(name="Sol Ring", line_status="pending", offer_cents=650)
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), [line], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Buy Total" in text
    assert "Estimated Consignment Payout" not in text


def test_post_finalize_committed_buy_and_committed_consignment_are_recognized():
    """After CF-BUY-004's finalize, line_status becomes committed_buy or
    committed_consignment (not the generic "consignment"/"pending"
    anymore) -- the PDF must still route these into the right total."""
    bought = make_line(name="Sol Ring", line_status="committed_buy", price_cents=1000, offer_cents=650)
    consigned = make_line(
        name="Paradox Engine", line_status="committed_consignment", price_cents=1000, offer_cents=700,
    )
    kept = make_line(name="Keeper Card", line_status="kept_by_seller", offer_cents=500)
    pile = make_pile(finalized_at=datetime(2026, 9, 8, 9, 0, 0))
    pdf_bytes = generate_buylist_seller_pdf(pile, [bought, consigned, kept], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "Sol Ring" in text
    assert "$6.50" in text
    assert "Paradox Engine" in text
    assert "$8.00 (est.)" in text
    assert "Keeper Card" not in text
    assert "Estimated Consignment Payout" in text


def test_eligible_seller_pdf_lines_filters_and_labels_correctly():
    kept = make_line(name="Keeper", line_status="kept_by_seller")
    zero = make_line(name="Zero", offer_cents=0)
    unpriced = make_line(name="Unpriced", offer_cents=None)
    buy = make_line(name="Buy", line_status="pending", offer_cents=650)
    consignment = make_line(name="Consign", line_status="consignment", price_cents=1000, offer_cents=700)
    rows = eligible_seller_pdf_lines([kept, zero, unpriced, buy, consignment], DEFAULT_TIERS)
    names = [row[0].name for row in rows]
    assert names == ["Buy", "Consign"]
    buy_row = next(row for row in rows if row[0].name == "Buy")
    consign_row = next(row for row in rows if row[0].name == "Consign")
    assert buy_row[1] == 650
    assert buy_row[2] is False
    assert consign_row[1] == 800  # 80% of $10.00
    assert consign_row[2] is True


def test_pile_date_prefers_finalized_at_over_created_at():
    pile = make_pile(created_at=datetime(2026, 9, 1), finalized_at=datetime(2026, 9, 8))
    pdf_bytes = generate_buylist_seller_pdf(pile, [make_line()], DEFAULT_TIERS)
    text = extract_text(pdf_bytes)
    assert "9/8/2026" in text
    assert "9/1/2026" not in text


def test_many_lines_overflow_to_a_second_page():
    lines = [make_line(name=f"Card {i}", offer_cents=100 + i) for i in range(60)]
    pdf_bytes = generate_buylist_seller_pdf(make_pile(), lines, DEFAULT_TIERS)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2
    text = extract_text(pdf_bytes)
    assert "Card 0" in text
    assert "Card 59" in text
