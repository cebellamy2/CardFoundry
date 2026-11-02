import io
from datetime import datetime
from types import SimpleNamespace

import pytest
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


def rgb(color) -> tuple:
    """A reportlab colour as a plain (r, g, b) tuple.

    `_fillColorObj` is a bare tuple at its default and a Color instance
    once set, so the two have to be compared through something.
    """
    if hasattr(color, "red"):
        return (color.red, color.green, color.blue)
    return tuple(color)[:3]


BLACK = (0, 0, 0)


class RecordingCanvas(_RealCanvas):
    """A real canvas that also remembers every string and rect it was asked to draw."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.rects = []
        self._current_font = (None, None)

    def setFont(self, psfontname, size, *args, **kwargs):
        self._current_font = (psfontname, size)
        return super().setFont(psfontname, size, *args, **kwargs)

    def _record(self, x, y, text):
        self.records.append({
            "page": self.getPageNumber(), "x": x, "y": y,
            "text": text, "font": self._current_font,
            # v2.69.1: the fill colour IN FORCE as this string is drawn.
            # The grey-page-1 bug lived entirely in this value and was
            # invisible to every font/geometry/extracted-text assertion.
            "fill": rgb(self._fillColorObj),
        })

    def drawString(self, x, y, text, *args, **kwargs):
        self._record(x, y, text)
        return super().drawString(x, y, text, *args, **kwargs)

    def drawRightString(self, x, y, text, *args, **kwargs):
        self._record(x, y, text)
        return super().drawRightString(x, y, text, *args, **kwargs)

    def rect(self, x, y, width, height, *args, **kwargs):
        # The Total row's shading is the lowest ink on the slip -- 3pt under
        # that row's baseline -- and no drawString record can see it. The
        # summary-past-the-margin defect was furthest out of bounds HERE.
        self.rects.append({
            "page": self.getPageNumber(), "x": x, "y": y,
            "w": width, "h": height,
        })
        return super().rect(x, y, width, height, *args, **kwargs)


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
    # The slip sorts by colour group (v2.69.0), so the drawn order is
    # multicolour, then mono B, then colourless -- not the input order.
    for row, marker in zip(rows, ("(WU)", "(B)", "(C)")):
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
        # ★ color="G", NOT "B". The slip sorts by colour group (v2.69.0),
        # and a (B) row would be pulled to the FRONT of the slip -- which
        # would leave this sweep silently never testing a wrapped row at a
        # page boundary again. Same group as the filler, and "Sephiroth..."
        # sorts after "Llanowar Elves", so it stays last where the sweep
        # needs it.
        items.append(make_item(name=LONG_DFC, color="G"))
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


# --- lines are grouped by colour, then A-Z by name ----------------------
#
# v2.69.0, operator decision 2026-10-26: multicolour, then W U B R G, then
# colourless, then lands (any colour), then anything whose colour we have
# not resolved. A-Z by card name inside each group, with set code and
# collector number as tie-breaks.
#
# These assert the DRAWN ROW ORDER off the recording canvas rather than the
# order of the list handed in, because the list handed in is exactly what
# the sort is allowed to ignore. Both generators are driven, because the
# operator prints from both.


def drawn_names(records):
    """One entry per drawn row: that row's Card cell, lines joined.

    Reading the rows back off the canvas is the only honest way to assert
    the printed order -- `sort_slip_items` could be right and the renderer
    could still iterate something else.
    """
    return [
        " ".join(r["text"] for r in row["cards"])
        for row in rows_from(records)
        if row["cards"]
    ]


def names_drawn_by(generator, items, monkeypatch):
    made = install_recorder(monkeypatch)
    if generator is pss.generate_bulk_packing_slip_pdf:
        generator([(make_order(), items)])
    else:
        generator(make_order(), items)
    return drawn_names(made[0].records)


# One line per group, deliberately named so that a plain A-Z sort of the
# whole slip would produce a DIFFERENT order from the grouped one: the
# multicolour card is a Z and the white card an A, so if grouping ever
# stopped happening, "Angel of Invention" would come out first.
ONE_PER_GROUP = [
    ("Zur the Enchanter", "WUB", "Legendary Creature — Human Wizard"),
    ("Angel of Invention", "W", "Creature — Angel"),
    ("Brainstorm", "U", "Instant"),
    ("Dark Ritual", "B", "Instant"),
    ("Lightning Bolt", "R", "Instant"),
    ("Giant Growth", "G", "Instant"),
    ("Sol Ring", "", "Artifact"),
    ("Dryad Arbor", "G", "Land Creature — Forest Dryad"),
    ("Mystery Printing", None, None),
]

EXPECTED_GROUP_ORDER = [
    "Zur the Enchanter (WUB)",
    "Angel of Invention (W)",
    "Brainstorm (U)",
    "Dark Ritual (B)",
    "Lightning Bolt (R)",
    "Giant Growth (G)",
    "Sol Ring (C)",
    "Dryad Arbor (L)",
    "Mystery Printing",
]


def group_items(scrambled=False):
    items = [
        make_item(name=name, color=color, type_line=type_line)
        for name, color, type_line in ONE_PER_GROUP
    ]
    if scrambled:
        # Reversed AND rotated: no rotation of the correct answer, and not
        # the input order either, so passing means the sort did the work.
        items = list(reversed(items))
        items = items[4:] + items[:4]
    return items


@pytest.mark.parametrize("scrambled", [False, True])
def test_rows_print_in_the_operators_colour_group_order(monkeypatch, scrambled):
    names = names_drawn_by(
        pss.generate_packing_slip_pdf, group_items(scrambled), monkeypatch,
    )
    assert names == EXPECTED_GROUP_ORDER


def test_print_all_prints_the_same_order_as_the_single_slip(monkeypatch):
    """The two paths share _draw_one_slip and the sort happens inside it;
    this is what proves neither route can print an unsorted slip."""
    items = group_items(scrambled=True)
    single = names_drawn_by(pss.generate_packing_slip_pdf, items, monkeypatch)
    bulk = names_drawn_by(pss.generate_bulk_packing_slip_pdf, items, monkeypatch)
    assert single == bulk == EXPECTED_GROUP_ORDER


def test_print_all_sorts_every_order_in_the_wave_independently(monkeypatch):
    """Each slip is sorted on its own; one order's lines never migrate into
    another's, and the orders stay in the wave's own sequence."""
    made = install_recorder(monkeypatch)
    pss.generate_bulk_packing_slip_pdf([
        (make_order(external_label="AAA-111"), [
            make_item(name="Sol Ring", color="", type_line="Artifact"),
            make_item(name="Brainstorm", color="U", type_line="Instant"),
        ]),
        (make_order(external_label="BBB-222"), [
            make_item(name="Forest", color="", type_line="Basic Land — Forest"),
            make_item(name="Dark Ritual", color="B", type_line="Instant"),
        ]),
    ])
    assert drawn_names(made[0].records) == [
        "Brainstorm (U)", "Sol Ring (C)",
        "Dark Ritual (B)", "Forest (L)",
    ]


def test_within_a_group_names_run_A_to_Z(monkeypatch):
    items = [
        make_item(name="Shock", color="R", type_line="Instant"),
        make_item(name="Ball Lightning", color="R", type_line="Creature — Elemental"),
        make_item(name="Lightning Bolt", color="R", type_line="Instant"),
        make_item(name="abyssal persecutor", color="R", type_line="Creature — Demon"),
    ]
    names = names_drawn_by(pss.generate_packing_slip_pdf, items, monkeypatch)
    assert names == [
        "abyssal persecutor (R)",  # A-Z as a human reads it: case-insensitive.
        "Ball Lightning (R)",
        "Lightning Bolt (R)",
        "Shock (R)",
    ]


def test_a_coloured_land_still_prints_after_a_colourless_non_land(monkeypatch):
    """★ Dryad Arbor is a GREEN land. It must print in the land group at
    the end -- after the colourless artifacts -- not with the green cards,
    which is the same rule that makes its marker (L) and not (G)."""
    items = [
        make_item(name="Dryad Arbor", color="G", type_line="Land Creature — Forest Dryad"),
        make_item(name="Sol Ring", color="", type_line="Artifact"),
        make_item(name="Giant Growth", color="G", type_line="Instant"),
    ]
    names = names_drawn_by(pss.generate_packing_slip_pdf, items, monkeypatch)
    assert names == ["Giant Growth (G)", "Sol Ring (C)", "Dryad Arbor (L)"]


def test_a_double_faced_card_is_grouped_by_its_front_face(monkeypatch):
    """★ Scryfall JOINS the faces, so "Sorcery // Land" contains "Land".
    A substring test would file both of these under lands; the front face
    decides, so they stay in their colour groups and only the real
    land-front card lands in the land group."""
    items = [
        make_item(
            name="Turntimber Symbiosis // Turntimber, Serpentine Wood",
            color="G", type_line="Sorcery // Land",
        ),
        make_item(
            name="Agadeem's Awakening // Agadeem, the Undercrypt",
            color="B", type_line="Sorcery // Land",
        ),
        make_item(
            name="Hidden Grove // Grove Dryad",
            color="G", type_line="Land // Creature — Dryad",
        ),
    ]
    names = names_drawn_by(pss.generate_packing_slip_pdf, items, monkeypatch)
    assert names == [
        "Agadeem's Awakening // Agadeem, the Undercrypt (B)",
        "Turntimber Symbiosis // Turntimber, Serpentine Wood (G)",
        "Hidden Grove // Grove Dryad (L)",
    ]


def test_a_line_with_no_known_colour_prints_last_and_is_never_dropped(monkeypatch):
    """A row whose colour was never looked up gets no marker, so it cannot
    be filed under a colour. It goes at the very end -- behind the lands --
    rather than being guessed at or quietly left off the slip."""
    items = [
        make_item(name="Aaaa Unknown", color=None, type_line=None),
        make_item(name="Zzzz Unknown", color="", type_line=None),
        make_item(name="Forest", color="", type_line="Basic Land — Forest"),
        make_item(name="Zur the Enchanter", color="WUB", type_line="Creature — Human"),
    ]
    names = names_drawn_by(pss.generate_packing_slip_pdf, items, monkeypatch)
    assert names == [
        "Zur the Enchanter (WUB)",
        "Forest (L)",
        "Aaaa Unknown",
        "Zzzz Unknown",
    ]


def test_an_unrecognised_marker_sorts_into_its_own_bucket_before_unknown(monkeypatch):
    """`color` only ever holds WUBRG letters today, so this cannot happen
    -- which is exactly why it is pinned. An unclassifiable line must not
    fall silently into a real colour group, and must not be confused with
    "never looked up" either, so it gets a bucket of its own at the end
    where the operator can see it."""
    items = [
        make_item(name="Never Looked Up", color=None, type_line=None),
        make_item(name="Nonsense Colour", color="Q", type_line="Instant"),
        make_item(name="Forest", color="", type_line="Basic Land — Forest"),
    ]
    names = names_drawn_by(pss.generate_packing_slip_pdf, items, monkeypatch)
    assert names == ["Forest (L)", "Nonsense Colour (Q)", "Never Looked Up"]


def test_two_printings_of_one_card_break_the_tie_on_set_then_collector(monkeypatch):
    """Same name, so the order has to come from somewhere stable. Set code
    first, then collector number by NATURAL sort -- 2 before 10, which a
    plain string sort gets backwards."""
    items = [
        make_item(name="Llanowar Elves", set_code="M19", collector_number="10"),
        make_item(name="Llanowar Elves", set_code="DOM", collector_number="10"),
        make_item(name="Llanowar Elves", set_code="M19", collector_number="2"),
        make_item(name="Llanowar Elves", set_code="DOM", collector_number="2"),
    ]
    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(make_order(), items)
    rows = rows_from(made[0].records)
    printed = [
        (
            next(r["text"] for r in row["others"] if r["text"] in {"DOM", "M19"}),
            next(r["text"] for r in row["others"] if r["text"] in {"2", "10"}),
        )
        for row in rows
    ]
    assert printed == [("DOM", "2"), ("DOM", "10"), ("M19", "2"), ("M19", "10")]


def test_sorting_does_not_reorder_the_callers_list():
    """The routes hand their fetched list straight to the renderer; the
    renderer sorting it in place would be a side effect the caller never
    asked for."""
    items = group_items(scrambled=True)
    before = [item.name for item in items]
    pss.generate_packing_slip_pdf(make_order(), items)
    assert [item.name for item in items] == before


def test_identical_lines_keep_the_order_they_arrived_in(monkeypatch):
    """Nothing distinguishes these, so the sort must be stable: both
    routes fetch by OrderItem.id, which is the order Mana Pool sent."""
    items = [make_item(name="Llanowar Elves", price_cents=cents) for cents in (100, 200, 300)]
    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(make_order(), items)
    prices = [
        next(r["text"] for r in row["others"] if r["text"].startswith("$"))
        for row in rows_from(made[0].records)
    ]
    assert prices == ["$1.00", "$2.00", "$3.00"]


def test_the_totals_do_not_change_when_the_order_of_lines_changes():
    """Only the ORDER of rows changes (operator decision). The subtotal,
    the item count and the Total are the same numbers as before."""
    items = group_items()
    straight = extract_text(pss.generate_packing_slip_pdf(make_order(), items))
    scrambled = extract_text(pss.generate_packing_slip_pdf(make_order(), group_items(True)))
    for fragment in ("9 items", "$33.84", "$1.35", "$35.19"):
        assert fragment in straight, fragment
        assert fragment in scrambled, fragment


# --- every slip line prints SOLID BLACK, on every page ------------------
#
# v2.69.1. `_draw_fold_line` set its label's grey fill AFTER its own
# restoreState and never put it back, so everything drawn afterwards
# inherited it: the entire line-items table, and on a one-page slip the
# subtotal/shipping/Total as well, printed at 50% grey. `showPage()`
# resets the graphics state, so page 2 onwards printed solid black -- the
# same slip came out in two different shades, which the operator reported
# as "page 2 looks bold".
#
# ★ THESE ASSERT THE FILL COLOUR THE PRINTER ACTUALLY RECEIVES. Extracted
# text cannot see a colour, fonts were identical on both pages, and the
# geometry was correct, so every existing test passed throughout. The
# first test below reads the emitted PDF content stream itself; the rest
# read the colour in force at each draw.

import re as _re

FOLD_GREY = (0.5, 0.5, 0.5)
SUMMARY_SHADE = (0.93, 0.93, 0.93)

_CONTENT_OPS = _re.compile(
    r"(?P<q>\bq\b)|(?P<Q>\bQ\b)"
    r"|(?P<rgb>[\d.]+ [\d.]+ [\d.]+) rg"
    r"|(?P<gray>[\d.]+) g\b"
    r"|\((?P<text>(?:[^()\\]|\\.)*)\) Tj"
)


def text_fills_per_page(pdf_bytes: bytes) -> list[list[tuple]]:
    """For each page, the (text, fill) of every string in the content
    stream, with q/Q handled so inherited state is resolved exactly the
    way a PDF reader resolves it.

    This reads the emitted file rather than the canvas, because the whole
    point of the bug was state that leaked at the PDF level.
    """
    pages = []
    for page in PdfReader(io.BytesIO(pdf_bytes)).pages:
        stream = page.get_contents().get_data().decode("latin-1")
        fill, stack, runs = BLACK, [], []
        for m in _CONTENT_OPS.finditer(stream):
            if m.group("q"):
                stack.append(fill)
            elif m.group("Q"):
                if stack:
                    fill = stack.pop()
            elif m.group("rgb"):
                fill = tuple(float(v) for v in m.group("rgb").split())
            elif m.group("gray"):
                value = float(m.group("gray"))
                fill = (value, value, value)
            elif m.group("text") is not None:
                runs.append((m.group("text"), fill))
        pages.append(runs)
    return pages


def test_no_slip_text_is_drawn_in_anything_but_black_except_the_fold_label():
    """★ The regression test for the grey page. Reads the PDF's own
    content stream: every string on every page is solid black, and the
    only exception in the whole document is the word "Fold"."""
    items = [make_item(name=f"Card Number {n}") for n in range(60)]
    pdf_bytes = generate_packing_slip_pdf(make_order(), items)

    pages = text_fills_per_page(pdf_bytes)
    assert len(pages) >= 2, "expected this slip to run onto page 2"
    for page_number, runs in enumerate(pages, 1):
        assert runs, f"page {page_number} drew no text"
        for text, fill in runs:
            if text == "Fold":
                assert fill == FOLD_GREY, "the Fold label should stay grey"
                continue
            assert fill == BLACK, (
                f"page {page_number}: {text!r} is drawn at {fill}, not black"
            )


def test_page_one_and_page_two_print_in_the_same_colour():
    """The symptom as the operator saw it: one slip, two shades. Stated
    as its own test because 'both pages are black' is the thing that was
    wrong, and it is worth failing loudly on its own terms."""
    items = [make_item(name=f"Card Number {n}") for n in range(60)]
    pages = text_fills_per_page(generate_packing_slip_pdf(make_order(), items))
    shades = [
        {fill for text, fill in runs if text != "Fold"}
        for runs in pages
    ]
    assert shades[0] == shades[1] == {BLACK}, shades


def test_a_one_page_slip_prints_its_rows_and_totals_in_black(monkeypatch):
    """The common case, and the one where the totals were grey too: on a
    short order the whole summary block sits on page 1, below the fold."""
    made = install_recorder(monkeypatch)
    pss.generate_packing_slip_pdf(make_order(), [make_item(), make_item(name="Sol Ring")])

    records = made[0].records
    assert {r["page"] for r in records} == {1}, "expected a one-page slip"
    for r in records:
        if r["text"] == "Fold":
            continue
        assert r["fill"] == BLACK, f"{r['text']!r} drawn at {r['fill']}"
    # ...and the summary really was in there, not skipped by the loop.
    assert any(r["text"] == "Total" for r in records)
    assert any(r["text"].startswith("$") for r in records)


@pytest.mark.parametrize("generator", ["single", "bulk"])
def test_both_printing_paths_draw_every_row_in_black(monkeypatch, generator):
    """The operator prints from the order page and from the pick wave's
    Print All; both go through _draw_one_slip, and both are checked."""
    items = [make_item(name=f"Card Number {n}") for n in range(60)]
    made = install_recorder(monkeypatch)
    if generator == "single":
        pss.generate_packing_slip_pdf(make_order(), items)
    else:
        pss.generate_bulk_packing_slip_pdf([(make_order(), items)])

    records = made[0].records
    assert len({r["page"] for r in records}) > 1, "expected more than one page"
    offenders = [
        (r["page"], r["text"], r["fill"])
        for r in records
        if r["text"] != "Fold" and r["fill"] != BLACK
    ]
    assert not offenders, offenders


def test_print_all_does_not_leak_one_orders_colour_into_the_next(monkeypatch):
    """Each order re-draws the fold line, so a leak would stain every
    following order's first page -- not just its own."""
    items = [make_item(), make_item(name="Sol Ring")]
    made = install_recorder(monkeypatch)
    pss.generate_bulk_packing_slip_pdf([
        (make_order(external_label="AAA-111"), items),
        (make_order(external_label="BBB-222"), items),
        (make_order(external_label="CCC-333"), items),
    ])
    for r in made[0].records:
        if r["text"] == "Fold":
            continue
        assert r["fill"] == BLACK, f"page {r['page']}: {r['text']!r} at {r['fill']}"


