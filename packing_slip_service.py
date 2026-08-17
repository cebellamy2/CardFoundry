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

import io
import os

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

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

COLUMNS = [
    ("Qty", 0.4 * inch, "left"),
    ("Card", 2.3 * inch, "left"),
    ("Set", 1.6 * inch, "left"),
    ("Cond", 0.5 * inch, "left"),
    ("Finish", 0.7 * inch, "left"),
    ("Lang", 0.5 * inch, "left"),
    ("#", 0.6 * inch, "left"),
    ("Price", 1.1 * inch, "right"),
]


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
        except Exception:
            pass

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


def _line_total_cents(item) -> int:
    if item.price_cents is None:
        return 0
    return item.price_cents * item.quantity


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


def _draw_item_row(c: canvas.Canvas, y: float, item) -> None:
    values = [
        str(item.quantity),
        item.name,
        item.set_code or "",
        (item.condition_id or ""),
        _finish_label(item.finish),
        (item.language_id or ""),
        item.collector_number or "",
        f"${_line_total_cents(item) / 100:.2f}",
    ]
    non_normal = _is_non_normal_finish(item.finish)

    x = LEFT_MARGIN
    c.setFont("Helvetica", 8)
    for (label, width, align), value in zip(COLUMNS, values):
        if label == "Card":
            c.setFont("Helvetica-Bold", 8)
            c.drawString(x, y, value)
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


def _draw_summary_rows(c: canvas.Canvas, y: float, order, subtotal_cents: int, item_count: int) -> float:
    label_x = LEFT_MARGIN
    value_x = PAGE_W - RIGHT_MARGIN

    def row(label: str, amount_cents: int, *, bold: bool = False, shaded: bool = False) -> float:
        nonlocal y
        if shaded:
            c.saveState()
            c.setFillColorRGB(0.93, 0.93, 0.93)
            c.rect(LEFT_MARGIN, y - 3, USABLE_WIDTH, 14, stroke=0, fill=1)
            c.restoreState()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(label_x, y, label)
        c.drawRightString(value_x, y, f"${amount_cents / 100:.2f}")
        y -= 16
        return y

    item_word = "item" if item_count == 1 else "items"
    y = row(f"{item_count} {item_word}", subtotal_cents)

    total_cents = subtotal_cents
    if order.shipping_cents is not None:
        y = row("Shipping", order.shipping_cents)
        total_cents += order.shipping_cents

    y = row("Total", total_cents, bold=True, shaded=True)
    return y


def generate_packing_slip_pdf(order, items: list) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=LETTER)

    _draw_header(c, order)
    _draw_address(c, order)
    _draw_fold_line(c)

    y = _y_from_top(FOLD_FROM_TOP + TABLE_TOP_PAD)
    _draw_table_header(c, y)
    y -= 14

    subtotal_cents = 0
    item_count = 0

    for item in items:
        if y < BOTTOM_MARGIN + 40:
            c.showPage()
            c.setFont("Helvetica", 8)
            label = order.external_label or order.external_order_id
            c.drawString(LEFT_MARGIN, PAGE_H - 0.5 * inch, f"Order #{label} (continued)")
            y = PAGE_H - 0.8 * inch
            _draw_table_header(c, y)
            y -= 14

        _draw_item_row(c, y, item)
        subtotal_cents += _line_total_cents(item)
        item_count += item.quantity
        y -= 14

    y -= 6
    _draw_summary_rows(c, y, order, subtotal_cents, item_count)

    c.showPage()
    c.save()
    return buffer.getvalue()
