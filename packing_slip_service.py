"""Generates a printable packing slip / order receipt PDF for one order.

Layout is modeled on a real Mana Pool order-slip PDF (order
#538473-1913946), with two measurements preserved exactly because they are
envelope-critical for a #10 window envelope: the single horizontal fold at
3.667in from the top, and the recipient address block at 2.44-2.99in from
the top / 1.38in from the left. Everything below the fold (the itemized
line-items/pricing table) stays below the fold on purpose -- that's what
keeps pricing hidden once the slip is folded, matching the reference
document's privacy structure.

The header above the fold is NOT a 1:1 copy of Mana Pool's: their logo is a
small horizontal wordmark that fit their short header band; Chris's Cards'
mark is a portrait illustration that would look cramped in that same band,
so the header zone here is taller (0.30-2.15in from top) to give the logo
room, using space between the header and the (unmoved) address block.
Mana Pool's buyer-facing "scan to mark received" QR code is dropped
entirely -- CardFoundry has no equivalent flow, and faking a QR that points
nowhere would be worse than omitting it.
"""

import logging
import io
import os

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from card_color_marker import FACE_SEPARATOR, color_marker, color_sort_group
from natural_sort import natural_sort_key


# Same shared logger as the rest of the app (v1.155.0).
logger = logging.getLogger("cardfoundry")

PAGE_W, PAGE_H = LETTER  # 8.5in x 11in

LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "chriss_cards_logo.png")

LEFT_MARGIN = 0.4 * inch
RIGHT_MARGIN = 0.4 * inch
USABLE_WIDTH = PAGE_W - LEFT_MARGIN - RIGHT_MARGIN

HEADER_TOP = 0.30 * inch          # from top
HEADER_BOTTOM = 2.15 * inch       # from top
ADDRESS_TOP = 2.44 * inch         # from top -- envelope-critical, do not move
ADDRESS_LEFT = 1.38 * inch        # from left -- envelope-critical, do not move
FOLD_FROM_TOP = 3.667 * inch      # envelope-critical, do not move
TABLE_TOP_PAD = 0.35 * inch       # gap after the fold before the table starts
BOTTOM_MARGIN = 0.5 * inch

# ★ WIDTHS REBALANCED 2026-10-26, MEASURED NOT GUESSED. Card was 2.3in
# and 217 of 12,754 production order lines did not fit it; on 137 of them
# the colour marker was drawn underneath the Set column's set code, which
# is painted after it, so the marker was there and invisible. Meanwhile
# Set was 1.6in to hold a set code whose widest value in all of
# production is 'PDP13' at 25.3pt -- 89.9pt of slack sitting directly
# right of the column that was overflowing.
#
# Set keeps 0.8in (operator decision: 0.49in fitted but looked cramped),
# and the rest comes from Cond and Lang, whose widest real values are
# 'DMG' at 18.7pt and 'DW' at 13.3pt. Their new widths are set by their
# HEADER labels, not their values: "Cond" is 20.4pt and "Lang" 19.1pt, so
# 0.35in/0.3in are the tightest honest sizes.
#
# THE TOTAL MUST STAY EXACTLY 7.7in == USABLE_WIDTH, or the table runs
# off the page. test_packing_slip_service pins both the sum and the fact
# that every header label still fits its column.
COLUMNS = [
    ("Qty", 0.4 * inch, "left"),
    ("Card", 3.45 * inch, "left"),
    ("Set", 0.8 * inch, "left"),
    ("Cond", 0.35 * inch, "left"),
    ("Finish", 0.7 * inch, "left"),
    ("Lang", 0.3 * inch, "left"),
    ("#", 0.6 * inch, "left"),
    ("Price", 1.1 * inch, "right"),
]

COLUMN_WIDTHS = {label: width for label, width, _ in COLUMNS}
CARD_WIDTH = COLUMN_WIDTHS["Card"]

# The Card cell is the only wrapping cell, and it is drawn bold so that
# the measurement and the drawing use the same font (a mismatch here is
# how a "fits" check passes and the ink still overruns).
CARD_FONT = "Helvetica-Bold"
ITEM_FONT_SIZE = 8

ITEM_ROW_HEIGHT = 14        # pitch of a normal, unwrapped row
WRAPPED_LINE_HEIGHT = 9     # each additional line WITHIN one row


# ★ A COPY OF main.TRACKING_REQUIRED_SHIPPING_METHOD, NOT AN IMPORT OF IT.
# main.py imports this module at startup, so importing main back from here
# is a circular import -- and would drag the whole 34,000-line app into a
# PDF renderer. test_packing_slip_service asserts the two are equal, so
# they cannot drift apart silently.
TRACKED_SHIPPING_METHOD = "ground_advantage"

# Operator, 2026-10-28: an untracked order of this many cards or more is
# too heavy for one stamp. Counted in CARDS (total quantity), not lines.
MULTISTAMP_MIN_CARDS = 6
MULTISTAMP_TEXT = "Multistamp needed"
MULTISTAMP_FONT = ("Helvetica-Bold", 10)


def total_quantity(items: list) -> int:
    """Cards in the order: the same sum the summary prints as "N items"."""
    return sum(item.quantity for item in items)


def is_tracked(order) -> bool:
    return order.shipping_method == TRACKED_SHIPPING_METHOD


def needs_multistamp(order, items: list) -> bool:
    """The ONE rule behind both the slip's tag and Print All's order, so
    the tag on a slip and the group it was printed in cannot disagree.

    A tracked order never qualifies, whatever its size: it ships on a
    label, not stamps. Anything else -- first_class, or no method recorded
    at all -- goes by card count.
    """
    return not is_tracked(order) and total_quantity(items) >= MULTISTAMP_MIN_CARDS


def _y_from_top(distance_from_top: float) -> float:
    return PAGE_H - distance_from_top


