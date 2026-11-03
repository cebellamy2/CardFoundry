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
        # UNTRACKED by default. Until the quarter panels this was tracked,
        # to keep the "Multistamp needed" tag off older tests' slips; but a
        # tracked order now prints as quarter panels, so a tracked default
        # would quietly move every full-size test below onto the panel
        # renderer. The full-size tests that compare against the frozen
        # pre-tag renderer filter the tag out (without_tag_or_markers), and
        # the panel tests at the end pass ground_advantage explicitly.
        shipping_method="first_class",
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


_DEFAULT_GSTATE = {"dx": 0, "dy": 0, "dash": None, "line_width": 1}


class RecordingCanvas(_RealCanvas):
    """A real canvas that also remembers every string, rect and line it
    was asked to draw.

    ★ COORDINATES ARE RECORDED ON THE SHEET, not as passed. Quarter panels
    are drawn in their own coordinates after a translate to the panel's
    corner, so the raw arguments say nothing about which quarter a string
    is in. The recorder keeps the translate (and the dash and line width,
    for the cut lines) on its own saveState/restoreState stack, reset by
    showPage just as the real graphics state is. A full-size slip never
    translates, so its records are exactly what they always were.
    """

    def __init__(self, *args, **kwargs):
        # Set BEFORE the real __init__, in case it touches state itself.
        self._gstate = dict(_DEFAULT_GSTATE)
        self._gstack = []
        super().__init__(*args, **kwargs)
        self.records = []
        self.rects = []
        self.lines = []
        self._current_font = (None, None)

    def saveState(self, *args, **kwargs):
        self._gstack.append(dict(self._gstate))
        return super().saveState(*args, **kwargs)

    def restoreState(self, *args, **kwargs):
        if self._gstack:
            self._gstate = self._gstack.pop()
        return super().restoreState(*args, **kwargs)

    def showPage(self, *args, **kwargs):
        result = super().showPage(*args, **kwargs)
        self._gstate = dict(_DEFAULT_GSTATE)
        self._gstack = []
        return result

    def translate(self, dx, dy):
        self._gstate["dx"] += dx
        self._gstate["dy"] += dy
        return super().translate(dx, dy)

    def setDash(self, *args, **kwargs):
        self._gstate["dash"] = args or None
        return super().setDash(*args, **kwargs)

    def setLineWidth(self, width):
        self._gstate["line_width"] = width
        return super().setLineWidth(width)

    def setFont(self, psfontname, size, *args, **kwargs):
        self._current_font = (psfontname, size)
        return super().setFont(psfontname, size, *args, **kwargs)

    def _record(self, x, y, text, align):
        self.records.append({
            "page": self.getPageNumber(),
            "x": x + self._gstate["dx"], "y": y + self._gstate["dy"],
            "text": text, "font": self._current_font,
            # v2.69.1: the fill colour IN FORCE as this string is drawn.
            # The grey-page-1 bug lived entirely in this value and was
            # invisible to every font/geometry/extracted-text assertion.
            "fill": rgb(self._fillColorObj),
            "align": align,
        })

    def drawString(self, x, y, text, *args, **kwargs):
        self._record(x, y, text, "left")
        return super().drawString(x, y, text, *args, **kwargs)

    def drawRightString(self, x, y, text, *args, **kwargs):
        self._record(x, y, text, "right")
        return super().drawRightString(x, y, text, *args, **kwargs)

    def rect(self, x, y, width, height, *args, **kwargs):
        # The Total row's shading is the lowest ink on the slip -- 3pt under
        # that row's baseline -- and no drawString record can see it. The
        # summary-past-the-margin defect was furthest out of bounds HERE.
        self.rects.append({
            "page": self.getPageNumber(),
            "x": x + self._gstate["dx"], "y": y + self._gstate["dy"],
            "w": width, "h": height, "fill": rgb(self._fillColorObj),
        })
        return super().rect(x, y, width, height, *args, **kwargs)

    def line(self, x1, y1, x2, y2):
        dx, dy = self._gstate["dx"], self._gstate["dy"]
        self.lines.append({
            "page": self.getPageNumber(),
            "x1": x1 + dx, "y1": y1 + dy, "x2": x2 + dx, "y2": y2 + dy,
            "stroke": rgb(self._strokeColorObj),
            "dash": self._gstate["dash"], "width": self._gstate["line_width"],
        })
        return super().line(x1, y1, x2, y2)


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
        ("_draw_header multistamp", lambda: pss._draw_header(c, order, multistamp=True)),
        ("_draw_address", lambda: pss._draw_address(c, order)),
        ("_draw_table_header", lambda: pss._draw_table_header(c, 500)),
        ("_draw_item_row", lambda: pss._draw_item_row(c, 500, make_item())),
        ("_draw_summary_rows", lambda: pss._draw_summary_rows(c, 400, order, 500, 2)),
        ("_draw_page_marker", lambda: pss._draw_page_marker(c, 1, 2)),
        ("_draw_cut_lines", lambda: pss._draw_cut_lines(c)),
        ("_draw_panel_header", lambda: pss._draw_panel_header(c, order, 2, 3)),
        ("_draw_panel_table_header", lambda: pss._draw_panel_table_header(c)),
        ("_draw_panel_row", lambda: pss._draw_panel_row(c, 200, make_item())),
        ("_draw_panel_summary", lambda: pss._draw_panel_summary(c, 100, order, 500, 2)),
        ("_draw_quarter_slip", lambda: pss._draw_quarter_slip(
            pss.QuarterSheets(c), make_order(shipping_method="ground_advantage"),
            [make_item()],
        )),
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

    # Page X of Y: everything but the marker (and the multistamp tag, which
    # these untracked 30-card slips carry) is the pre-change slip, and the
    # moved summary's page is counted in Y.
    before, before_pages = render_before([slip_shape(plain, wrapped)])
    assert without_tag_or_markers(new) == all_ink(before)
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
    # is identical in every op but the multistamp tag; a longer one differs
    # only by its markers and the tag.
    before, before_pages = render_before([slip_shape(plain, wrapped, shipping)])
    assert without_tag_or_markers(new) == all_ink(before)
    if pages == 1:
        assert not [r for r in new.records if is_page_marker(r)]
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
    "Page X of Y" strings and, at six cards or more, the multistamp tag
    (make_order is untracked, first_class)."""
    shape = slip_shape(plain, wrapped, shipping)
    before, before_pages = render_before([shape])
    now, now_pages = render_now([shape])

    assert without_tag_or_markers(now) == all_ink(before)
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
    """B (3 pages), C (2 pages), A (1 page): B and C each count from 1,
    and A -- a one-page slip -- has no marker at all.

    Handed in A, B, C; all three are untracked, B and C are multistamp
    (group 1) and A is not, and inside the group the most cards go first,
    so they print B, C, A."""
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

    expected = ["Page 1 of 3", "Page 2 of 3", "Page 3 of 3", "Page 1 of 2", "Page 2 of 2", None]
    owners = ["B-2", "B-2", "B-2", "C-3", "C-3", "A-1"]
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
    assert headers == [1, 4, 6]


# --- "Multistamp needed" on untracked 6+ card orders, special ones first -----
#
# Operator, 2026-10-28 and 2026-10-30: when packing, the operator needs to
# see at a glance which envelopes need extra postage, and to pack the
# special orders first. An UNTRACKED order of 6 or more cards (total
# quantity, not lines) gets a "Multistamp needed" tag under the Order Date
# on its first page; a tracked (ground_advantage) order never does. Print
# All prints tracked orders, then multistamp orders, then the rest, most
# cards first inside each group.
#
# Like the sections above, these read the DRAWN ink off the recording
# canvas -- position, font and fill -- and compare against the frozen
# pre-change renderer, because "the text is in the PDF" says nothing about
# whether it overlaps the header or moved anything else.


def is_tag(record) -> bool:
    return record["text"] == pss.MULTISTAMP_TEXT


def without_tag_or_markers(canvas_):
    return [
        (r["page"], r["x"], r["y"], r["text"], r["font"], r["fill"])
        for r in canvas_.records if not is_page_marker(r) and not is_tag(r)
    ]


def untracked(order, items, method=None):
    order.shipping_method = method
    return order, items


def test_the_tracking_constant_cannot_drift_from_main():
    import main
    assert pss.TRACKED_SHIPPING_METHOD == main.TRACKING_REQUIRED_SHIPPING_METHOD


# (shipping_method, item quantities, tag expected)
TAG_BOUNDARIES = [
    ("first_class", [1] * 5, False),        # 5 cards: one stamp
    ("first_class", [5], False),            # 5 cards on one line
    ("first_class", [1] * 6, True),         # 6 cards: the threshold
    ("first_class", [2, 3, 1], True),       # 6 cards over 3 lines
    (None, [1] * 6, True),                  # no method recorded = untracked
    (None, [3, 2], False),                  # 5 cards, 2 lines
    ("ground_advantage", [1] * 20, False),  # tracked: on a label, never tagged
    ("ground_advantage", [6], False),
]


@pytest.mark.parametrize("method,quantities,tagged", TAG_BOUNDARIES)
def test_the_tag_appears_exactly_on_untracked_orders_of_six_cards_or_more(
    monkeypatch, method, quantities, tagged,
):
    items = [make_item(name=f"Card {n}", quantity=q) for n, q in enumerate(quantities)]
    order = make_order(shipping_method=method)
    made = install_recorder(monkeypatch)
    pdf = pss.generate_packing_slip_pdf(order, items)

    tags = [r for r in made[0].records if is_tag(r)]
    assert len(tags) == (1 if tagged else 0)
    assert (pss.MULTISTAMP_TEXT in extract_text(pdf)) == tagged
    # The count the rule uses is the count the buyer sees in the summary.
    total = sum(quantities)
    assert f"{total} items" in extract_text(pdf)
    assert pss.total_quantity(items) == total


@pytest.mark.parametrize("method,quantities", [
    ("first_class", [2, 3, 1]),
    (None, [1] * 6),
    (None, [1] * 40),   # runs onto page 2: the tag stays on page 1 only
])
def test_a_tagged_slip_differs_from_its_untagged_twin_only_by_the_tag(monkeypatch, method, quantities):
    """Same order, same items, tag rule on vs off: every other string, rect
    and page is identical -- rows, breaks, summary, markers, line order.

    The twin used to be the same order shipped tracked; a tracked order now
    prints as quarter panels, so the twin is the same untracked order with
    the tag rule switched off instead."""
    items = [make_item(name=f"Card {n}", quantity=q) for n, q in enumerate(quantities)]
    with pytest.MonkeyPatch.context() as mp:
        made = install_recorder(mp)
        mp.setattr(pss, "needs_multistamp", lambda order, items: False)
        tracked_pdf = pss.generate_packing_slip_pdf(make_order(shipping_method=method), items)
    tracked = made[0]
    assert not [r for r in tracked.records if is_tag(r)]
    with pytest.MonkeyPatch.context() as mp:
        made = install_recorder(mp)
        tagged_pdf = pss.generate_packing_slip_pdf(make_order(shipping_method=method), items)
    tagged = made[0]

    assert [r for r in all_ink(tagged) if r[3] != pss.MULTISTAMP_TEXT] == all_ink(tracked)
    assert rect_ink(tagged) == rect_ink(tracked)
    pages = len(PdfReader(io.BytesIO(tagged_pdf)).pages)
    assert pages == len(PdfReader(io.BytesIO(tracked_pdf)).pages)
    assert [r["page"] for r in tagged.records if is_tag(r)] == [1]


@pytest.mark.parametrize("plain,wrapped,shipping", SWEEP_SHAPES)
def test_an_untracked_slip_is_the_frozen_slip_plus_only_the_tag(plain, wrapped, shipping):
    """The same sweep the "Page X of Y" change was pinned with -- every
    v2.80.1 summary boundary, one page through three -- but untracked.
    Under 6 cards it is op-for-op the pre-change slip; at 6 or more the
    ONLY addition beyond the page markers is one tag, on page 1."""
    shape = untracked(*slip_shape(plain, wrapped, shipping))
    before, before_pages = render_before([shape])
    now, now_pages = render_now([shape])

    assert without_tag_or_markers(now) == all_ink(before)
    assert rect_ink(now) == rect_ink(before)
    assert now_pages == before_pages

    tags = [r["page"] for r in now.records if is_tag(r)]
    if plain + wrapped >= pss.MULTISTAMP_MIN_CARDS:
        assert tags == [1]
    else:
        assert tags == []
        assert all_ink(now) == all_ink(before)


@pytest.mark.parametrize("plain", [6, 31, 78])
def test_the_tag_sits_under_the_order_date_inside_the_header_band(monkeypatch, plain):
    """Right-aligned with the header text, under "Order Date:", bold 10pt,
    solid black, and its ink box clear of everything else on page 1: the
    logo, every header string, the address, the Fold label and line, and
    the table header."""
    made = install_recorder(monkeypatch)
    pdf = pss.generate_packing_slip_pdf(*untracked(*slip_shape(plain)))
    records = made[0].records

    [tag] = [r for r in records if is_tag(r)]
    [order_date] = [r for r in records if r["text"].startswith("Order Date:")]
    assert tag["page"] == 1
    assert tag["font"] == ("Helvetica-Bold", 10)
    assert tag["fill"] == BLACK
    assert tag["x"] == order_date["x"], "not right-aligned with the header text"
    assert tag["y"] < order_date["y"]

    width = stringWidth(tag["text"], *tag["font"])
    left, right = tag["x"] - width, tag["x"]
    bottom = tag["y"] + getDescent(*tag["font"])
    top = tag["y"] + getAscent(*tag["font"])

    # Inside the header band, and above the address block and the fold.
    assert pss._y_from_top(pss.HEADER_BOTTOM) < bottom
    assert top < pss._y_from_top(pss.HEADER_TOP)
    assert right <= pss.PAGE_W - pss.RIGHT_MARGIN
    assert bottom > pss._y_from_top(pss.FOLD_FROM_TOP)

    # Right of the logo: the image is not a recorded string, so its box
    # comes from the same arithmetic _draw_header places it with.
    band = pss._y_from_top(pss.HEADER_TOP) - pss._y_from_top(pss.HEADER_BOTTOM)
    logo_size = min(band - 0.1 * pss.inch, 1.7 * pss.inch)
    assert left > pss.LEFT_MARGIN + 0.15 * pss.inch + logo_size

    for other in records:
        if other["page"] != 1 or other is tag:
            continue
        o_left, o_bottom, o_right, o_top = text_box(other)
        separate = o_top < bottom or o_bottom > top or o_right < left or o_left > right
        assert separate, f"the tag overlaps {other['text']!r} at y={other['y']}"

    # ...and in the PDF itself: black in the content stream, page 1 only.
    runs = text_fills_per_page(pdf)
    assert (pss.MULTISTAMP_TEXT, BLACK) in runs[0]
    for later in runs[1:]:
        assert pss.MULTISTAMP_TEXT not in [text for text, _ in later]


def labelled(label, quantity, method):
    order = make_order(external_label=label, external_order_id=label, shipping_method=method)
    return order, [make_item(name=f"Card {n}") for n in range(quantity)]


def header_labels_in_print_order(canvas_):
    """The "Order #..." under "Packing Slip" on each slip's first page --
    the continued banner is a different string and is left out."""
    return [
        r["text"].removeprefix("Order #")
        for r in canvas_.records
        if r["text"].startswith("Order #") and not r["text"].endswith("(continued)")
    ]


def test_print_all_prints_tracked_then_multistamp_then_the_rest_most_cards_first(monkeypatch):
    entries = [
        labelled("U3", 3, "first_class"),
        labelled("T2", 2, "ground_advantage"),
        labelled("U8", 8, "first_class"),
        labelled("U6", 6, None),
        labelled("T9", 9, "ground_advantage"),
        labelled("U1", 1, "first_class"),
    ]
    made = install_recorder(monkeypatch)
    pdf = pss.generate_bulk_packing_slip_pdf(entries)

    expected = ["T9", "T2", "U8", "U6", "U3", "U1"]
    assert header_labels_in_print_order(made[0]) == expected
    reader = PdfReader(io.BytesIO(pdf))
    # The two tracked orders are two quarter panels on ONE sheet; each
    # untracked order is a full-size page after it.
    pages_holding = [["T9", "T2"], ["U8"], ["U6"], ["U3"], ["U1"]]
    assert len(reader.pages) == len(pages_holding)
    for page, labels in zip(reader.pages, pages_holding):
        for label in labels:
            assert f"Order #{label}" in page.extract_text()

    # The group an order printed in and the tag on its slip agree.
    tagged = [
        labels[0] for page, labels in zip(reader.pages, pages_holding)
        if pss.MULTISTAMP_TEXT in page.extract_text()
    ]
    assert tagged == ["U8", "U6"]
    # The caller's list is not reordered in place.
    assert [order.external_label for order, _ in entries] == ["U3", "T2", "U8", "U6", "T9", "U1"]


def test_print_all_keeps_the_callers_order_for_ties_and_still_skips_empty_orders(monkeypatch):
    entries = [
        labelled("U7-first", 7, None),
        labelled("EMPTY-T", 0, "ground_advantage"),
        labelled("T4-first", 4, "ground_advantage"),
        labelled("U5", 5, "first_class"),
        labelled("U7-second", 7, "first_class"),
        labelled("T4-second", 4, "ground_advantage"),
        labelled("EMPTY-U", 0, None),
    ]
    made = install_recorder(monkeypatch)
    pdf = pss.generate_bulk_packing_slip_pdf(entries)

    assert header_labels_in_print_order(made[0]) == [
        "T4-first", "T4-second", "U7-first", "U7-second", "U5",
    ]
    text = extract_text(pdf)
    assert "EMPTY" not in text
    # T4-first and T4-second share one quarter sheet, then three full pages.
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 4


def test_print_all_still_starts_every_order_on_a_fresh_page_after_reordering(monkeypatch):
    """A multi-panel tracked order printed first, then a multistamp order:
    each has its own first page or panel and its own Page X of Y count.

    T31 is three quarter panels (15 rows, 15 rows, 1 row and the summary)
    and T1 the fourth, so sheet 1 is exactly full -- and the untracked
    order still starts on a fresh page, not a blank one after it."""
    small_tracked = labelled("T1", 1, "ground_advantage")
    big_untracked = untracked(*slip_shape(31))
    big_untracked[0].external_label = big_untracked[0].external_order_id = "U31"
    big_tracked = slip_shape(31)
    big_tracked[0].shipping_method = "ground_advantage"
    big_tracked[0].external_label = big_tracked[0].external_order_id = "T31"

    made = install_recorder(monkeypatch)
    pdf = pss.generate_bulk_packing_slip_pdf([big_untracked, small_tracked, big_tracked])
    records = made[0].records

    assert header_labels_in_print_order(made[0]) == ["T31", "T1", "U31"]
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 3
    assert [r["page"] for r in records if r["text"] == "Packing Slip"] == [1, 1, 1, 1, 2]
    assert [(r["page"], r["text"]) for r in records if is_page_marker(r)] == [
        (1, "Page 1 of 3"), (1, "Page 2 of 3"), (1, "Page 3 of 3"),
        (2, "Page 1 of 2"), (3, "Page 2 of 2"),
    ]
    assert [r["page"] for r in records if is_tag(r)] == [2]


def test_a_single_order_slip_is_just_that_order():
    pdf = pss.generate_packing_slip_pdf(*labelled("U8", 8, None))
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "Order #U8" in text and pss.MULTISTAMP_TEXT in text


# --- Tracked orders print as quarter-size panels, four to a sheet -------------
#
# Operator, 2026-10-28 and 2026-10-30: a tracked order ships in a bubble
# mailer under a printed label, so its slip is a 4.25 x 5.5in quarter panel,
# up to four to a letter sheet, with dashed cut lines. 2026-11-03: an order
# too long for one panel continues onto the next panel.
#
# These read the DRAWN ink off the recording canvas -- on-sheet position,
# alignment, font, fill, rects and lines -- because every way this can go
# wrong (a row under a cut line, a panel shared by two orders, a grey
# summary) is invisible in extracted text.

TRACKED = "ground_advantage"
EDGE_CLEARANCE = 0.2 * pss.inch


def tracked_order(label="T-1", **overrides):
    return make_order(
        external_label=label, external_order_id=label, shipping_method=TRACKED, **overrides,
    )


def tracked_shape(label, plain, wrapped=0, shipping_cents=135):
    items = [make_item() for _ in range(plain)]
    items += [make_item(name=LONG_DFC) for _ in range(wrapped)]
    return tracked_order(label, shipping_cents=shipping_cents), items


def quarter_of(x, y) -> int:
    """Which panel a point on the sheet is in: 0 top-left, 1 top-right,
    2 bottom-left, 3 bottom-right -- pss.PANEL_ORIGINS' order."""
    return (0 if x < pss.PANEL_W else 1) + (0 if y >= pss.PANEL_H else 2)