# ★ THE GENERAL RULE, not just this one bug: a draw helper may set
# whatever colour, dash or font it likes, and must leave none of it behind.
# Every helper that touches graphics state is listed here, so the next one
# added without a saveState fails this instead of quietly tinting a page.

def _state(c):
    return (rgb(c._fillColorObj), rgb(c._strokeColorObj))


def test_no_draw_helper_leaves_a_colour_set_behind():
    c = _RealCanvas(io.BytesIO(), pagesize=pss.LETTER)
    order = make_order()
    before = _state(c)
    assert before == (BLACK, BLACK), "canvas should start black"

    helpers = [
        ("_draw_fold_line", lambda: pss._draw_fold_line(c)),
        ("_draw_header", lambda: pss._draw_header(c, order)),
        ("_draw_address", lambda: pss._draw_address(c, order)),
        ("_draw_table_header", lambda: pss._draw_table_header(c, 500)),
        ("_draw_item_row", lambda: pss._draw_item_row(c, 500, make_item())),
        ("_draw_summary_rows", lambda: pss._draw_summary_rows(c, 400, order, 500, 2)),
        ("_draw_page_marker", lambda: pss._draw_page_marker(c, 1, 2)),
    ]
    for name, call in helpers:
        call()
        assert _state(c) == before, f"{name} left the canvas at {_state(c)}"