def _draw_fold_line(c: canvas.Canvas) -> None:
    """The dashed fold guide and its small grey "Fold" label.

    ★ EVERY STATE CHANGE IS INSIDE ONE saveState/restoreState, and the
    restore is the LAST thing this function does. The grey for the label
    used to be set AFTER the restore and never put back, so it stayed in
    force for the rest of the page -- the whole line-items table, and on
    a one-page slip the totals too, printed in 50% grey. `showPage()`
    resets the graphics state, so page 2 onwards printed solid black and
    the same slip came out two different shades. That is how it survived:
    the defect is invisible unless you compare two pages, and it is
    invisible in extracted text, which is all any earlier test read.

    Reported by the operator 2026-10-26 as "page 2 looks bold" -- it was
    page 1 being washed out. Fixed 2026-10-27, v2.69.1. The label itself
    is still deliberately grey; nothing else is.
    """
    y = _y_from_top(FOLD_FROM_TOP)
    c.saveState()
    c.setDash(3, 3)
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.line(LEFT_MARGIN, y, PAGE_W - RIGHT_MARGIN, y)
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawCentredString(PAGE_W / 2, y + 2, "Fold")
    c.restoreState()


def _draw_header(c: canvas.Canvas, order, multistamp: bool = False) -> None:
    top_y = _y_from_top(HEADER_TOP)
    bottom_y = _y_from_top(HEADER_BOTTOM)
    band_height = top_y - bottom_y

    logo_size = min(band_height - 0.1 * inch, 1.7 * inch)
    if os.path.exists(LOGO_PATH):
        try:
            image = ImageReader(LOGO_PATH)
            c.drawImage(
                image,
                LEFT_MARGIN + 0.15 * inch,
                bottom_y + (band_height - logo_size) / 2,
                width=logo_size, height=logo_size,
                mask="auto", preserveAspectRatio=True, anchor="c",
            )
        except Exception as exc:
            # Same call as the buylist PDF header: decoration, so still a
            # swallow, but no longer a silent one.
            logger.warning(
                "packing slip: header logo could not be drawn, continuing "
                "without it: %s: %s", type(exc).__name__, exc,
            )

    right_x = PAGE_W - RIGHT_MARGIN - 0.15 * inch
    text_y = top_y - 0.30 * inch
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(right_x, text_y, "Packing Slip")
    c.setFont("Helvetica", 9)
    text_y -= 0.22 * inch
    label = order.external_label or order.external_order_id
    c.drawRightString(right_x, text_y, f"Order #{label}")
    text_y -= 0.18 * inch
    order_date = order.created_at.strftime("%-m/%-d/%Y") if order.created_at else ""
    c.drawRightString(right_x, text_y, f"Order Date: {order_date}")

    if multistamp:
        # One more line at the same 0.22in step as "Packing Slip" to
        # "Order #": about 1.22in from the top, so well inside the header
        # band (which ends at 2.15in) and right of the logo, which stops
        # near 2.25in from the LEFT edge. Drawn here, in the header, so it
        # is on the first page only -- continuation pages never draw one.
        #
        # Black is set explicitly inside its own saveState: this is a tag
        # the operator must not miss, and the v2.69.1 grey-page bug was a
        # fill inherited from a helper that did not clean up.
        c.saveState()
        c.setFillColorRGB(0, 0, 0)
        c.setFont(*MULTISTAMP_FONT)
        c.drawRightString(right_x, text_y - 0.22 * inch, MULTISTAMP_TEXT)
        c.restoreState()


def _draw_address(c: canvas.Canvas, order) -> None:
    lines = [
        order.shipping_name,
        order.shipping_line1,
        order.shipping_line2,
    ]
    city_line = ", ".join(
        part for part in [order.shipping_city, order.shipping_state] if part
    )
    if order.shipping_postal_code:
        city_line = f"{city_line} {order.shipping_postal_code}".strip()
    if city_line:
        lines.append(city_line)
    if order.shipping_country and order.shipping_country.upper() not in {"US", "USA"}:
        lines.append(order.shipping_country)
    lines = [line for line in lines if line]

    x = LEFT_MARGIN + ADDRESS_LEFT
    y = _y_from_top(ADDRESS_TOP)
    c.setFont("Helvetica", 10)
    for line in lines:
        c.drawString(x, y, line)
        y -= 12


# OrderItem.finish stores Mana Pool's two-letter finish_id code (confirmed
# in production: NF/FO/EF), unlike InventoryCard.finish which stores the
# full word -- these need mapping, not just capitalizing, or "NF" prints
# as the meaningless "Nf".
FINISH_LABELS = {
    "NF": "Non-Foil",
    "FO": "Foil",
    "EF": "Etched",
}


def _finish_label(finish: str | None) -> str:
    if not finish:
        return ""
    code = finish.strip().upper()
    return FINISH_LABELS.get(code, finish.strip().capitalize())


def _is_non_normal_finish(finish: str | None) -> bool:
    if not finish:
        return False
    code = finish.strip().upper()
    if code in FINISH_LABELS:
        return code != "NF"
    return code.lower() != "normal"


def _line_total_cents(item) -> int | None:
    """Line total, or None when the line has no stored price.

    This used to return 0 for an unpriced line, so the slip printed a
    confident "$0.00" and silently counted it as nothing in the subtotal
    and Total -- a wrong number that looked exact. Five real order lines
    have no stored price. None now propagates: the cell prints an em dash
    and the line is EXCLUDED from the subtotal, matching how the Orders
    list and Order Detail treat the same gap.
    """
    if item.price_cents is None:
        return None
    return item.price_cents * item.quantity


def _money(cents: int | None) -> str:
    """Same em-dash-for-unknown convention as the web pages."""
    if cents is None:
        return "\u2014"
    return f"${cents / 100:.2f}"


def _draw_table_header(c: canvas.Canvas, y: float) -> None:
    c.setFont("Helvetica-Bold", 8)
    x = LEFT_MARGIN
    for label, width, align in COLUMNS:
        if align == "right":
            c.drawRightString(x + width, y, label)
        else:
            c.drawString(x, y, label)
        x += width
    c.saveState()
    c.setStrokeColorRGB(0.2, 0.2, 0.2)
    c.line(LEFT_MARGIN, y - 3, PAGE_W - RIGHT_MARGIN, y - 3)
    c.restoreState()