def panel_box(index):
    x, y = pss.PANEL_ORIGINS[index]
    return x, y, x + pss.PANEL_W, y + pss.PANEL_H


def ink_box(record):
    """A drawn string's real ink box, using the recorded alignment."""
    font, size = record["font"]
    width = stringWidth(record["text"], font, size)
    left = record["x"] - width if record["align"] == "right" else record["x"]
    return (
        left, record["y"] + getDescent(font, size),
        left + width, record["y"] + getAscent(font, size),
    )


def is_panel_heading(record) -> bool:
    """A panel's "Order #..." line. The full slip's header is right-aligned
    and its continued banner is 8pt, so neither matches."""
    return (
        record["text"].startswith("Order #")
        and record["align"] == "left"
        and record["font"] == ("Helvetica", 9)
    )


def panel_headings(canvas_):
    return [
        (r["page"], quarter_of(r["x"], r["y"]), r["text"])
        for r in canvas_.records if is_panel_heading(r)
    ]


def cut_lines(canvas_):
    """The full slip's fold line is dashed too (3, 3, grey), so a cut line
    is told apart by its own dash pattern."""
    return [l for l in canvas_.lines if l["dash"] == pss.CUT_LINE_DASH]


def quarter_pages(canvas_):
    return sorted({l["page"] for l in cut_lines(canvas_)})