def test_the_fold_label_is_still_grey():
    """The fix must not turn the label black -- it is deliberately quiet,
    and it is the one thing on the slip that should not be solid."""
    pages = text_fills_per_page(generate_packing_slip_pdf(make_order(), [make_item()]))
    labels = [fill for text, fill in pages[0] if text == "Fold"]
    assert labels == [FOLD_GREY]


# --- The summary block never prints below the bottom margin -----------------
#
# The item loop reserves BOTTOM_MARGIN + 40pt per row, but the summary that
# follows it had no page-break check at all. With a Shipping row the block's
# lowest ink -- the Total shading, 3pt under the Total baseline -- sits 41pt
# under the last row's y, so a slip whose last row just cleared the reserve
# printed its totals into the margin. Extracted text cannot see this (the
# totals are in the PDF either way), so these tests read drawn geometry.
#
# The counts are exact on purpose: the row pitch is 14pt against a 40pt
# reserve, so only narrow, specific counts fail. "Wrapped" rows are LONG_DFC
# in the filler's colour group, which sorts after "Llanowar Elves" and so
# stays last, where it decides the summary's starting y.

def slip_shape(plain, wrapped=0, shipping_cents=135):
    items = [make_item() for _ in range(plain)]
    items += [make_item(name=LONG_DFC) for _ in range(wrapped)]
    return make_order(shipping_cents=shipping_cents), items