def _hard_break(token: str, width: float) -> list[str]:
    """Break a single unbreakable token by character.

    Only reachable for a token with no space in it that is still wider
    than the Card cell. No real card name is, at 3.45in -- the widest in
    production is 240.9pt against 248.4pt of cell -- so this is a
    guarantee rather than a code path anyone should see.

    It breaks rather than truncates because truncating was explicitly
    ruled out (operator decision 2026-10-26: nothing is cut), and
    overrunning the cell is the bug being fixed. Breaking is the only
    remaining option that keeps every character on the page.
    """
    lines, current = [], ""
    for char in token:
        if current and stringWidth(current + char, CARD_FONT, ITEM_FONT_SIZE) > width:
            lines.append(current)
            current = char
        else:
            current += char
    if current:
        lines.append(current)
    return lines or [token]


def _wrap_words(text: str, width: float, tail: str = "") -> list[str]:
    """Greedy word wrap, with ``tail`` welded to the final word.

    ★ THE TAIL IS WHY THIS IS NOT A GENERIC WRAPPER. It carries the colour
    marker (or the " //" face separator), and it is glued to the last word
    BEFORE any wrapping decision is made, so it becomes part of an atomic
    token. That is what makes it impossible for the marker to be orphaned
    on a line of its own or pushed off the end -- the operator's rule is
    that the marker must never be split from the name or lost, and the
    only way to guarantee that is for the wrapper never to see it as
    separable in the first place.
    """
    words = (text or "").split()
    if not words:
        stripped = tail.strip()
        return [stripped] if stripped else []
    words[-1] = words[-1] + tail

    lines, current = [], ""
    for word in words:
        if current:
            candidate = f"{current} {word}"
            if stringWidth(candidate, CARD_FONT, ITEM_FONT_SIZE) <= width:
                current = candidate
                continue
            lines.append(current)
            current = ""
        # `word` now starts a line of its own. If it STILL does not fit,
        # nothing can be gained by wrapping it, so it is broken. Checking
        # this here rather than against the accumulated line is the
        # correction to a bug I wrote and then caught in this same
        # session: welding the tail on introduces a space, so a
        # `" " not in current` test read the pathological single-token
        # case as breakable-by-space and let it overrun the cell.
        if stringWidth(word, CARD_FONT, ITEM_FONT_SIZE) > width:
            pieces = _hard_break(word, width)
            lines.extend(pieces[:-1])
            current = pieces[-1]
        else:
            current = word
    if current:
        lines.append(current)
    return lines


def card_cell_lines(name: str | None, suffix: str, width: float) -> list[str]:
    """The Card cell as one string per rendered line.

    One line whenever the name and its marker fit, which is the normal
    case: after the 2026-10-26 rebalance that is every one of the 12,754
    order lines in production.

    A name that does not fit breaks at " // " FIRST, because every
    overlong name in production is a double-faced card and the face
    boundary is where a reader expects the split -- the picker is holding
    the front face. The separator stays on the first line, so line one
    does not read as a different, shorter card.
    """
    name = name or ""
    if stringWidth(name + suffix, CARD_FONT, ITEM_FONT_SIZE) <= width:
        return [name + suffix]

    if FACE_SEPARATOR in name:
        faces = name.split(FACE_SEPARATOR)
        lines = []
        for index, face in enumerate(faces):
            is_last = index == len(faces) - 1
            # The separator is welded to its face's last word for exactly
            # the same reason as the marker: so it cannot be orphaned.
            tail = suffix if is_last else FACE_SEPARATOR.rstrip()
            lines.extend(_wrap_words(face, width, tail))
        return lines

    return _wrap_words(name, width, suffix)


def _color_suffix(color, type_line=None) -> str:
    """Plain-text "(WU)" next to a printed card name -- packing slips are
    often printed in black and white, so this mirrors the HTML color-pip
    badges without relying on color to carry the information.

    The RULE lives in card_color_marker, not here, so the HTML badge sites
    can adopt the same one later: a LAND is (L) whatever its colour, a
    colourless non-land is (C), and a card whose type we have not looked up
    yet gets NO marker rather than a confident, wrong (C).
    """
    marker = color_marker(color, type_line)
    if not marker:
        return ""
    return f" ({marker})"


def slip_sort_key(item) -> tuple:
    """Where one line sorts on the slip (operator decision, 2026-10-26).

    Colour group first, then the card NAME A-Z, then set code and collector
    number as tie-breaks so two printings of the same card always come out
    in the same order rather than in whatever order the rows were fetched.

    ★ THE GROUP COMES FROM card_color_marker, NOT FROM A SECOND COLOUR TEST
    HERE. The printed marker and the group are the same rule evaluated
    once, so a line cannot show (L) and sort among the green cards.

    Name sorts case-insensitively (A-Z as a human reads it) with the raw
    name kept as the next element, so two names differing only in case
    still have one fixed order instead of depending on the input order.
    Set code and collector number use the app's shared natural sort, so
    collector 2 comes before 10 -- a plain string sort gets that backwards,
    and collector numbers are exactly the short alphanumeric codes that
    rule was written for.
    """
    name = item.name or ""
    return (
        color_sort_group(item.color, getattr(item, "type_line", None)),
        name.casefold(),
        name,
        natural_sort_key(item.set_code),
        natural_sort_key(item.collector_number),
    )


def sort_slip_items(items: list) -> list:
    """The slip's line order. Returns a NEW list; the caller's is untouched
    (the routes hand the same list to the renderer and to nothing else
    today, but a sort that reaches back out of the renderer would be a
    surprise waiting to happen).

    Python's sort is stable, so lines that tie on every key above stay in
    the order they arrived -- which for both routes is OrderItem.id, i.e.
    the order Mana Pool sent them.
    """
    return sorted(items, key=slip_sort_key)


def item_card_lines(item) -> list[str]:
    """The Card cell's lines for one item. One line in the normal case."""
    return card_cell_lines(
        item.name,
        _color_suffix(item.color, getattr(item, "type_line", None)),
        CARD_WIDTH,
    )