def in_panel(canvas_, page, index):
    return [
        r for r in canvas_.records
        if r["page"] == page and quarter_of(r["x"], r["y"]) == index
    ]


def panel_origin_x(record):
    return pss.PANEL_ORIGINS[quarter_of(record["x"], record["y"])][0]


def panel_qty_cells(records):
    """Each panel row's Qty: plain 8pt digits at the panel's left pad."""
    return [
        r for r in records
        if r["align"] == "left" and r["font"] == ("Helvetica", pss.ITEM_FONT_SIZE)
        and r["text"].isdigit()
        and r["x"] == pytest.approx(panel_origin_x(r) + pss.PANEL_LEFT)
    ]


def panel_card_cells(records):
    return [
        r for r in records
        if r["font"] == (pss.CARD_FONT, pss.ITEM_FONT_SIZE) and r["text"] != "Card"
        and r["x"] == pytest.approx(panel_origin_x(r) + pss.PANEL_CARD_X)
    ]


def render_quarters(entries=None, *, single=None):
    with pytest.MonkeyPatch.context() as mp:
        made = install_recorder(mp)
        if single is not None:
            pdf = pss.generate_packing_slip_pdf(*single)
        else:
            pdf = pss.generate_bulk_packing_slip_pdf(entries)
    return made[0], pdf