def summary_ink(canvas_):
    """The summary block's strings and shading: (strings, rects).

    Every string drawn after the last item row's Qty cell at size 9 -- the
    row's own cells and the continued banner are size 8. The Total shading
    is the only rect the slip draws.
    """
    last_qty = rows_from(canvas_.records)[-1]["others"][0]
    start = next(i for i, r in enumerate(canvas_.records) if r is last_qty)
    strings = [r for r in canvas_.records[start:] if r["font"][1] == 9]
    return strings, canvas_.rects


def lowest_summary_ink(canvas_):
    strings, rects = summary_ink(canvas_)
    return min([r["y"] for r in strings] + [r["y"] for r in rects])


def render(monkeypatch, order, items, *, summary_guard=True):
    """Render one slip onto a RecordingCanvas: (canvas, page count).

    ``summary_guard=False`` forces the new summary check to say "fits",
    which is exactly the pre-fix renderer -- that branch is the only
    behaviour the fix added. Each test proves this reference really is the
    old output by matching the lowest-ink figure measured at 619d082.
    """
    if not summary_guard:
        monkeypatch.setattr(pss, "_summary_fits", lambda y, order: True)
    made = install_recorder(monkeypatch)
    pdf = pss.generate_packing_slip_pdf(order, items)
    monkeypatch.undo()
    return made[0], len(PdfReader(io.BytesIO(pdf)).pages)