def item_row_height(item) -> float:
    """How much vertical space this row needs, wrapping included.

    The page-break check calls this BEFORE drawing, which is what keeps a
    wrapped row from being split across a page boundary.
    """
    extra_lines = len(item_card_lines(item)) - 1
    return ITEM_ROW_HEIGHT + extra_lines * WRAPPED_LINE_HEIGHT


def _draw_item_row(c: canvas.Canvas, y: float, item) -> float:
    """Draws one item row at baseline ``y`` and returns its height.

    Every cell other than Card is drawn on the row's FIRST line, so a
    wrapped row still reads across cleanly instead of floating its
    quantity and price next to the second half of a card name.
    """
    card_lines = item_card_lines(item)
    values = [
        str(item.quantity),
        None,  # Card is drawn from card_lines, which may be several.
        item.set_code or "",
        (item.condition_id or ""),
        _finish_label(item.finish),
        (item.language_id or ""),
        item.collector_number or "",
        _money(_line_total_cents(item)),
    ]
    non_normal = _is_non_normal_finish(item.finish)

    x = LEFT_MARGIN
    c.setFont("Helvetica", 8)
    for (label, width, align), value in zip(COLUMNS, values):
        if label == "Card":
            c.setFont(CARD_FONT, ITEM_FONT_SIZE)
            line_y = y
            for line in card_lines:
                c.drawString(x, line_y, line)
                line_y -= WRAPPED_LINE_HEIGHT
            c.setFont("Helvetica", 8)
        elif label == "Finish" and non_normal:
            c.setFont("Helvetica-Bold", 8)
            c.drawString(x, y, value)
            c.setFont("Helvetica", 8)
        elif align == "right":
            c.drawRightString(x + width, y, value)
        else:
            c.drawString(x, y, value)
        x += width

    return ITEM_ROW_HEIGHT + (len(card_lines) - 1) * WRAPPED_LINE_HEIGHT


SUMMARY_ROW_PITCH = 16
# The Total row's shading starts this far BELOW its own baseline, so it --
# not any baseline -- is the lowest ink on the slip.
TOTAL_SHADE_UNDERHANG = 3


def summary_block_height(order) -> float:
    """From the first summary baseline down to the Total shading's bottom.

    Derived from the same two constants `_draw_summary_rows` draws with, and
    the same `shipping_cents is not None` test it uses to add the Shipping
    row, so the page-break check cannot drift from what is actually drawn.
    """
    rows = 3 if order.shipping_cents is not None else 2
    return SUMMARY_ROW_PITCH * (rows - 1) + TOTAL_SHADE_UNDERHANG


def _summary_fits(y: float, order) -> bool:
    return y - summary_block_height(order) >= BOTTOM_MARGIN


def _draw_summary_rows(
    c: canvas.Canvas, y: float, order, subtotal_cents: int, item_count: int,
    unpriced_lines: int = 0,
) -> float:
    label_x = LEFT_MARGIN
    value_x = PAGE_W - RIGHT_MARGIN

    def row(label: str, amount_cents: int | None, *, bold: bool = False, shaded: bool = False) -> float:
        nonlocal y
        if shaded:
            c.saveState()
            c.setFillColorRGB(0.93, 0.93, 0.93)
            c.rect(LEFT_MARGIN, y - TOTAL_SHADE_UNDERHANG, USABLE_WIDTH, 14, stroke=0, fill=1)
            c.restoreState()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(label_x, y, label)
        c.drawRightString(value_x, y, _money(amount_cents))
        y -= SUMMARY_ROW_PITCH
        return y

    item_word = "item" if item_count == 1 else "items"
    subtotal_label = f"{item_count} {item_word}"
    if unpriced_lines:
        # Named on the slip, because a subtotal that quietly omits a line
        # is indistinguishable from one that is simply smaller.
        line_word = "line" if unpriced_lines == 1 else "lines"
        subtotal_label += f" ({unpriced_lines} {line_word} without a price, excluded)"
    y = row(subtotal_label, subtotal_cents)

    total_cents = subtotal_cents
    if order.shipping_cents is not None:
        y = row("Shipping", order.shipping_cents)
        total_cents += order.shipping_cents

    # An order with an unpriced line has no knowable total. Printing the
    # partial sum as "Total" would be the same lie in a bolder font.
    y = row("Total", None if unpriced_lines else total_cents, bold=True, shaded=True)
    return y


CONTINUED_BANNER_Y = PAGE_H - 0.5 * inch
# Where a continuation page's content starts (its table header, or a moved
# summary's first row less the 6pt gap). A constant rather than something
# only _start_continued_page knows, because the layout pass below has to
# know it without a canvas.
CONTINUED_TOP = PAGE_H - 0.8 * inch

# ★ THE PAGE MARKER SITS IN THE BOTTOM MARGIN, bottom right. Nothing else on
# any page is drawn there: item rows stop 40pt above BOTTOM_MARGIN, and the
# lowest summary ink -- the Total shading -- stops AT it (_summary_fits).
# A 7pt baseline at 24pt puts its descenders about 22.5pt from the bottom
# edge and its tallest glyph about 29pt up, clear of both the 18pt
# printable limit and the 36pt margin. The top of the page was rejected:
# page 1's header band and a continuation page's banner both live there.
PAGE_MARKER_FONT = ("Helvetica", 7)
PAGE_MARKER_Y = 24


def _draw_page_marker(c: canvas.Canvas, page: int, pages: int) -> None:
    """The small "Page X of Y" on one page of a multi-page slip.

    Solid black set EXPLICITLY, inside its own saveState/restoreState --
    the v2.69.1 grey-page bug was a colour inherited from a helper that
    did not clean up, and this one neither inherits nor leaves anything.
    """
    c.saveState()
    c.setFillColorRGB(0, 0, 0)
    c.setFont(*PAGE_MARKER_FONT)
    c.drawRightString(PAGE_W - RIGHT_MARGIN, PAGE_MARKER_Y, f"Page {page} of {pages}")
    c.restoreState()


