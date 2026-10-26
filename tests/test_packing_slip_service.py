import io
from datetime import datetime
from types import SimpleNamespace

from pypdf import PdfReader

from packing_slip_service import generate_bulk_packing_slip_pdf, generate_packing_slip_pdf


def make_order(**overrides):
    defaults = dict(
        external_label="538473-1913946", external_order_id="538473-1913946",
        created_at=datetime(2026, 8, 17),
        shipping_name="Adrian Lovato", shipping_line1="7554 Belmar Ave",
        shipping_line2=None, shipping_city="Reseda", shipping_state="CA",
        shipping_postal_code="91335-2428", shipping_country="US",
        shipping_cents=135,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_item(**overrides):
    defaults = dict(
        name="Llanowar Elves", set_code="SLD", collector_number="7167",
        # Real OrderItem.finish values are Mana Pool's two-letter finish_id
        # codes (confirmed in production: NF/FO/EF), not full words.
        condition_id="NM", finish="FO", language_id="EN",
        price_cents=376, quantity=1, color="G",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_produces_a_valid_single_page_pdf():
    pdf_bytes = generate_packing_slip_pdf(make_order(), [make_item()])
    assert pdf_bytes.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1


def test_content_matches_the_reference_order():
    pdf_bytes = generate_packing_slip_pdf(make_order(), [make_item()])
    text = extract_text(pdf_bytes)
    assert "Order #538473-1913946" in text
    assert "Order Date: 8/17/2026" in text
    assert "Adrian Lovato" in text
    assert "7554 Belmar Ave" in text
    assert "Reseda, CA 91335-2428" in text
    assert "Llanowar Elves" in text
    assert "SLD" in text
    assert "$3.76" in text
    assert "Shipping" in text
    assert "$1.35" in text
    assert "Total" in text
    assert "$5.11" in text


def test_color_shown_as_plain_text_next_to_name():
    pdf_bytes = generate_packing_slip_pdf(make_order(), [make_item(color="WU")])
    text = extract_text(pdf_bytes)
    assert "Llanowar Elves (WU)" in text


def test_colorless_omits_the_suffix():
    pdf_bytes = generate_packing_slip_pdf(make_order(), [make_item(color="")])
    text = extract_text(pdf_bytes)
    assert "Llanowar Elves (" not in text


def test_extended_price_multiplies_unit_price_by_quantity():
    item = make_item(price_cents=200, quantity=3)
    pdf_bytes = generate_packing_slip_pdf(make_order(), [item])
    text = extract_text(pdf_bytes)
    assert "$6.00" in text  # 200 cents * 3


def test_total_sums_multiple_items_plus_shipping():
    items = [
        make_item(name="Card A", price_cents=100, quantity=1),
        make_item(name="Card B", price_cents=250, quantity=2),
    ]
    pdf_bytes = generate_packing_slip_pdf(make_order(shipping_cents=99), items)
    text = extract_text(pdf_bytes)
    # subtotal = 100 + 500 = 600 cents; total = 600 + 99 = 699 cents
    assert "$6.00" in text
    assert "$0.99" in text
    assert "$6.99" in text


def test_omits_shipping_row_when_shipping_cost_unknown():
    pdf_bytes = generate_packing_slip_pdf(make_order(shipping_cents=None), [make_item()])
    text = extract_text(pdf_bytes)
    assert "Shipping" not in text
    assert "Total" in text
    assert "$3.76" in text  # subtotal only, no shipping added


def test_no_shipping_line2_does_not_leave_a_blank_line():
    pdf_bytes = generate_packing_slip_pdf(make_order(shipping_line2=None), [make_item()])
    text = extract_text(pdf_bytes)
    assert "7554 Belmar Ave\nReseda" in text


def test_shipping_line2_included_when_present():
    pdf_bytes = generate_packing_slip_pdf(make_order(shipping_line2="Apt 4"), [make_item()])
    text = extract_text(pdf_bytes)
    assert "Apt 4" in text


def test_many_items_overflow_to_a_second_page():
    items = [make_item(name=f"Card {i}", price_cents=100, quantity=1) for i in range(60)]
    pdf_bytes = generate_packing_slip_pdf(make_order(), items)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2
    text = extract_text(pdf_bytes)
    assert "(continued)" in text
    assert "Card 0" in text
    assert "Card 59" in text
    assert "$60.00" in text  # 60 * $1.00 subtotal


def test_non_us_country_is_shown_on_address():
    pdf_bytes = generate_packing_slip_pdf(
        make_order(shipping_state="ON", shipping_country="Canada"), [make_item()],
    )
    text = extract_text(pdf_bytes)
    assert "Canada" in text


def test_us_country_is_not_shown_on_address():
    pdf_bytes = generate_packing_slip_pdf(make_order(shipping_country="US"), [make_item()])
    text = extract_text(pdf_bytes)
    assert "\nUS\n" not in text


def test_finish_codes_render_as_readable_words_not_raw_codes():
    items = [
        make_item(name="Non-Foil Card", finish="NF"),
        make_item(name="Foil Card", finish="FO"),
        make_item(name="Etched Card", finish="EF"),
    ]
    pdf_bytes = generate_packing_slip_pdf(make_order(), items)
    text = extract_text(pdf_bytes)
    assert "Non-Foil" in text
    assert "Foil" in text
    assert "Etched" in text
    # None of the raw two-letter codes should appear as standalone tokens.
    assert "\nNF\n" not in text
    assert "\nFO\n" not in text
    assert "\nEF\n" not in text


def test_unrecognized_finish_code_falls_back_to_capitalized_raw_value():
    pdf_bytes = generate_packing_slip_pdf(make_order(), [make_item(finish="surge")])
    text = extract_text(pdf_bytes)
    assert "Surge" in text


def test_bulk_pdf_contains_one_page_per_order_in_order():
    order_a = make_order(external_label="AAA-111", shipping_name="Alice")
    order_b = make_order(external_label="BBB-222", shipping_name="Bob")
    pdf_bytes = generate_bulk_packing_slip_pdf([
        (order_a, [make_item(name="Card A")]),
        (order_b, [make_item(name="Card B")]),
    ])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 2
    assert "AAA-111" in reader.pages[0].extract_text()
    assert "Alice" in reader.pages[0].extract_text()
    assert "Card A" in reader.pages[0].extract_text()
    assert "BBB-222" in reader.pages[1].extract_text()
    assert "Bob" in reader.pages[1].extract_text()
    assert "Card B" in reader.pages[1].extract_text()


def test_bulk_pdf_skips_orders_with_no_items_without_blank_page():
    order_a = make_order(external_label="AAA-111")
    order_empty = make_order(external_label="EMPTY-000")
    order_b = make_order(external_label="BBB-222")
    pdf_bytes = generate_bulk_packing_slip_pdf([
        (order_a, [make_item()]),
        (order_empty, []),
        (order_b, [make_item()]),
    ])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 2
    text = extract_text(pdf_bytes)
    assert "AAA-111" in text
    assert "BBB-222" in text
    assert "EMPTY-000" not in text


def test_bulk_pdf_of_one_order_matches_single_order_output():
    order = make_order()
    items = [make_item()]
    single = generate_packing_slip_pdf(order, items)
    bulk = generate_bulk_packing_slip_pdf([(order, items)])
    assert extract_text(single) == extract_text(bulk)


# --- long card names wrap instead of overrunning the Set column ----------
#
# v2.68.0. Card was 2.3in and 217 of 12,754 production order lines did not
# fit; on 137 of them the colour marker was drawn and then painted over by
# the Set column's set code, so it was present and invisible. These tests
# assert the DRAWN GEOMETRY -- every string's x, y, page and font as the
# canvas received it -- because the bug was never visible in the extracted
# text: both strings were in the PDF, just on top of each other.

from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas as _RealCanvas

import packing_slip_service as pss

HEADER_LABELS = {label for label, _, _ in pss.COLUMNS}
COLUMN_X = {}
_x = pss.LEFT_MARGIN
for _label, _width, _align in pss.COLUMNS:
    COLUMN_X[_label] = _x
    _x += _width

# Longer than any real card name: production's widest is 240.9pt against a
# 248.4pt cell, so nothing in the shop wraps today and a synthetic name is
# the only way to exercise the wrapper. That is the point of it -- it is
# the guarantee for the next set, not a fix for current data.
LONG_DFC = (
    "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel of the Endless Nightmare"
)
LONG_SINGLE_FACE = (
    "Some Extremely Long Hypothetical Card Name That Just Keeps Going And Going"
)
UNBREAKABLE = "A" * 120


class RecordingCanvas(_RealCanvas):
    """A real canvas that also remembers every string it was asked to draw."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._current_font = (None, None)

    def setFont(self, psfontname, size, *args, **kwargs):
        self._current_font = (psfontname, size)
        return super().setFont(psfontname, size, *args, **kwargs)

    def _record(self, x, y, text):
        self.records.append({
            "page": self.getPageNumber(), "x": x, "y": y,
            "text": text, "font": self._current_font,
        })

    def drawString(self, x, y, text, *args, **kwargs):
        self._record(x, y, text)
        return super().drawString(x, y, text, *args, **kwargs)

    def drawRightString(self, x, y, text, *args, **kwargs):
        self._record(x, y, text)
        return super().drawRightString(x, y, text, *args, **kwargs)


def install_recorder(monkeypatch):
    """Make the slip generators draw onto a RecordingCanvas."""
    made = []

    class Shim:
        @staticmethod
        def Canvas(*args, **kwargs):
            c = RecordingCanvas(*args, **kwargs)
            made.append(c)
            return c

    monkeypatch.setattr(pss, "canvas", Shim)
    return made


def card_cells(records):
    """Only the Card column's item text: at the Card x, bold, not the header."""
    return [
        r for r in records
        if r["x"] == COLUMN_X["Card"]
        and r["font"] == (pss.CARD_FONT, pss.ITEM_FONT_SIZE)
        and r["text"] != "Card"
    ]


def rows_from(records):
    """Group the drawn strings into item rows.

    Each row begins with its Qty value: the only plain-font string drawn at
    the left margin that is all digits (the "Qty" header is bold).
    """
    rows, current = [], None
    for r in records:
        is_qty = (
            r["x"] == pss.LEFT_MARGIN
            and r["text"].isdigit()
            and r["font"] == ("Helvetica", pss.ITEM_FONT_SIZE)
        )
        if is_qty:
            current = {"page": r["page"], "y": r["y"], "cards": [], "others": [r]}
            rows.append(current)
        elif (
            current is not None
            and r["text"] not in HEADER_LABELS
            # The summary block is drawn at size 9, and the continued-page
            # banner is an "Order #..." string sitting at the Qty x on a
            # fresh page. Neither is a cell of any item row.
            and r["font"][1] == pss.ITEM_FONT_SIZE
            and not r["text"].startswith("Order #")
        ):
            if r["x"] == COLUMN_X["Card"] and r["font"] == (pss.CARD_FONT, pss.ITEM_FONT_SIZE):
                current["cards"].append(r)
            else:
                current["others"].append(r)
    return rows


def test_column_widths_still_sum_to_the_usable_width():
    """The table runs off the page the moment this stops being true, and a
    rebalance is exactly when it would be broken."""
    total = sum(width for _, width, _ in pss.COLUMNS)
    assert abs(total - pss.USABLE_WIDTH) < 1e-9


def test_every_header_label_still_fits_its_column():
    """Cond and Lang were narrowed to feed Card, and their HEADER text --
    not their values -- is what sets the floor: 'Cond' is 20.4pt."""
    for label, width, _ in pss.COLUMNS:
        needed = stringWidth(label, "Helvetica-Bold", pss.ITEM_FONT_SIZE)
        assert needed <= width, f"{label!r} header needs {needed}pt, column is {width}pt"


def test_no_card_text_is_ever_drawn_past_its_column(monkeypatch):
    made = install_recorder(monkeypatch)
    items = [
        make_item(name=LONG_DFC, color="B"),
        make_item(name=LONG_SINGLE_FACE, color="W"),
        make_item(name=UNBREAKABLE, color=""),
        make_item(name="Llanowar Elves", color="G"),
    ]
    pss.generate_packing_slip_pdf(make_order(), items)

    cells = card_cells(made[0].records)
    assert cells, "no card text was recorded"
    limit = COLUMN_X["Card"] + pss.CARD_WIDTH
    for r in cells:
        end = r["x"] + stringWidth(r["text"], *r["font"])
        assert end <= limit + 1e-6, f"{r['text']!r} ends at {end}, cell ends at {limit}"


def test_a_long_double_faced_name_wraps_at_the_face_boundary(monkeypatch):
    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(make_order(), [make_item(name=LONG_DFC, color="B")])

    lines = [r["text"] for r in card_cells(made[0].records)]
    assert len(lines) == 2, lines
    assert lines[0] == "Sephiroth, Fabled SOLDIER //"
    assert lines[1].startswith("Sephiroth, One-Winged Angel")


def test_the_marker_stays_on_the_last_line_and_is_never_orphaned(monkeypatch):
    made = install_recorder(monkeypatch)
    items = [
        make_item(name=LONG_DFC, color="B"),
        make_item(name=LONG_SINGLE_FACE, color="WU"),
        make_item(name=UNBREAKABLE, color="", type_line="Artifact"),
    ]
    pss.generate_packing_slip_pdf(make_order(), items)

    rows = rows_from(made[0].records)
    assert len(rows) == 3
    for row, marker in zip(rows, ("(B)", "(WU)", "(C)")):
        lines = [r["text"] for r in row["cards"]]
        assert lines[-1].endswith(marker), lines
        # Never alone on its own line: the marker travels with real text.
        assert lines[-1].strip() != marker, lines


def test_other_cells_align_to_the_first_line_of_a_wrapped_row(monkeypatch):
    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(make_order(), [make_item(name=LONG_DFC, color="B")])

    row = rows_from(made[0].records)[0]
    assert len(row["cards"]) == 2, "expected this row to wrap"
    first_line_y = row["cards"][0]["y"]
    for r in row["others"]:
        assert r["y"] == first_line_y, f"{r['text']!r} drifted off the first line"


def test_an_unbreakable_name_is_broken_rather_than_truncated(monkeypatch):
    """Nothing is cut (operator decision 2026-10-26), so a token with no
    break point is split by character and every letter still prints."""
    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(
        make_order(), [make_item(name=UNBREAKABLE, color="", type_line="Artifact")],
    )

    lines = [r["text"] for r in card_cells(made[0].records)]
    assert len(lines) > 1
    assert lines[-1].endswith("(C)"), lines
    assert "".join(lines).replace(" (C)", "") == UNBREAKABLE


def test_a_wrapped_row_is_never_split_across_a_page(monkeypatch):
    """Swept across filler counts so the wrapping row lands at every
    offset near the page bottom -- including straddling it, which is the
    failure wrapping could have introduced."""
    for filler in range(20, 45):
        made = install_recorder(monkeypatch)
        items = [make_item(name="Llanowar Elves") for _ in range(filler)]
        items.append(make_item(name=LONG_DFC, color="B"))
        pss.generate_packing_slip_pdf(make_order(), items)

        for row in rows_from(made[0].records):
            pages = {r["page"] for r in row["cards"]}
            assert len(pages) == 1, f"filler={filler}: a row spanned pages {pages}"
            lowest = min(r["y"] for r in row["cards"])
            assert lowest >= pss.BOTTOM_MARGIN, (
                f"filler={filler}: a card line sat at y={lowest}, "
                f"below the {pss.BOTTOM_MARGIN}pt bottom margin"
            )


def test_every_row_is_drawn_exactly_once_across_a_multi_page_slip(monkeypatch):
    made = install_recorder(monkeypatch)
    items = [make_item(name=f"Card Number {n}") for n in range(70)]
    items[35] = make_item(name=LONG_DFC, color="B")
    pss.generate_packing_slip_pdf(make_order(), items)

    rows = rows_from(made[0].records)
    assert len(rows) == 70
    assert len({r["page"] for r in rows}) > 1, "expected this slip to run onto page 2"


def test_bulk_print_all_wraps_exactly_like_a_single_slip(monkeypatch):
    """The two paths share _draw_item_row, and this is what proves they
    cannot drift: the same order wraps identically through both."""
    items = [make_item(name=LONG_DFC, color="B"), make_item(name="Llanowar Elves")]

    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(make_order(), items)
    single = [r["text"] for r in card_cells(made[0].records)]

    made = install_recorder(monkeypatch)
    pss.generate_bulk_packing_slip_pdf([(make_order(), items)])
    bulk = [r["text"] for r in card_cells(made[0].records)]

    assert single == bulk


def test_real_production_long_names_need_no_wrapping_after_the_rebalance():
    """Measured against production 2026-10-26: the widest Card string in
    all 12,754 order lines is 240.9pt, against a 248.4pt cell. These are
    the four longest real names; if a future rebalance narrows Card they
    start wrapping, which is survivable but should be a deliberate choice.
    """
    widest = [
        ("Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel", "B"),
        ("Balamb Garden, SeeD Academy // Balamb Garden, Airborne", "L"),
        ("Cyclonus, the Saboteur // Cyclonus, Cybertronian Fighter", "UB"),
        ("Reidane, God of the Worthy // Valkmira, Protector's Shield", "W"),
    ]
    for name, marker in widest:
        lines = pss.card_cell_lines(name, f" ({marker})", pss.CARD_WIDTH)
        assert len(lines) == 1, f"{name!r} now wraps: {lines}"