def drawn(canvas_):
    return [(r["page"], r["x"], r["y"], r["text"], r["font"]) for r in canvas_.records]


# (plain items, wrapped last items, lowest ink measured before the fix)
PROVEN_OUT_OF_BOUNDS = [
    (30, 0, 27.776),
    (76, 0, 35.400),
    (77, 0, 21.400),
    (28, 1, 32.776),
    (27, 2, 23.776),
    (73, 2, 31.400),
    (75, 1, 26.400),
]

# (plain, wrapped, shipping_cents, page count, lowest ink)
MUST_NOT_CHANGE = [
    (1, 0, 135, 1, 433.776),
    (5, 0, 135, 1, 377.776),
    (28, 0, 135, 1, 55.776),
    (29, 0, 135, 1, 41.776),
    (30, 0, None, 1, 43.776),
    (31, 0, 135, 2, 665.400),
    (75, 0, 135, 2, 49.400),
    (78, 0, 135, 3, 665.400),
]


@pytest.mark.parametrize("plain,wrapped,before", PROVEN_OUT_OF_BOUNDS)
def test_the_summary_never_prints_below_the_bottom_margin(monkeypatch, plain, wrapped, before):
    old, old_pages = render(monkeypatch, *slip_shape(plain, wrapped), summary_guard=False)
    assert lowest_summary_ink(old) == pytest.approx(before, abs=1e-6), "reference is not the pre-fix slip"

    new, new_pages = render(monkeypatch, *slip_shape(plain, wrapped))
    ink = lowest_summary_ink(new)
    assert ink >= pss.BOTTOM_MARGIN, f"summary ink at y={ink}, below the {pss.BOTTOM_MARGIN}pt margin"

    # Moved WHOLE: subtotal, Shipping and Total strings plus the shading,
    # all on one page -- the page after the last item row.
    strings, rects = summary_ink(new)
    assert [r["text"] for r in strings[::2]] == [f"{plain + wrapped} items", "Shipping", "Total"]
    assert len(rects) == 1
    last_item_page = rows_from(new.records)[-1]["page"]
    assert {r["page"] for r in strings + rects} == {last_item_page + 1}
    assert new_pages == old_pages + 1

    # Item rows have not moved: same page, same y, as before the fix.
    rows = lambda c: [(r["page"], r["y"]) for r in rows_from(c.records)]
    assert rows(new) == rows(old)
    assert len(rows(new)) == plain + wrapped

    # Page X of Y: everything but the marker is the pre-change slip, and
    # the moved summary's page is counted in Y.
    before, before_pages = render_before([slip_shape(plain, wrapped)])
    assert without_markers(new) == all_ink(before)
    assert rect_ink(new) == rect_ink(before)
    assert new_pages == before_pages
    assert [r["text"] for r in new.records if is_page_marker(r)] == \
        [f"Page {p} of {new_pages}" for p in range(1, new_pages + 1)]