def _start_continued_page(c: canvas.Canvas, order) -> float:
    """Starts a new page with the "(continued)" banner; returns the top y.

    Shared by item overflow and a moved summary, so a continuation page
    looks the same whichever of them caused it.
    """
    c.showPage()
    c.setFont("Helvetica", 8)
    label = order.external_label or order.external_order_id
    c.drawString(LEFT_MARGIN, CONTINUED_BANNER_Y, f"Order #{label} (continued)")
    return CONTINUED_TOP


class SlipLayout:
    """Where one slip's rows and summary land: pages are counted from 1
    within the order, never across a bulk PDF."""

    def __init__(self, table_top: float, rows: list, summary_page: int, summary_y: float):
        self.table_top = table_top
        self.rows = rows                # (page, baseline y) per sorted item
        self.summary_page = summary_page
        self.summary_y = summary_y
        self.pages = summary_page       # the summary is always the last thing


def layout_slip(order, items: list) -> SlipLayout:
    """Every page-break decision on a slip, made without drawing anything.

    ``items`` must already be in slip order (sort_slip_items).

    ★ THIS IS THE ONLY PLACE THE BREAKS ARE DECIDED. "Page X of Y" has to
    know Y while drawing page 1, and the obvious way to get it -- a second
    loop that re-runs the reserve arithmetic just to count -- is two
    copies of the break rule that drift the first time either is edited.
    So the drawing loop no longer decides anything: _draw_one_slip draws
    each row and the summary at the page and y this returns, and the
    marker reads its Y from the same result.

    The arithmetic is the pre-change loop's, step for step, including
    subtracting a wrapped row's extra height and its pitch as two separate
    steps: folding them into one subtraction can round the float y
    differently and move a row by a fraction of a point.
    """
    table_top = _y_from_top(FOLD_FROM_TOP + TABLE_TOP_PAD)
    page = 1
    y = table_top - ITEM_ROW_HEIGHT
    rows = []
    for item in items:
        # ★ MEASURE THE ROW BEFORE DECIDING THE PAGE. A wrapped row is
        # taller than the pitch, and its lines are drawn DOWNWARD from
        # this baseline, so the break has to be judged against the row's
        # full height. Testing bare `y` here -- which is what this did
        # before wrapping existed -- would let a two-line row start just
        # above the margin and put its second line off the bottom of the
        # page, which is the one failure mode wrapping must not introduce.
        extra = item_row_height(item) - ITEM_ROW_HEIGHT
        if y - extra < BOTTOM_MARGIN + 40:
            page += 1
            y = CONTINUED_TOP - ITEM_ROW_HEIGHT
        rows.append((page, y))
        y -= extra
        y -= ITEM_ROW_HEIGHT

    y -= 6
    # ★ A SEPARATE CHECK HERE, NOT A BIGGER RESERVE IN THE LOOP. The loop's
    # 40pt reserve is sized for item rows; a summary with a Shipping row
    # reaches 41pt below the last row's y (6pt gap, two 16pt pitches, the
    # 3pt shading underhang), so a slip whose last row only just cleared the
    # reserve printed its totals into the bottom margin. Raising the
    # reserve would fix that by pushing ITEM rows onto later pages, which
    # changes slips that print correctly today; this moves only the summary,
    # and moves it whole -- no row of it is ever split from the Total.
    if not _summary_fits(y, order):
        page += 1
        y = CONTINUED_TOP - 6
    return SlipLayout(table_top, rows, page, y)


def _draw_one_slip(c: canvas.Canvas, order, items: list) -> None:
    """Draws one order's full slip (header/address/fold/table/summary) onto
    the canvas's current page, advancing to new pages as needed for
    overflow, and ending on a fresh page so a caller drawing multiple
    orders in sequence can call this once per order with no other
    bookkeeping.

    The LINE ORDER is this function's business, not the caller's: it sorts
    ``items`` itself, so a route cannot hand in a differently-ordered list
    and get a differently-ordered slip.
    """
    # ★ SORTED HERE, IN THE ONE SHARED RENDERER, not in either route.
    # /orders/{id}/packing-slip and the pick wave's "Print All" both come
    # through this function, so neither can print an unsorted slip and the
    # two cannot drift apart.
    items = sort_slip_items(items)
    layout = layout_slip(order, items)

    def marker(page: int) -> None:
        # A one-page slip gets no marker at all, so it stays op-for-op
        # what it was before the marker existed.
        if layout.pages > 1:
            _draw_page_marker(c, page, layout.pages)

    _draw_header(c, order, multistamp=needs_multistamp(order, items))
    _draw_address(c, order)
    _draw_fold_line(c)
    marker(1)

    _draw_table_header(c, layout.table_top)

    subtotal_cents = 0
    item_count = 0
    unpriced_lines = 0
    page = 1

    for item, (row_page, y) in zip(items, layout.rows):
        if row_page != page:
            page = row_page
            _draw_table_header(c, _start_continued_page(c, order))
            marker(page)

        _draw_item_row(c, y, item)
        line_total = _line_total_cents(item)
        if line_total is None:
            unpriced_lines += 1
        else:
            subtotal_cents += line_total
        item_count += item.quantity

    if layout.summary_page != page:
        # No table header: no item rows follow on this page.
        _start_continued_page(c, order)
        marker(layout.summary_page)
    _draw_summary_rows(c, layout.summary_y, order, subtotal_cents, item_count, unpriced_lines)
    c.showPage()


# --- Quarter-size slips for tracked orders -----------------------------------
#
# Operator, 2026-10-28 and 2026-10-30: a tracked (ground_advantage) order
# ships in a bubble mailer under a printed label, so its slip has to fit the
# mailer -- a quarter-letter panel, up to four to a sheet, with cut lines.
# The label carries the address, so a panel has no address block and no
# fold. Operator 2026-11-03: an order too long for one panel CONTINUES onto
# the next panel; it is never shrunk and never falls back to a full page.
#
# ★ A SEPARATE LAYOUT AND RENDERER, BESIDE THE FULL-SIZE ONES, NOT A MODE
# THREADED THROUGH THEM. The full slip's geometry is envelope-critical and
# pinned op-for-op by the tests; a panel flag inside _draw_one_slip would
# put every untracked slip at risk for a feature that never touches one.
# What IS shared is every helper that carries a RULE -- sort_slip_items,
# card_cell_lines (wrapping and the colour marker), _finish_label,
# _is_non_normal_finish, _money, _line_total_cents -- so a panel and a full
# slip can disagree about where a line sits, never about what it says.

