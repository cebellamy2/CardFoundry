"""CF-BUY-006: the seller-facing buylist PDF -- the document Chris hands
or sends to the person who brought the cards in, deliberately much
simpler than the internal report at /admin/piles/{pile_id}: no tier
math, no percentages, no $0.00 lines, no operator-only detail. Chris's
Cards branding (this is a customer-facing document, same standing rule
packing_slip_service.py already follows for the packing slip), never
CardFoundry's own.

A new, standalone tabular layout -- packing_slip_service.py's canvas is
laid out for a #10 window envelope (a fixed fold line, a fixed address
block, everything below the fold reserved for one order's line items)
and has no notion of a running grand total or a variable-length seller
item list; adapting it would fight its own envelope-critical
measurements rather than reuse them for something they were never
shaped for.
"""

import io
import os

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from buylist_pricing_service import CONSIGNMENT_LINE_STATUSES, pile_line_final_cents

PAGE_W, PAGE_H = LETTER  # 8.5in x 11in

LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "chriss_cards_logo.png")

LEFT_MARGIN = 0.5 * inch
RIGHT_MARGIN = 0.5 * inch
USABLE_WIDTH = PAGE_W - LEFT_MARGIN - RIGHT_MARGIN
TOP_MARGIN = 0.5 * inch
BOTTOM_MARGIN = 0.6 * inch
LOGO_SIZE = 0.9 * inch

COLUMNS = [
    ("Card", 3.4 * inch, "left"),
    ("Set / #", 1.6 * inch, "left"),
    ("Condition", 1.0 * inch, "left"),
    ("Amount", 1.0 * inch, "right"),
]


def _y_from_top(distance_from_top: float) -> float:
    return PAGE_H - distance_from_top


def _line_display_name(line) -> str:
    """Card name plus a parenthetical only when the finish or language
    is worth flagging (foil/etched, or non-English) -- operator decision:
    skip a dedicated Finish/Language column for the common case, keep it
    simple."""
    tags = []
    finish = str(line.finish or "").strip().lower()
    if finish and finish != "nonfoil":
        tags.append(finish.capitalize())
    language = str(line.language or "").strip()
    if language and language.lower() not in ("en", "english"):
        tags.append(language)
    if tags:
        return f"{line.name} ({', '.join(tags)})"
    return line.name


def eligible_seller_pdf_lines(lines: list, consignment_tiers: list[dict]) -> list[tuple]:
    """Every line the seller-facing PDF should show, in the given order,
    as (line, amount_cents, is_consignment) tuples.

    Omitted entirely: kept-by-seller lines (the seller already knows
    they're keeping those -- it's not a purchase or consignment line),
    and any line whose final amount is None (never priced) or exactly
    $0.00 (the sub-$1 "freebies for scanning their cards" tier) -- the
    operator's own explicit rule for this document, distinct from the
    internal report, which shows every line including $0.00 ones.
    """
    rows = []
    for line in lines:
        if line.line_status == "kept_by_seller":
            continue
        _computed_cents, final_cents = pile_line_final_cents(line, consignment_tiers)
        if not final_cents:
            continue
        rows.append((line, final_cents, line.line_status in CONSIGNMENT_LINE_STATUSES))
    return rows


def _draw_header(c: canvas.Canvas, pile) -> float:
    top_y = _y_from_top(TOP_MARGIN)
    if os.path.exists(LOGO_PATH):
        try:
            image = ImageReader(LOGO_PATH)
            c.drawImage(
                image, LEFT_MARGIN, top_y - LOGO_SIZE,
                width=LOGO_SIZE, height=LOGO_SIZE,
                mask="auto", preserveAspectRatio=True, anchor="c",
            )
        except Exception:
            pass

    right_x = PAGE_W - RIGHT_MARGIN
    text_y = top_y - 0.28 * inch
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(right_x, text_y, "Chris's Cards")
    text_y -= 0.22 * inch
    c.setFont("Helvetica", 11)
    c.drawRightString(right_x, text_y, "Buylist Offer Summary")
    text_y -= 0.20 * inch
    date_value = pile.finalized_at or pile.created_at
    date_label = date_value.strftime("%-m/%-d/%Y") if date_value else ""
    c.setFont("Helvetica", 9)
    c.drawRightString(right_x, text_y, f"Pile: {pile.code}   |   {date_label}")

    bottom_y = top_y - LOGO_SIZE
    c.saveState()
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.line(LEFT_MARGIN, bottom_y - 0.1 * inch, PAGE_W - RIGHT_MARGIN, bottom_y - 0.1 * inch)
    c.restoreState()
    return bottom_y - 0.35 * inch


def _draw_table_header(c: canvas.Canvas, y: float) -> None:
    c.setFont("Helvetica-Bold", 9)
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


def _draw_row(c: canvas.Canvas, y: float, line, amount_cents: int, *, is_consignment: bool) -> None:
    amount_label = f"${amount_cents / 100:.2f}"
    if is_consignment:
        amount_label += " (est.)"
    values = [
        _line_display_name(line),
        f"{line.set_code or ''} #{line.collector_number or ''}".strip(),
        line.condition or "",
        amount_label,
    ]
    x = LEFT_MARGIN
    c.setFont("Helvetica", 9)
    for (label, width, align), value in zip(COLUMNS, values):
        if label == "Card":
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x, y, value)
            c.setFont("Helvetica", 9)
        elif align == "right":
            c.drawRightString(x + width, y, value)
        else:
            c.drawString(x, y, value)
        x += width


def _draw_totals(c: canvas.Canvas, y: float, buy_total_cents: int, consignment_total_cents: int) -> float:
    label_x = LEFT_MARGIN
    value_x = PAGE_W - RIGHT_MARGIN

    def row(label: str, amount_cents: int) -> float:
        nonlocal y
        c.saveState()
        c.setFillColorRGB(0.93, 0.93, 0.93)
        c.rect(LEFT_MARGIN, y - 3, USABLE_WIDTH, 15, stroke=0, fill=1)
        c.restoreState()
        c.setFont("Helvetica-Bold", 10)
        c.drawString(label_x, y, label)
        c.drawRightString(value_x, y, f"${amount_cents / 100:.2f}")
        y -= 18
        return y

    y = row("Buy Total", buy_total_cents)
    if consignment_total_cents:
        y = row("Estimated Consignment Payout", consignment_total_cents)
        c.setFont("Helvetica-Oblique", 7)
        c.drawString(
            label_x, y,
            "Consignment payout is an estimate -- the final amount is set when the card actually sells.",
        )
        y -= 12
    return y


def generate_buylist_seller_pdf(pile, lines: list, consignment_tiers: list[dict]) -> bytes:
    rows = eligible_seller_pdf_lines(lines, consignment_tiers)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=LETTER)
    y = _draw_header(c, pile)
    _draw_table_header(c, y)
    y -= 16

    buy_total_cents = 0
    consignment_total_cents = 0

    for line, amount_cents, is_consignment in rows:
        if y < BOTTOM_MARGIN + 60:
            c.showPage()
            y = PAGE_H - TOP_MARGIN
            _draw_table_header(c, y)
            y -= 16
        _draw_row(c, y, line, amount_cents, is_consignment=is_consignment)
        if is_consignment:
            consignment_total_cents += amount_cents
        else:
            buy_total_cents += amount_cents
        y -= 14

    y -= 10
    if y < BOTTOM_MARGIN + 45:
        c.showPage()
        y = PAGE_H - TOP_MARGIN
    _draw_totals(c, y, buy_total_cents, consignment_total_cents)
    c.showPage()
    c.save()
    return buffer.getvalue()