@pytest.mark.parametrize("plain,wrapped,shipping,pages,ink", MUST_NOT_CHANGE)
def test_a_slip_that_already_fits_is_drawn_exactly_as_before(monkeypatch, plain, wrapped, shipping, pages, ink):
    old, old_pages = render(monkeypatch, *slip_shape(plain, wrapped, shipping), summary_guard=False)
    new, new_pages = render(monkeypatch, *slip_shape(plain, wrapped, shipping))

    assert drawn(new) == drawn(old)
    assert [(r["page"], r["x"], r["y"], r["w"], r["h"]) for r in new.rects] == \
        [(r["page"], r["x"], r["y"], r["w"], r["h"]) for r in old.rects]
    assert new_pages == old_pages == pages
    assert lowest_summary_ink(new) == pytest.approx(ink, abs=1e-6)

    # Page X of Y: against the frozen pre-change renderer, a one-page slip
    # is identical in every op; a longer one differs only by its markers.
    before, before_pages = render_before([slip_shape(plain, wrapped, shipping)])
    if pages == 1:
        assert all_ink(new) == all_ink(before)
        assert not [r for r in new.records if is_page_marker(r)]
    else:
        assert without_markers(new) == all_ink(before)
    assert rect_ink(new) == rect_ink(before)
    assert before_pages == pages


def test_a_moved_summary_gets_the_continued_banner_and_no_table_header(monkeypatch):
    moved, _ = render(monkeypatch, *slip_shape(30))
    overflow, _ = render(monkeypatch, *slip_shape(31))

    # The "Page 2 of 2" marker is on this page too; it has its own tests
    # below, and is left out here so the banner/summary order still reads.
    page2 = [r for r in drawn(moved) if r[0] == 2 and not PAGE_MARKER.match(r[3])]
    banner = [r for r in drawn(overflow) if r[0] == 2][0]
    assert banner[3] == "Order #538473-1913946 (continued)"
    # The same banner, at the same place in the same font, as the
    # item-overflow path draws -- and it is the first thing on the page.
    assert page2[0] == banner
    assert not [r for r in page2 if r[3] in HEADER_LABELS], "a table header was drawn over no rows"
    assert [r[3] for r in page2[1::2]] == ["30 items", "Shipping", "Total"]


def test_print_all_starts_the_next_order_on_a_fresh_page_after_a_moved_summary(monkeypatch):
    made = install_recorder(monkeypatch)
    first = slip_shape(30)
    second = (make_order(external_label="B-2", external_order_id="B-2"), [make_item()])
    pdf = pss.generate_bulk_packing_slip_pdf([first, second])

    records = made[0].records
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 3
    by_page = {p: [r["text"] for r in records if r["page"] == p] for p in (1, 2, 3)}
    assert all(by_page.values()), "a blank page was produced"
    assert "Total" in by_page[2] and "Packing Slip" not in by_page[2]
    assert "Packing Slip" in by_page[3] and "Order #B-2" in by_page[3]
    assert "Order #B-2" not in by_page[2]


# --- "Page X of Y" on slips that run past one page ---------------------------
#
# Operator, 2026-10-28: a slip that runs onto more than one page shows a
# small "Page X of Y" on every page, counted per ORDER, not per PDF. The
# order number was already on every page; the count is what was missing.
#
# ★ THE "BEFORE" HERE IS A FROZEN COPY OF THE PRE-CHANGE RENDERER, not the
# new code with the marker switched off. Adding the marker meant moving the
# page-break decisions out of the drawing loop into a layout pass, so a
# reference built from the new code would only prove the new code agrees
# with itself. The copy below is _draw_one_slip and _start_continued_page
# exactly as they stood at 9fff7e1, calling the same unchanged row, header
# and summary helpers; it is test-only and is never imported by the app.


def _frozen_start_continued_page(c, order):
    c.showPage()
    c.setFont("Helvetica", 8)
    label = order.external_label or order.external_order_id
    c.drawString(pss.LEFT_MARGIN, pss.PAGE_H - 0.5 * pss.inch, f"Order #{label} (continued)")
    return pss.PAGE_H - 0.8 * pss.inch