PANEL_W = PAGE_W / 2               # 4.25in
PANEL_H = PAGE_H / 2               # 5.5in
PANELS_PER_SHEET = 4
# Each panel's bottom-left corner, in reading order: top-left, top-right,
# bottom-left, bottom-right. The panel is drawn in its own coordinates
# after a translate to this origin, so nothing below knows which quarter
# of the sheet it is on.
PANEL_ORIGINS = [(0, PANEL_H), (PANEL_W, PANEL_H), (0, 0), (PANEL_W, 0)]

# 0.25in of clear paper inside every panel edge. The ticket's floor is
# 0.2in; the extra is for a cut that wanders off the dashed line, and on
# the sheet's outer edges it is about the printer's own unprintable border.
PANEL_PAD = 0.25 * inch
PANEL_LEFT = PANEL_PAD
PANEL_RIGHT = PANEL_W - PANEL_PAD

PANEL_TITLE_Y = PANEL_H - PANEL_PAD - 10   # an 11pt cap height under the pad
PANEL_ORDER_Y = PANEL_TITLE_Y - 14
PANEL_DATE_Y = PANEL_ORDER_Y - 12
PANEL_TABLE_HEADER_Y = PANEL_DATE_Y - 18
PANEL_ROWS_TOP = PANEL_TABLE_HEADER_Y - 14
# The lowest baseline any row may use. A 7pt descender reaches about 1.5pt
# under its baseline, so this keeps the whole pad clear of ink.
PANEL_ROWS_FLOOR = PANEL_PAD + 4

# The full slip's eight columns need 7.7in; a panel has 3.75in. Squeezing
# all eight into one row would leave the Card cell under 1.4in and wrap
# most names, so each row is Qty | Card | Price on its first line and the
# other five values on a 7pt detail line under the name.
PANEL_QTY_WIDTH = 20
PANEL_PRICE_WIDTH = 44
PANEL_CARD_X = PANEL_LEFT + PANEL_QTY_WIDTH
PANEL_CARD_WIDTH = PANEL_RIGHT - PANEL_PRICE_WIDTH - PANEL_CARD_X
PANEL_DETAIL_FONT_SIZE = 7         # the operator's floor for item text
PANEL_DETAIL_LINE_HEIGHT = 8
PANEL_DETAIL_GAP = 7               # between two values on a detail line
PANEL_ROW_GAP = 12                 # a row's lowest baseline to the next row
PANEL_SUMMARY_GAP = 14             # the last row's lowest baseline to the summary
PANEL_SUMMARY_PITCH = 13
PANEL_TOTAL_SHADE_HEIGHT = 13

CUT_LINE_WIDTH = 0.5
CUT_LINE_DASH = (4, 3)


def _draw_cut_lines(c: canvas.Canvas) -> None:
    """The two dashed centre lines a quarter sheet is cut along.

    Edge to edge rather than stopping at the pads: the operator lines a
    guillotine up on them, and a line that stops short is harder to sight.
    Solid black, so it survives a light print.
    """
    c.saveState()
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(CUT_LINE_WIDTH)
    c.setDash(*CUT_LINE_DASH)
    c.line(PANEL_W, 0, PANEL_W, PAGE_H)
    c.line(0, PANEL_H, PAGE_W, PANEL_H)
    c.restoreState()


class QuarterSheets:
    """Hands out quarter panels in reading order, across as many sheets as
    the orders need.

    ★ THE PAGE IS TURNED LAZILY -- when a fifth panel is asked for, not when
    the fourth is used -- so a sheet that ends exactly full does not leave a
    blank page behind it. close() turns the page on a part-used sheet; its
    unused panels stay blank.
    """

    def __init__(self, c: canvas.Canvas):
        self.c = c
        self.used = 0

    def next_origin(self) -> tuple[float, float]:
        if self.used == PANELS_PER_SHEET:
            self.c.showPage()
            self.used = 0
        if self.used == 0:
            _draw_cut_lines(self.c)
        origin = PANEL_ORIGINS[self.used]
        self.used += 1
        return origin

    def close(self) -> None:
        if self.used:
            self.c.showPage()
            self.used = 0


def _break_to_width(text: str, font: str, size: float, width: float) -> list[str]:
    """``text`` as one piece if it fits, else broken by character.

    _hard_break does the same for the Card cell but is fixed to the card
    font; a detail value is never close to this wide in real data (set
    codes and collector numbers are a few characters), so this is the same
    nothing-is-cut guarantee rather than a path anyone should see.
    """
    if stringWidth(text, font, size) <= width:
        return [text]
    pieces, current = [], ""
    for char in text:
        if current and stringWidth(current + char, font, size) > width:
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    return pieces


def panel_card_lines(item) -> list[str]:
    """The Card cell's lines on a panel: the full slip's wrapper and marker
    rule, at the panel's narrower width."""
    return card_cell_lines(
        item.name,
        _color_suffix(item.color, getattr(item, "type_line", None)),
        PANEL_CARD_WIDTH,
    )


def _panel_detail_values(item) -> list[tuple[str, str]]:
    """(text, font) for the five values that sit under the name, in the
    full slip's column order: Set, Cond, Finish, Lang, #. A non-normal
    finish is bold, as it is on the full slip, so a foil still stands out
    to the picker. Empty values are left out rather than spaced for."""
    values = [
        (item.set_code or "", "Helvetica"),
        (item.condition_id or "", "Helvetica"),
        (_finish_label(item.finish),
         "Helvetica-Bold" if _is_non_normal_finish(item.finish) else "Helvetica"),
        (item.language_id or "", "Helvetica"),
        (f"#{item.collector_number}" if item.collector_number else "", "Helvetica"),
    ]
    return [(text, font) for text, font in values if text]