def sheet_position(k):
    """Where an order's k-th panel (from 0) lands when it starts a fresh
    sheet: (page, quarter)."""
    return k // pss.PANELS_PER_SHEET + 1, k % pss.PANELS_PER_SHEET


def test_a_panel_is_a_quarter_of_a_letter_sheet():
    assert pss.PANEL_W == pytest.approx(4.25 * pss.inch)
    assert pss.PANEL_H == pytest.approx(5.5 * pss.inch)
    assert pss.PANEL_ORIGINS == [
        (0, pss.PANEL_H), (pss.PANEL_W, pss.PANEL_H), (0, 0), (pss.PANEL_W, 0),
    ]


def test_a_tracked_order_prints_one_top_left_panel_with_cut_lines():
    canvas_, pdf = render_quarters(single=(tracked_order(), [make_item(), make_item(name="Sol Ring")]))

    assert len(PdfReader(io.BytesIO(pdf)).pages) == 1
    assert panel_headings(canvas_) == [(1, 0, "Order #T-1")]
    assert {quarter_of(r["x"], r["y"]) for r in canvas_.records} == {0}
    texts = [r["text"] for r in canvas_.records]
    assert "Packing Slip" in texts and "Order Date: 8/17/2026" in texts

    # Thin dashed black, along both centre lines, edge to edge.
    lines = cut_lines(canvas_)
    assert sorted((l["x1"], l["y1"], l["x2"], l["y2"]) for l in lines) == sorted([
        (pss.PANEL_W, 0, pss.PANEL_W, pss.PAGE_H),
        (0, pss.PANEL_H, pss.PAGE_W, pss.PANEL_H),
    ])
    assert [l for l in canvas_.lines if l["dash"]] == lines
    for l in lines:
        assert l["page"] == 1
        assert l["stroke"] == BLACK
        assert len(l["dash"]) == 2 and all(l["dash"]), "not a dashed line"
        assert l["width"] <= 1

    # ...and the buyer's PDF says the same.
    text = extract_text(pdf)
    assert "Packing Slip" in text and "Order #T-1" in text and "Sol Ring" in text