def _frozen_draw_one_slip(c, order, items):
    items = pss.sort_slip_items(items)
    pss._draw_header(c, order)
    pss._draw_address(c, order)
    pss._draw_fold_line(c)
    y = pss._y_from_top(pss.FOLD_FROM_TOP + pss.TABLE_TOP_PAD)
    pss._draw_table_header(c, y)
    y -= pss.ITEM_ROW_HEIGHT
    subtotal_cents = item_count = unpriced_lines = 0
    for item in items:
        extra = pss.item_row_height(item) - pss.ITEM_ROW_HEIGHT
        if y - extra < pss.BOTTOM_MARGIN + 40:
            y = _frozen_start_continued_page(c, order)
            pss._draw_table_header(c, y)
            y -= pss.ITEM_ROW_HEIGHT
        y -= pss._draw_item_row(c, y, item) - pss.ITEM_ROW_HEIGHT
        line_total = pss._line_total_cents(item)
        if line_total is None:
            unpriced_lines += 1
        else:
            subtotal_cents += line_total
        item_count += item.quantity
        y -= pss.ITEM_ROW_HEIGHT
    y -= 6
    if not pss._summary_fits(y, order):
        y = _frozen_start_continued_page(c, order) - 6
    pss._draw_summary_rows(c, y, order, subtotal_cents, item_count, unpriced_lines)
    c.showPage()


def render_before(orders_with_items):
    """The pre-change slip(s) on a RecordingCanvas: (canvas, page count)."""
    c = RecordingCanvas(io.BytesIO(), pagesize=pss.LETTER)
    for order, items in orders_with_items:
        if items:
            _frozen_draw_one_slip(c, order, items)
    return c, c.getPageNumber() - 1


def render_now(orders_with_items):
    """The same slip(s) through the real bulk generator: (canvas, page count)."""
    with pytest.MonkeyPatch.context() as mp:
        made = install_recorder(mp)
        pdf = pss.generate_bulk_packing_slip_pdf(orders_with_items)
    return made[0], len(PdfReader(io.BytesIO(pdf)).pages)


PAGE_MARKER = _re.compile(r"^Page \d+ of \d+$")


def is_page_marker(record) -> bool:
    return bool(PAGE_MARKER.match(record["text"]))


def without_markers(canvas_):
    return [
        (r["page"], r["x"], r["y"], r["text"], r["font"], r["fill"])
        for r in canvas_.records if not is_page_marker(r)
    ]


def all_ink(canvas_):
    return [
        (r["page"], r["x"], r["y"], r["text"], r["font"], r["fill"])
        for r in canvas_.records
    ]


def rect_ink(canvas_):
    return [(r["page"], r["x"], r["y"], r["w"], r["h"]) for r in canvas_.rects]


# Every order shape the summary tests above pin, plus a sweep through and
# past both page boundaries, with and without a Shipping row -- which is
# what moves the v2.80.1 summary break by one row.
SWEEP_SHAPES = sorted(
    {(plain, wrapped, 135) for plain, wrapped, _ in PROVEN_OUT_OF_BOUNDS}
    | {(plain, wrapped, shipping) for plain, wrapped, shipping, _, _ in MUST_NOT_CHANGE}
    | {(plain, 0, shipping) for plain in range(1, 82) for shipping in (135, None)}
    | {(plain, wrapped, shipping)
       for plain in (26, 27, 28, 29, 72, 73, 74, 75)
       for wrapped in (1, 2)
       for shipping in (135, None)},
    key=lambda shape: (shape[0], shape[1], shape[2] is None),
)


@pytest.mark.parametrize("plain,wrapped,shipping", SWEEP_SHAPES)
def test_the_frozen_reference_matches_the_live_renderer_minus_the_marker(plain, wrapped, shipping):
    """Every string (position, font, fill), every rect and the page count
    are what the pre-change renderer drew; the only additions anywhere are
    "Page X of Y" strings."""
    shape = slip_shape(plain, wrapped, shipping)
    before, before_pages = render_before([shape])
    now, now_pages = render_now([shape])

    assert without_markers(now) == all_ink(before)
    assert rect_ink(now) == rect_ink(before)
    assert now_pages == before_pages

    markers = [(r["page"], r["text"]) for r in now.records if is_page_marker(r)]
    if now_pages == 1:
        assert markers == []
    else:
        assert markers == [(p, f"Page {p} of {now_pages}") for p in range(1, now_pages + 1)]


from reportlab.pdfbase.pdfmetrics import getAscent, getDescent


def marker_records(canvas_):
    return [r for r in canvas_.records if is_page_marker(r)]


def text_box(record):
    """A drawn string's ink box: (left, bottom, right, top) in points.

    The recorder keeps x but not alignment, so the horizontal extent is
    taken both ways round -- wide enough to cover a right-aligned string
    as well as a left-aligned one. Vertical extent is the font's real
    ascent/descent, which is what reaches the paper.
    """
    font, size = record["font"]
    width = stringWidth(record["text"], font, size)
    return (
        record["x"] - width,
        record["y"] + getDescent(font, size),
        record["x"] + width,
        record["y"] + getAscent(font, size),
    )


MULTI_PAGE_SHAPES = [
    (31, 0, 135),    # items overflow onto page 2
    (30, 0, 135),    # only the summary is moved to page 2 (v2.80.1)
    (77, 0, 135),    # items fill page 2, summary moved to page 3
    (78, 0, 135),    # items run onto page 3
    (75, 1, None),   # a wrapped last row, no Shipping row
]