def panel_detail_lines(item) -> list[list[tuple[float, str, str]]]:
    """The detail values laid out as lines of (x offset, text, font).

    A value that would run past the Card cell's width starts a new line
    instead; one line is the normal case by a wide margin."""
    lines, current, x = [], [], 0.0
    for text, font in _panel_detail_values(item):
        for piece in _break_to_width(text, font, PANEL_DETAIL_FONT_SIZE, PANEL_CARD_WIDTH):
            width = stringWidth(piece, font, PANEL_DETAIL_FONT_SIZE)
            if current and x + width > PANEL_CARD_WIDTH:
                lines.append(current)
                current, x = [], 0.0
            current.append((x, piece, font))
            x += width + PANEL_DETAIL_GAP
    if current:
        lines.append(current)
    return lines


def panel_row_depth(item) -> float:
    """From a panel row's first baseline down to its lowest one."""
    return (
        (len(panel_card_lines(item)) - 1) * WRAPPED_LINE_HEIGHT
        + len(panel_detail_lines(item)) * PANEL_DETAIL_LINE_HEIGHT
    )


def panel_summary_depth(order) -> float:
    """From the summary's first baseline down to the Total shading's
    bottom. Same row test (`shipping_cents is not None`) the drawing uses."""
    rows = 3 if order.shipping_cents is not None else 2
    return PANEL_SUMMARY_PITCH * (rows - 1) + TOTAL_SHADE_UNDERHANG


class QuarterLayout:
    """Where one tracked order's rows and summary land: panels are counted
    from 1 within the order, never across a sheet or a bulk PDF."""

    def __init__(self, rows: list, summary_panel: int, summary_y: float):
        self.rows = rows                # (panel, baseline y) per sorted item
        self.summary_panel = summary_panel
        self.summary_y = summary_y
        self.panels = summary_panel     # the summary is always the last thing


def layout_quarter_slip(order, items: list) -> QuarterLayout:
    """Every panel-break decision for one tracked order, made without
    drawing anything. ``items`` must already be in slip order.

    ★ THE ONLY PLACE PANEL BREAKS ARE DECIDED, for the reason layout_slip
    is the only place page breaks are: "Page X of Y" needs Y on panel 1,
    and a second loop that re-ran this arithmetic just to count would be
    a copy of the rule waiting to drift. _draw_quarter_slip draws each row
    and the summary where this says, and the marker reads Y from it.

    Like the full slip, a row is measured BEFORE it is placed, so a
    wrapped row is never split across two panels, and the summary moves
    to the next panel whole rather than being split from its Total.
    """
    panel = 1
    y = PANEL_ROWS_TOP
    bottom = None   # lowest baseline used on the current panel, if any row
    rows = []
    for item in items:
        depth = panel_row_depth(item)
        # `bottom is not None`: a row is never moved off an EMPTY panel,
        # which could only produce a blank panel and the same overflow on
        # the next one. No real row comes near a panel's height.
        if bottom is not None and y - depth < PANEL_ROWS_FLOOR:
            panel += 1
            y = PANEL_ROWS_TOP
        rows.append((panel, y))
        bottom = y - depth
        y = bottom - PANEL_ROW_GAP

    summary_y = PANEL_ROWS_TOP if bottom is None else bottom - PANEL_SUMMARY_GAP
    if summary_y - panel_summary_depth(order) < PANEL_PAD:
        panel += 1
        summary_y = PANEL_ROWS_TOP
    return QuarterLayout(rows, panel, summary_y)


def _draw_panel_header(c: canvas.Canvas, order, panel: int, panels: int) -> None:
    """The same three header lines on every panel, so a panel cut from its
    sheet still says whose it is; a continuation says so. No logo: the
    portrait mark needs about 1.7in to read, which is a third of a panel's
    height, and the ticket left it optional."""
    c.setFont("Helvetica-Bold", 11)
    c.drawString(PANEL_LEFT, PANEL_TITLE_Y, "Packing Slip")
    if panels > 1:
        # Same size as the full slip's marker. Top right of the panel
        # rather than the bottom, where the summary's Total shading can
        # sit right on the pad.
        c.setFont(*PAGE_MARKER_FONT)
        c.drawRightString(PANEL_RIGHT, PANEL_TITLE_Y, f"Page {panel} of {panels}")
    label = order.external_label or order.external_order_id
    heading = f"Order #{label}" if panel == 1 else f"Order #{label} (continued)"
    c.setFont("Helvetica", 9)
    c.drawString(PANEL_LEFT, PANEL_ORDER_Y, heading)
    order_date = order.created_at.strftime("%-m/%-d/%Y") if order.created_at else ""
    c.drawString(PANEL_LEFT, PANEL_DATE_Y, f"Order Date: {order_date}")


def _draw_panel_table_header(c: canvas.Canvas) -> None:
    y = PANEL_TABLE_HEADER_Y
    c.setFont("Helvetica-Bold", ITEM_FONT_SIZE)
    c.drawString(PANEL_LEFT, y, "Qty")
    c.drawString(PANEL_CARD_X, y, "Card")
    c.drawRightString(PANEL_RIGHT, y, "Price")
    c.saveState()
    c.setStrokeColorRGB(0.2, 0.2, 0.2)
    c.line(PANEL_LEFT, y - 3, PANEL_RIGHT, y - 3)
    c.restoreState()


def _draw_panel_row(c: canvas.Canvas, y: float, item) -> None:
    """Qty and Price on the row's first line, as on the full slip; the name
    wraps downward from there and the detail line follows the name."""
    c.setFont("Helvetica", ITEM_FONT_SIZE)
    c.drawString(PANEL_LEFT, y, str(item.quantity))
    c.drawRightString(PANEL_RIGHT, y, _money(_line_total_cents(item)))

    c.setFont(CARD_FONT, ITEM_FONT_SIZE)
    line_y = y
    for index, line in enumerate(panel_card_lines(item)):
        if index:
            line_y -= WRAPPED_LINE_HEIGHT
        c.drawString(PANEL_CARD_X, line_y, line)
    for detail in panel_detail_lines(item):
        line_y -= PANEL_DETAIL_LINE_HEIGHT
        for x, text, font in detail:
            c.setFont(font, PANEL_DETAIL_FONT_SIZE)
            c.drawString(PANEL_CARD_X + x, line_y, text)