# Tracked orders of every shape that matters on one bulk print: a one-line
# order, the summary pushed alone onto a panel, wrapped and unbreakable
# names, an unpriced line, no Shipping row, a long detail line.
MIXED_TRACKED = [
    tracked_shape("TA", 1),
    tracked_shape("TB", 14),
    (tracked_order("TC", shipping_cents=None), [
        *[make_item(name=f"Card {n}") for n in range(25)],
        make_item(name=LONG_DFC, color="B"),
        make_item(name=LONG_SINGLE_FACE, color="WU"),
        make_item(name=UNBREAKABLE, color="", type_line="Artifact"),
        make_item(name="Unpriced", price_cents=None),
    ]),
    # Detail values far longer than any real one: the first wraps onto a
    # second detail line, the second has to be broken by character.
    (tracked_order("TD"), [
        make_item(set_code="PLST", collector_number="DOM-123456789-ABCDEFGHIJKLMNOP",
                  condition_id="DMG", finish="EF", language_id="ZHS",
                  price_cents=123456, quantity=12),
        make_item(name="Opt", collector_number="7" * 70),
    ]),
]


def test_an_overlong_detail_value_wraps_and_breaks_inside_the_card_width():
    _, items = MIXED_TRACKED[3]
    wrapped, broken = (pss.panel_detail_lines(item) for item in items)
    assert len(wrapped) == 2
    assert len(broken) >= 3
    for lines in (wrapped, broken):
        for line in lines:
            for x, text, font in line:
                assert x + stringWidth(text, font, pss.PANEL_DETAIL_FONT_SIZE) <= \
                    pss.PANEL_CARD_WIDTH + 1e-6
    # Nothing is cut: every character of the collector number is drawn.
    assert "".join(text for line in broken for _, text, _ in line if text.startswith(("#", "7"))) \
        == "#" + "7" * 70


def test_no_panel_ink_crosses_a_cut_line_or_comes_within_0_2in_of_a_panel_edge():
    entries = MIXED_TRACKED + [labelled("U1", 2, "first_class")]
    canvas_, _ = render_quarters(entries)
    pages = quarter_pages(canvas_)
    assert len(pages) >= 2, "expected more than one quarter sheet"

    def assert_inside(index, left, bottom, right, top, what):
        x0, y0, x1, y1 = panel_box(index)
        assert left >= x0 + EDGE_CLEARANCE and right <= x1 - EDGE_CLEARANCE, (what, left, right)
        assert bottom >= y0 + EDGE_CLEARANCE and top <= y1 - EDGE_CLEARANCE, (what, bottom, top)

    checked = 0
    for r in canvas_.records:
        if r["page"] in pages:
            assert_inside(quarter_of(r["x"], r["y"]), *ink_box(r), r["text"])
            checked += 1
    for rect in canvas_.rects:
        if rect["page"] in pages:
            assert_inside(
                quarter_of(rect["x"], rect["y"]),
                rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"], "shading",
            )
    for l in canvas_.lines:
        if l["page"] in pages and l["dash"] is None:
            assert_inside(
                quarter_of(l["x1"], l["y1"]),
                min(l["x1"], l["x2"]), min(l["y1"], l["y2"]),
                max(l["x1"], l["x2"]), max(l["y1"], l["y2"]), "rule",
            )
    assert checked > 100


