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


def _y_from_top(distance_from_top: float) -> float:
    return PAGE_H - distance_from_top


def _draw_fold_line(c: canvas.Canvas) -> None:
    y = _y_from_top(FOLD_FROM_TOP)
    c.saveState()
    c.setDash(3, 3)
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.line(LEFT_MARGIN, y, PAGE_W - RIGHT_MARGIN, y)
    c.restoreState()
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawCentredString(PAGE_W / 2, y + 2, "Fold")


def _draw_header(c: canvas.Canvas, order) -> None:
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
            c.rect(LEFT_MARGIN, y - 3, USABLE_WIDTH, 14, stroke=0, fill=1)
            c.restoreState()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(label_x, y, label)
        c.drawRightString(value_x, y, _money(amount_cents))
        y -= 16
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

    _draw_header(c, order)
    _draw_address(c, order)
    _draw_fold_line(c)

    y = _y_from_top(FOLD_FROM_TOP + TABLE_TOP_PAD)
    _draw_table_header(c, y)
    y -= ITEM_ROW_HEIGHT

    subtotal_cents = 0
    item_count = 0
    unpriced_lines = 0

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
            c.showPage()
            c.setFont("Helvetica", 8)
            label = order.external_label or order.external_order_id
            c.drawString(LEFT_MARGIN, PAGE_H - 0.5 * inch, f"Order #{label} (continued)")
            y = PAGE_H - 0.8 * inch
            _draw_table_header(c, y)
            y -= ITEM_ROW_HEIGHT

        y -= _draw_item_row(c, y, item) - ITEM_ROW_HEIGHT
        line_total = _line_total_cents(item)
        if line_total is None:
            unpriced_lines += 1
        else:
            subtotal_cents += line_total
        item_count += item.quantity
        y -= ITEM_ROW_HEIGHT

    y -= 6
    _draw_summary_rows(c, y, order, subtotal_cents, item_count, unpriced_lines)
    c.showPage()


def generate_packing_slip_pdf(order, items: list) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=LETTER)
    _draw_one_slip(c, order, items)
    c.save()
    return buffer.getvalue()


def generate_bulk_packing_slip_pdf(orders_with_items: list) -> bytes:
    """One combined PDF, one order's slip per page-set, in the given order.

    ``orders_with_items`` is a list of ``(order, items)`` tuples. Skips
    silently over any entry with zero items rather than drawing an empty
    slip -- an order with no items shouldn't happen in practice, but this
    keeps a batch print from producing a broken-looking page if it does.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=LETTER)
    for order, items in orders_with_items:
        if not items:
            continue
        _draw_one_slip(c, order, items)
    c.save()
    return buffer.getvalue()