def _draw_panel_summary(
    c: canvas.Canvas, y: float, order, subtotal_cents: int, item_count: int,
    unpriced_lines: int = 0,
) -> None:
    """The full slip's summary rows, at panel width.

    ★ THE WORDING IS A COPY OF _draw_summary_rows', NOT A CALL TO IT. That
    function draws at the full page's margins and its body is pinned
    unchanged, so the label rules could not be pulled out of it without
    editing it. test_packing_slip_service renders the same order both ways
    and asserts the summary strings are identical, so the copy cannot drift
    silently.
    """
    item_word = "item" if item_count == 1 else "items"
    subtotal_label = f"{item_count} {item_word}"
    if unpriced_lines:
        line_word = "line" if unpriced_lines == 1 else "lines"
        subtotal_label += f" ({unpriced_lines} {line_word} without a price, excluded)"
    rows = [(subtotal_label, subtotal_cents, False)]
    total_cents = subtotal_cents
    if order.shipping_cents is not None:
        rows.append(("Shipping", order.shipping_cents, False))
        total_cents += order.shipping_cents
    rows.append(("Total", None if unpriced_lines else total_cents, True))

    for label, amount_cents, is_total in rows:
        if is_total:
            c.saveState()
            c.setFillColorRGB(0.93, 0.93, 0.93)
            c.rect(
                PANEL_LEFT, y - TOTAL_SHADE_UNDERHANG, PANEL_RIGHT - PANEL_LEFT,
                PANEL_TOTAL_SHADE_HEIGHT, stroke=0, fill=1,
            )
            c.restoreState()
        c.setFont("Helvetica-Bold" if is_total else "Helvetica", 9)
        c.drawString(PANEL_LEFT, y, label)
        c.drawRightString(PANEL_RIGHT, y, _money(amount_cents))
        y -= PANEL_SUMMARY_PITCH


def _draw_quarter_slip(sheets: QuarterSheets, order, items: list) -> None:
    """Draws one tracked order as one or more quarter panels, taking the
    next free panel from ``sheets`` for each. Two orders never share a
    panel: every order starts on a panel of its own.

    Sorts ``items`` itself, for the same reason _draw_one_slip does.
    """
    c = sheets.c
    items = sort_slip_items(items)
    layout = layout_quarter_slip(order, items)

    subtotal_cents = 0
    item_count = 0
    unpriced_lines = 0
    for item in items:
        line_total = _line_total_cents(item)
        if line_total is None:
            unpriced_lines += 1
        else:
            subtotal_cents += line_total
        item_count += item.quantity

    for panel in range(1, layout.panels + 1):
        origin_x, origin_y = sheets.next_origin()
        # Every state change for the panel -- origin, fonts, the explicit
        # black -- is undone before the next panel, so no panel inherits
        # anything from the one before it (the v2.69.1 grey-page lesson).
        c.saveState()
        c.translate(origin_x, origin_y)
        c.setFillColorRGB(0, 0, 0)
        _draw_panel_header(c, order, panel, layout.panels)
        panel_rows = [
            (item, y) for item, (row_panel, y) in zip(items, layout.rows)
            if row_panel == panel
        ]
        if panel_rows:
            _draw_panel_table_header(c)
        for item, y in panel_rows:
            _draw_panel_row(c, y, item)
        if panel == layout.summary_panel:
            _draw_panel_summary(
                c, layout.summary_y, order, subtotal_cents, item_count, unpriced_lines,
            )
        c.restoreState()


def generate_packing_slip_pdf(order, items: list) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=LETTER)
    if is_tracked(order):
        sheets = QuarterSheets(c)
        _draw_quarter_slip(sheets, order, items)
        sheets.close()
    else:
        _draw_one_slip(c, order, items)
    c.save()
    return buffer.getvalue()


def print_order_key(order, items: list) -> tuple:
    """Where one order's slip falls in a Print All PDF (operator,
    2026-10-28): tracked orders first, then untracked orders that need a
    multistamp, then everything else -- the special ones are packed first.
    Inside each group, the order with the most cards first.

    The group test is needs_multistamp itself, so an order printed in the
    multistamp group is always one whose slip carries the tag.
    """
    if is_tracked(order):
        group = 0
    elif needs_multistamp(order, items):
        group = 1
    else:
        group = 2
    return (group, -total_quantity(items))


def sort_print_orders(orders_with_items: list) -> list:
    """Print All's order sequence. A NEW list, like sort_slip_items.

    Stable, so orders that tie on group and card count keep the caller's
    order -- for the pick wave, the order get_wave_orders returned them in.
    """
    return sorted(orders_with_items, key=lambda entry: print_order_key(*entry))


def generate_bulk_packing_slip_pdf(orders_with_items: list) -> bytes:
    """One combined PDF, one order's slip per page-set, in print order.

    ``orders_with_items`` is a list of ``(order, items)`` tuples. Skips
    silently over any entry with zero items rather than drawing an empty
    slip -- an order with no items shouldn't happen in practice, but this
    keeps a batch print from producing a broken-looking page if it does.
    """
    # ★ SORTED HERE, NOT IN THE ROUTE, for the same reason the line order
    # lives in _draw_one_slip: this is the one place every Print All goes
    # through, so no caller can print the wave in a different sequence.
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=LETTER)
    sheets = QuarterSheets(c)
    for order, items in sort_print_orders(orders_with_items):
        if not items:
            continue
        if is_tracked(order):
            _draw_quarter_slip(sheets, order, items)
        else:
            # A full-size slip never shares a sheet with panels: the open
            # quarter sheet is finished first, blank panels and all. With
            # tracked orders sorted first this happens once, before the
            # first untracked slip; with none it is a no-op, so a wave with
            # no tracked orders prints exactly what it did before.
            sheets.close()
            _draw_one_slip(c, order, items)
    sheets.close()
    c.save()
    return buffer.getvalue()