@pytest.mark.parametrize("generator", ["single", "bulk"])
@pytest.mark.parametrize("plain,wrapped,shipping", MULTI_PAGE_SHAPES)
def test_every_page_of_a_long_slip_reads_page_x_of_y_small_and_black(
    monkeypatch, generator, plain, wrapped, shipping,
):
    """Both printing paths: the order page and the pick wave's Print All."""
    order, items = slip_shape(plain, wrapped, shipping)
    made = install_recorder(monkeypatch)
    if generator == "single":
        pdf = pss.generate_packing_slip_pdf(order, items)
    else:
        pdf = pss.generate_bulk_packing_slip_pdf([(order, items)])
    pages = len(PdfReader(io.BytesIO(pdf)).pages)
    assert pages >= 2

    markers = marker_records(made[0])
    assert [(r["page"], r["text"]) for r in markers] == \
        [(p, f"Page {p} of {pages}") for p in range(1, pages + 1)]
    for r in markers:
        assert r["font"][0] == "Helvetica" and r["font"][1] in (7, 8), r["font"]
        assert r["fill"] == BLACK, f"{r['text']!r} drawn at {r['fill']}"

    # ...and in the PDF the buyer gets: extracted text and the colour the
    # content stream really sets, not just what the canvas was told.
    reader = PdfReader(io.BytesIO(pdf))
    for p, page in enumerate(reader.pages, 1):
        assert f"Page {p} of {pages}" in page.extract_text()
    for p, runs in enumerate(text_fills_per_page(pdf), 1):
        assert (f"Page {p} of {pages}", BLACK) in runs


@pytest.mark.parametrize("plain,wrapped,shipping", MULTI_PAGE_SHAPES)
def test_the_page_marker_sits_inside_the_printable_area_clear_of_all_other_ink(
    monkeypatch, plain, wrapped, shipping,
):
    """At least 18pt from every edge, and sharing no vertical band with
    any other string (header, banner, rows, summary, Fold label), the
    Total shading, or the fold line -- so it cannot overlap any of them
    whatever their horizontal extent."""
    canvas_, pages = render(monkeypatch, *slip_shape(plain, wrapped, shipping))
    markers = marker_records(canvas_)
    assert len(markers) == pages

    fold_y = pss._y_from_top(pss.FOLD_FROM_TOP)
    for marker in markers:
        _, bottom, _, top = text_box(marker)
        # The marker is right-aligned, so its true box is [x - width, x].
        left = marker["x"] - stringWidth(marker["text"], *marker["font"])
        right = marker["x"]
        assert left >= 18 and bottom >= 18, (left, bottom)
        assert right <= pss.PAGE_W - 18 and top <= pss.PAGE_H - 18, (right, top)
        # Below the bottom margin, where the rows and summary never go.
        assert top < pss.BOTTOM_MARGIN

        page = marker["page"]
        if page == 1:
            assert not bottom <= fold_y <= top
        for other in canvas_.records:
            if other["page"] != page or other is marker:
                continue
            _, o_bottom, _, o_top = text_box(other)
            assert o_top < bottom or o_bottom > top, (
                f"page {page}: marker overlaps {other['text']!r} at y={other['y']}"
            )
        for rect in canvas_.rects:
            if rect["page"] != page:
                continue
            assert rect["y"] + rect["h"] < bottom or rect["y"] > top, (
                f"page {page}: marker overlaps the Total shading"
            )


def test_a_one_page_slip_gets_no_marker_in_the_pdf_either():
    pdf = pss.generate_packing_slip_pdf(*slip_shape(29))
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 1
    assert "Page 1 of" not in extract_text(pdf)


def test_print_all_counts_pages_per_order_not_per_pdf(monkeypatch):
    """A (1 page), B (3 pages), C (2 pages): B and C each count from 1,
    and A -- a one-page slip -- has no marker at all."""
    order_a, items_a = slip_shape(1)
    order_b, items_b = slip_shape(78)
    order_c, items_c = slip_shape(31)
    order_a.external_label = order_a.external_order_id = "A-1"
    order_b.external_label = order_b.external_order_id = "B-2"
    order_c.external_label = order_c.external_order_id = "C-3"

    made = install_recorder(monkeypatch)
    pdf = pss.generate_bulk_packing_slip_pdf([
        (order_a, items_a), (order_b, items_b), (order_c, items_c),
    ])
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 6

    expected = [None, "Page 1 of 3", "Page 2 of 3", "Page 3 of 3", "Page 1 of 2", "Page 2 of 2"]
    owners = ["A-1", "B-2", "B-2", "B-2", "C-3", "C-3"]
    records = made[0].records
    for p, (marker, owner) in enumerate(zip(expected, owners), 1):
        on_page = [r["text"] for r in records if r["page"] == p and is_page_marker(r)]
        assert on_page == ([marker] if marker else []), (p, on_page)
        text = reader.pages[p - 1].extract_text()
        # The order number is on every page, so the count is never orphaned
        # from the order it counts.
        assert f"Order #{owner}" in text
        if marker:
            assert marker in text
        else:
            assert "Page " not in text

    # Each order still starts on a fresh page with its own header.
    headers = [r["page"] for r in records if r["text"] == "Packing Slip"]
    assert headers == [1, 2, 5]