def test_every_value_the_full_slip_shows_is_on_the_panel_line():
    """Qty, Card, Set, Cond, Finish, Lang, # and Price -- all eight of the
    full slip's columns, Language included."""
    item = make_item(
        name="Llanowar Elves", set_code="SLD", collector_number="7167",
        condition_id="LP", finish="FO", language_id="JA", price_cents=376, quantity=2,
    )
    canvas_, pdf = render_quarters(single=(tracked_order(), [item]))
    records = canvas_.records

    [qty] = panel_qty_cells(records)
    [card] = panel_card_cells(records)
    assert qty["text"] == "2"
    assert card["text"] == "Llanowar Elves (G)" and card["y"] == qty["y"]
    # Size 8: the summary's subtotal is the same $7.52, at size 9.
    [price] = [r for r in records if r["text"] == "$7.52" and r["font"][1] == pss.ITEM_FONT_SIZE]
    assert price["y"] == qty["y"] and price["align"] == "right"

    detail = [r for r in records if r["y"] < card["y"] and r["font"][1] == pss.PANEL_DETAIL_FONT_SIZE]
    assert [r["text"] for r in detail] == ["SLD", "LP", "Foil", "JA", "#7167"]
    assert {r["y"] for r in detail} == {card["y"] - pss.PANEL_DETAIL_LINE_HEIGHT}
    xs = [r["x"] for r in detail]
    assert xs == sorted(xs) and xs[0] == pytest.approx(pss.PANEL_CARD_X)
    # A non-normal finish is bold, as on the full slip.
    assert [r["font"][0] for r in detail if r["text"] == "Foil"] == ["Helvetica-Bold"]
    assert "Llanowar Elves (G)" in extract_text(pdf)


def test_every_panel_string_is_7pt_or_larger_and_solid_black():
    canvas_, pdf = render_quarters(MIXED_TRACKED)
    for r in canvas_.records:
        assert r["font"][1] >= 7, (r["text"], r["font"])
        assert r["fill"] == BLACK, (r["text"], r["fill"])
    # The content stream itself, where the v2.69.1 grey page lived.
    for p, runs in enumerate(text_fills_per_page(pdf), 1):
        assert runs, f"page {p} drew no text"
        for text, fill in runs:
            assert fill == BLACK, (p, text, fill)


def test_a_long_name_wraps_on_the_panel_rather_than_being_cut():
    items = [
        make_item(name=LONG_DFC, color="B"),
        make_item(name=UNBREAKABLE, color="", type_line="Artifact"),
    ]
    canvas_, _ = render_quarters(single=(tracked_order(), items))
    rows = panel_qty_cells(canvas_.records)
    cards = panel_card_cells(canvas_.records)
    assert len(rows) == 2

    # Rows print in slip order: the (B) card, then the colourless one.
    first = [r for r in cards if rows[0]["y"] >= r["y"] > rows[1]["y"]]
    second = [r for r in cards if r["y"] <= rows[1]["y"]]
    for lines in (first, second):
        assert len(lines) >= 2, [r["text"] for r in lines]
        ys = [r["y"] for r in lines]
        assert ys == sorted(ys, reverse=True) and len(set(ys)) == len(ys)
        for r in lines:
            assert r["font"][1] >= 7
            assert r["x"] + stringWidth(r["text"], *r["font"]) <= \
                pss.PANEL_CARD_X + pss.PANEL_CARD_WIDTH + 1e-6
    assert " ".join(r["text"] for r in first) == LONG_DFC + " (B)"
    assert "".join(r["text"] for r in second).replace(" (C)", "") == UNBREAKABLE
    assert not [r for r in canvas_.records if "…" in r["text"] or "..." in r["text"]]


def test_a_panel_has_no_address_and_no_fold():
    order = tracked_order(shipping_line2="Apt 4")
    canvas_, pdf = render_quarters(single=(order, [make_item()]))
    texts = [r["text"] for r in canvas_.records]
    for line in ("Adrian Lovato", "7554 Belmar Ave", "Apt 4", "Reseda, CA 91335-2428", "Fold"):
        assert line not in texts
        assert line not in extract_text(pdf)
    # The only dashed lines are the two cut lines; the fold line is gone.
    fold_y = pss._y_from_top(pss.FOLD_FROM_TOP)
    assert [l for l in canvas_.lines if l["dash"]] == cut_lines(canvas_)
    assert len(cut_lines(canvas_)) == 2
    assert not [l for l in canvas_.lines if l["y1"] == l["y2"] == fold_y]


@pytest.mark.parametrize("quantities", [[1] * 6, [6], [1] * 20, [3] * 31])
def test_a_tracked_order_never_shows_the_multistamp_tag(quantities):
    items = [make_item(name=f"Card {n}", quantity=q) for n, q in enumerate(quantities)]
    order = tracked_order()
    assert not pss.needs_multistamp(order, items)
    canvas_, pdf = render_quarters(single=(order, items))
    assert not [r for r in canvas_.records if is_tag(r)]
    assert pss.MULTISTAMP_TEXT not in extract_text(pdf)


def test_the_panel_summary_reads_exactly_like_the_full_slip_summary():
    """The wording is a copy (see _draw_panel_summary); this is what holds
    it to the original, unpriced-lines wording and em-dash Total included."""
    items = [make_item(price_cents=500), make_item(name="Sol Ring", price_cents=None),
             make_item(name="Opt", price_cents=None, quantity=3)]
    for shipping in (135, None):
        full, _ = render_quarters(single=(make_order(shipping_cents=shipping), items))
        panel, _ = render_quarters(single=(tracked_order(shipping_cents=shipping), items))
        full_summary = [r["text"] for r in summary_ink(full)[0]]
        panel_summary = [
            r["text"] for r in panel.records
            if r["font"][1] == 9 and not r["text"].startswith("Order")
        ]
        assert panel_summary == full_summary
        assert panel_summary[0] == "5 items (2 lines without a price, excluded)"
        assert panel_summary[-2:] == ["Total", "—"]

    # The Total row is shaded, under its own baseline, in the full slip's grey.
    [shade] = panel.rects
    [total] = [r for r in panel.records if r["text"] == "Total"]
    assert shade["fill"] == SUMMARY_SHADE
    assert shade["y"] == pytest.approx(total["y"] - pss.TOTAL_SHADE_UNDERHANG)
    assert total["font"] == ("Helvetica-Bold", 9)


# Item counts through and past the panel boundaries -- the summary alone on
# a panel, a second panel with one row, a third panel -- with and without
# Shipping, and with a wrapped last row.
PANEL_SWEEP = sorted(
    # 65 rows is five panels, so a long order also runs onto a second sheet.
    {(plain, 0, shipping) for plain in range(1, 66) for shipping in (135, None)}
    | {(plain, 1, shipping) for plain in (11, 12, 13, 14, 27, 28) for shipping in (135, None)},
    key=lambda shape: (shape[0], shape[1], shape[2] is None),
)


@pytest.mark.parametrize("plain,wrapped,shipping", PANEL_SWEEP)
def test_a_long_tracked_order_continues_onto_the_next_panels(plain, wrapped, shipping):
    order, items = tracked_shape("T-1", plain, wrapped, shipping)
    layout = pss.layout_quarter_slip(order, pss.sort_slip_items(items))
    canvas_, pdf = render_quarters(single=(order, items))
    panels = layout.panels

    # One heading per panel, in grid order from the top-left, continuing
    # onto a new sheet after the fourth; every continuation says so.
    expected = [
        (*sheet_position(k), "Order #T-1" if k == 0 else "Order #T-1 (continued)")
        for k in range(panels)
    ]
    assert panel_headings(canvas_) == expected
    assert len(PdfReader(io.BytesIO(pdf)).pages) == sheet_position(panels - 1)[0]

    # Page X of Y, counted in panels, on every panel -- or none at all.
    markers = [(r["page"], quarter_of(r["x"], r["y"]), r["text"])
               for r in canvas_.records if is_page_marker(r)]
    if panels == 1:
        assert markers == []
    else:
        assert markers == [
            (*sheet_position(k), f"Page {k + 1} of {panels}") for k in range(panels)
        ]

    # Every row drawn once, where the shared layout put it.
    rows = panel_qty_cells(canvas_.records)
    assert len(rows) == plain + wrapped
    for row, (panel, y) in zip(rows, layout.rows):
        page, index = sheet_position(panel - 1)
        assert (row["page"], quarter_of(row["x"], row["y"])) == (page, index)
        assert row["y"] == pytest.approx(pss.PANEL_ORIGINS[index][1] + y)

    # The summary is on the LAST panel only, after every row.
    totals = [(r["page"], quarter_of(r["x"], r["y"])) for r in canvas_.records if r["text"] == "Total"]
    assert totals == [sheet_position(panels - 1)]
    assert layout.summary_panel == panels


def test_the_sweep_reaches_a_panel_holding_only_the_summary():
    """So the moved-summary branch is exercised above, not just written."""
    alone = []
    for plain, wrapped, shipping in PANEL_SWEEP:
        order, items = tracked_shape("T-1", plain, wrapped, shipping)
        layout = pss.layout_quarter_slip(order, pss.sort_slip_items(items))
        if layout.rows[-1][0] < layout.summary_panel:
            alone.append((plain, wrapped, shipping))
    assert alone


def test_a_summary_alone_on_a_panel_gets_the_header_and_no_table_header():
    order, items = tracked_shape("T-1", 14)
    layout = pss.layout_quarter_slip(order, pss.sort_slip_items(items))
    assert layout.panels == 2 and all(panel == 1 for panel, _ in layout.rows)

    canvas_, _ = render_quarters(single=(order, items))
    texts = [r["text"] for r in in_panel(canvas_, 1, 1)]
    assert texts[:3] == ["Packing Slip", "Page 2 of 2", "Order #T-1 (continued)"]
    assert "Order Date: 8/17/2026" in texts
    assert not {"Qty", "Card", "Price"} & set(texts)
    assert "Total" in texts and "14 items" in texts


def test_two_orders_never_share_a_panel():
    canvas_, _ = render_quarters(MIXED_TRACKED)
    headings = panel_headings(canvas_)
    positions = [(page, index) for page, index, _ in headings]
    assert len(positions) == len(set(positions))
    # Each order's panels are consecutive, and every order starts its own.
    labels = [text.removeprefix("Order #").removesuffix(" (continued)") for _, _, text in headings]
    assert [label for n, label in enumerate(labels) if n == 0 or labels[n - 1] != label] == \
        ["TC", "TB", "TD", "TA"]
    total = sum(
        pss.layout_quarter_slip(order, pss.sort_slip_items(items)).panels
        for order, items in MIXED_TRACKED
    )
    assert len(headings) == total


def full_size_ink(canvas_, first_page):
    """Records on full-size pages, renumbered from 1, tag and markers out."""
    return [
        (r["page"] - first_page + 1, r["x"], r["y"], r["text"], r["font"], r["fill"])
        for r in canvas_.records
        if r["page"] >= first_page and not is_page_marker(r) and not is_tag(r)
    ]


def test_print_all_fills_quarter_panels_then_starts_untracked_on_a_fresh_page():
    """The ticket's example: three tracked orders, one long enough for two
    panels, and two untracked. Sheet 1 holds TL, TL continued, TA and TB;
    the untracked slips follow, each on its own full page."""
    tl = tracked_shape("TL", 20)
    ta = tracked_shape("TA", 3)
    tb = tracked_shape("TB", 2)
    ua = labelled("UA", 7, "first_class")
    ub = labelled("UB", 1, "first_class")
    canvas_, pdf = render_quarters([ub, tb, ua, tl, ta])

    assert panel_headings(canvas_) == [
        (1, 0, "Order #TL"), (1, 1, "Order #TL (continued)"),
        (1, 2, "Order #TA"), (1, 3, "Order #TB"),
    ]
    assert [r["text"] for r in canvas_.records if r["text"].startswith("Order #")] == [
        "Order #TL", "Order #TL (continued)", "Order #TA", "Order #TB",
        "Order #UA", "Order #UB",
    ]
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 3
    assert quarter_pages(canvas_) == [1]

    # Pages 2 and 3 are exactly the untracked slips as the frozen renderer
    # draws them -- UA's tag is the only addition -- and nothing of a
    # panel leaks onto them.
    before, before_pages = render_before([ua, ub])
    assert before_pages == 2
    assert full_size_ink(canvas_, 2) == all_ink(before)
    assert [r for r in canvas_.rects if r["page"] >= 2] == \
        [dict(r, page=r["page"] + 1) for r in before.rects]
    assert [r["page"] for r in canvas_.records if is_tag(r)] == [2]


def test_unused_panels_on_the_last_quarter_sheet_stay_blank():
    tl = tracked_shape("TL", 20)
    ta = tracked_shape("TA", 3)
    tb = tracked_shape("TB", 2)
    tc = tracked_shape("TC", 1)
    ua = labelled("UA", 2, "first_class")
    canvas_, pdf = render_quarters([ua, tc, tb, ta, tl])

    assert panel_headings(canvas_) == [
        (1, 0, "Order #TL"), (1, 1, "Order #TL (continued)"),
        (1, 2, "Order #TA"), (1, 3, "Order #TB"),
        (2, 0, "Order #TC"),
    ]
    assert quarter_pages(canvas_) == [1, 2]
    assert len(cut_lines(canvas_)) == 4
    assert {quarter_of(r["x"], r["y"]) for r in canvas_.records if r["page"] == 2} == {0}
    assert {quarter_of(r["x"], r["y"]) for r in canvas_.rects if r["page"] == 2} == {0}
    # UA starts on a fresh full page after the part-used sheet.
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 3
    assert [r["page"] for r in canvas_.records if r["text"] == "Packing Slip" and r["align"] == "right"] == [3]


def test_a_sheet_filled_exactly_leaves_no_blank_page():
    canvas_, pdf = render_quarters([tracked_shape(f"T{n}", 1) for n in range(4)])
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 1
    assert [index for _, index, _ in panel_headings(canvas_)] == [0, 1, 2, 3]


def test_a_single_tracked_slip_matches_the_same_order_through_print_all():
    shape = tracked_shape("TL", 33, 1)
    single, single_pdf = render_quarters(single=shape)
    bulk, bulk_pdf = render_quarters([shape])
    assert all_ink(single) == all_ink(bulk)
    assert rect_ink(single) == rect_ink(bulk)
    assert single.lines == bulk.lines
    assert panel_headings(single)[0] == (1, 0, "Order #TL")
    assert len(PdfReader(io.BytesIO(single_pdf)).pages) == len(PdfReader(io.BytesIO(bulk_pdf)).pages)


# ★ v2.96.0'S TWO GENERATORS, FROZEN, test-only. Untracked output must be
# op-for-op what they drew. They call the live _draw_one_slip, which this
# change leaves byte-identical; the frozen 9fff7e1 renderer above
# (render_before) separately pins _draw_one_slip itself.

def _v2_96_single(c, order, items):
    pss._draw_one_slip(c, order, items)


def _v2_96_bulk(c, orders_with_items):
    for order, items in pss.sort_print_orders(orders_with_items):
        if not items:
            continue
        pss._draw_one_slip(c, order, items)


def full_record(canvas_):
    return (
        [tuple(sorted(r.items())) for r in canvas_.records],
        [tuple(sorted(r.items())) for r in canvas_.rects],
        [tuple(sorted(l.items())) for l in canvas_.lines],
    )


UNTRACKED_SHAPES = [
    labelled("U1", 1, "first_class"),
    labelled("U5", 5, None),
    labelled("U6", 6, "first_class"),            # tagged
    untracked(*slip_shape(30)),                  # summary moved to page 2
    untracked(*slip_shape(31), "first_class"),   # items onto page 2
    untracked(*slip_shape(77)),                  # summary moved to page 3
    untracked(*slip_shape(75, 1, None)),
    labelled("EMPTY", 0, None),
]


@pytest.mark.parametrize("shape", UNTRACKED_SHAPES[:-1])
def test_an_untracked_single_slip_is_op_for_op_v2_96(shape):
    now, _ = render_quarters(single=shape)
    before = RecordingCanvas(io.BytesIO(), pagesize=pss.LETTER)
    _v2_96_single(before, *shape)
    assert full_record(now) == full_record(before)
    assert not cut_lines(now)


def test_a_print_all_with_no_tracked_orders_is_op_for_op_v2_96():
    now, _ = render_quarters(UNTRACKED_SHAPES)
    before = RecordingCanvas(io.BytesIO(), pagesize=pss.LETTER)
    _v2_96_bulk(before, UNTRACKED_SHAPES)
    assert full_record(now) == full_record(before)
    assert not cut_lines(now)
