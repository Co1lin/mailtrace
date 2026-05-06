"""PDF rendering for label sheets, single-piece labels, and envelopes.

Uses reportlab + the `labels` library directly — no HTML-to-PDF conversion
layer. Produces precise positioning to within fractional-millimeter tolerance
of Avery's published spec, and drops the wkhtmltopdf + Qt + xfonts stack
entirely.

The labels library handles physical cell positioning on Avery sheets (a
solved problem with ~10 years of production use); this module just provides
the per-label drawing function and the envelope-specific layout.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import labels
from reportlab.graphics import shapes
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"

# ---------------------------------------------------------------------------
# Font registration (one-time at import)
# ---------------------------------------------------------------------------

IMB_FONT = "USPSIMBStandard"
# Default to ReportLab's built-in Times-Roman if DejaVu isn't on the box.
ADDRESS_FONT = "Times-Roman"

pdfmetrics.registerFont(TTFont(IMB_FONT, str(STATIC_DIR / "USPSIMBStandard.ttf")))

_DEJAVU_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/Library/Fonts/DejaVuSerif.ttf",
    "/System/Library/Fonts/Supplemental/DejaVuSerif.ttf",
)
for _path in _DEJAVU_CANDIDATES:
    if Path(_path).is_file():
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSerif", _path))
            ADDRESS_FONT = "DejaVuSerif"
        except Exception:  # pragma: no cover - font load failure is environment-dependent
            pass
        break


# ---------------------------------------------------------------------------
# Per-Avery-model content sizing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelStyle:
    """Per-Avery-model font sizing. Smaller-height labels get smaller
    fonts (and the 1"-tall 5161 drops the human-readable IMb line entirely).
    """

    imb_pt: int
    human_readable_pt: int  # 0 = hide
    address_pt: float
    address_line_pt: float  # line height (typically ~1.15 * address_pt)
    padding_in: float


# IMb size MUST satisfy USPS-STD-39: 65 bars at 20-24 bars/inch, total
# outside-to-outside length 2.683"-3.225". Empirically with this TTF,
# imb_pt * 0.179" = rendered length:
#   17pt → 3.04"  (comfortable middle of spec)
#   16pt → 2.87"  (within spec, lower edge)
#   14pt → 2.51"  (BELOW MIN — sub-spec, AFCS scanners may reject)
# 17pt across all three so the IMb is always scannable; the 5161 (1"
# tall) needs the address font shrunk to make room.
_STYLES: dict[str, LabelStyle] = {
    "5163": LabelStyle(
        imb_pt=17, human_readable_pt=9, address_pt=11, address_line_pt=13, padding_in=0.1
    ),
    "5162": LabelStyle(
        imb_pt=17, human_readable_pt=8, address_pt=10, address_line_pt=11.5, padding_in=0.08
    ),
    "5161": LabelStyle(
        imb_pt=17, human_readable_pt=0, address_pt=8, address_line_pt=9, padding_in=0.05
    ),
}
_DEFAULT_STYLE = _STYLES["5163"]

# 1 pt = 1/72 inch (ReportLab's native unit; the labels library passes
# width/height to the draw callback in pts, NOT mm — even though the
# Specification was given in mm. Internally everything routes through
# ReportLab points.)
_PT_PER_IN = 72.0
_MM_PER_IN = 25.4


# ---------------------------------------------------------------------------
# Public protocol — the subset of MailPiece this module reads
# ---------------------------------------------------------------------------


class _Piece(Protocol):
    imb_letters: str
    recipient_block: str
    sender_block: str

    def human_readable_imb(self) -> str: ...


# ---------------------------------------------------------------------------
# Sheet rendering
# ---------------------------------------------------------------------------


_ALIGNMENTS = ("left", "center")


def _normalize_alignment(alignment: str) -> str:
    if alignment not in _ALIGNMENTS:
        return "left"
    return alignment


def _spec_for_layout(layout: dict[str, Any]) -> labels.Specification:
    """Convert a mailtrace AVERY_LAYOUTS entry (in inches) into the
    labels library's Specification (in mm)."""
    return labels.Specification(
        sheet_width=8.5 * 25.4,
        sheet_height=11.0 * 25.4,
        columns=layout["cols"],
        rows=layout["rows"],
        label_width=layout["label_width_in"] * 25.4,
        label_height=layout["label_height_in"] * 25.4,
        left_margin=layout["left_margin_in"] * 25.4,
        top_margin=layout["top_margin_in"] * 25.4,
        column_gap=(layout["col_pitch_in"] - layout["label_width_in"]) * 25.4,
        row_gap=(layout["row_pitch_in"] - layout["label_height_in"]) * 25.4,
    )


def render_label_sheet(
    *,
    layout: dict[str, Any],
    pieces: Sequence[_Piece],
    start_row: int = 1,
    start_col: int = 1,
    block_align: str = "left",
    text_align: str = "left",
) -> bytes:
    """Render `pieces` onto an Avery sheet matching `layout`.

    Cells before (start_row, start_col) on page 1 are marked as
    already-used and skipped. Page 2+ always start at (1, 1).

    `block_align`: where the content block sits horizontally on each
        label. "left" anchors the block at the left padding; "center"
        anchors the block such that the widest line is centered on
        the label's horizontal midpoint.

    `text_align`: how individual lines are justified within the block.
        "left" left-aligns each line to a common left edge; "center"
        centers each line independently. Independent of block_align —
        e.g. block=center + text=left gives a centered address block
        with left-justified lines (the natural address layout).
    """
    block = _normalize_alignment(block_align)
    text = _normalize_alignment(text_align)
    style = _STYLES.get(layout["model"], _DEFAULT_STYLE)
    spec = _spec_for_layout(layout)

    def draw_one(label: shapes.Group, width_pt: float, height_pt: float, piece: _Piece) -> None:
        _draw_piece(label, width_pt, height_pt, piece, style, block, text)

    sheet = labels.Sheet(spec, draw_one, border=False)

    start_idx = (start_row - 1) * layout["cols"] + (start_col - 1)
    if start_idx > 0:
        used = [
            (r, c)
            for r in range(1, layout["rows"] + 1)
            for c in range(1, layout["cols"] + 1)
            if (r - 1) * layout["cols"] + (c - 1) < start_idx
        ]
        sheet.partial_page(1, used)

    for piece in pieces:
        sheet.add_label(piece)

    out = io.BytesIO()
    sheet.save(out)
    return out.getvalue()


def render_single_label(
    *,
    layout: dict[str, Any],
    piece: _Piece,
    row: int = 1,
    col: int = 1,
    block_align: str = "left",
    text_align: str = "left",
) -> bytes:
    """Render one piece into a single cell of an otherwise-empty sheet."""
    return render_label_sheet(
        layout=layout,
        pieces=[piece],
        start_row=row,
        start_col=col,
        block_align=block_align,
        text_align=text_align,
    )


def _draw_piece(
    label: shapes.Group,
    width_pt: float,
    height_pt: float,
    piece: _Piece,
    style: LabelStyle,
    block_align: str,
    text_align: str,
) -> None:
    """Draw IMb + human-readable + recipient block on one label cell.

    Two independent axes:
      block_align: where the bounding box of all content sits horizontally
          on the label (left or center).
      text_align: how each line is justified within the block (left or
          center). Independent of block_align — e.g. block=center +
          text=left renders a centered block of left-justified address
          lines, the natural form for a mailing label.

    Uses real font metrics (ascent/descent from the TTF). The IMb font
    is special: at 16pt it has ascent=+8pt and descent=-8pt (the bars
    span the full em-square), so a naive 1.0x line height stacks the
    next line on top of the bars.

    NB: width_pt and height_pt are REPORTLAB POINTS (1/72 in), even
    though we declared the labels.Specification in mm — the labels lib
    converts to pts before invoking this callback.
    """
    pad_pt = style.padding_in * _PT_PER_IN

    # Pre-compute every line we'll draw with its font+size, so we can
    # find the widest line (= block bounding-box width) before rendering.
    address_lines = [ln for ln in piece.recipient_block.split("\n") if ln.strip()]
    rendered_lines: list[tuple[str, str, float]] = [
        (piece.imb_letters, IMB_FONT, float(style.imb_pt)),
    ]
    if style.human_readable_pt > 0:
        rendered_lines.append(
            (piece.human_readable_imb(), "Helvetica", float(style.human_readable_pt))
        )
    for ln in address_lines:
        rendered_lines.append((ln, ADDRESS_FONT, float(style.address_pt)))
    line_widths = [pdfmetrics.stringWidth(t, f, p) for (t, f, p) in rendered_lines]
    block_width = max(line_widths) if line_widths else 0.0

    # Block left edge: where the bounding box starts on the label.
    if block_align == "center":
        block_left = (width_pt - block_width) / 2
    else:
        block_left = pad_pt

    # Per-line anchor: text_align decides whether each line grows from
    # the block's left edge (textAnchor="start") or centers itself
    # around the block's horizontal midpoint (textAnchor="middle").
    if text_align == "center":
        x_anchor = block_left + block_width / 2
        text_anchor_attr = "middle"
    else:
        x_anchor = block_left
        text_anchor_attr = "start"

    # Cursor: y-coordinate (pts) of the TOP edge of the next text line.
    cur_top = [height_pt - pad_pt]  # boxed for closure

    def draw_line(text: str, font: str, pt: float, gap_after_pt: float = 1.5) -> None:
        ascent = pdfmetrics.getAscent(font, pt)  # pts, positive
        descent = -pdfmetrics.getDescent(font, pt)  # pts, positive (|descent|)
        baseline = cur_top[0] - ascent
        label.add(
            shapes.String(
                x_anchor,
                baseline,
                text,
                fontName=font,
                fontSize=pt,
                textAnchor=text_anchor_attr,
            )
        )
        cur_top[0] -= ascent + descent + gap_after_pt

    # IMb barcode (extra gap after — it has tall descender bars).
    draw_line(piece.imb_letters, IMB_FONT, float(style.imb_pt), gap_after_pt=2.0)

    if style.human_readable_pt > 0:
        draw_line(
            piece.human_readable_imb(),
            "Helvetica",
            float(style.human_readable_pt),
            gap_after_pt=3.0,  # section break before address block
        )
    else:
        cur_top[0] -= 3.0  # pts of breathing room before address when no h/r

    for line in address_lines:
        draw_line(line, ADDRESS_FONT, float(style.address_pt), gap_after_pt=1.0)


# ---------------------------------------------------------------------------
# Envelope rendering (#10 business envelope, 9.5 x 4.125 in)
# ---------------------------------------------------------------------------


def render_envelope(
    piece: _Piece,
    *,
    block_align: str = "left",
    text_align: str = "left",
) -> bytes:
    """Render a #10 business envelope: sender top-left, recipient block
    center-right, IMb directly above the recipient block.

    USPS placement: the IMb sits in the OCR clear zone above the
    delivery address line, with the address block in the lower-right
    quadrant where AFCS scanners look.

    `block_align`: position of the recipient block — "left" anchors it
        at x=4", "center" centers it within the right half of the
        envelope. `text_align`: how each line within the block is
        justified (left or center). Bumped to a USPS-compliant 17pt IMb.
    """
    block = _normalize_alignment(block_align)
    text = _normalize_alignment(text_align)

    out = io.BytesIO()
    page_w = 9.5 * inch
    page_h = 4.125 * inch
    c = canvas.Canvas(out, pagesize=(page_w, page_h))

    # Sender block: top-left.
    sender_lines = [ln for ln in (piece.sender_block or "").split("\n") if ln.strip()]
    if sender_lines:
        c.setFont(ADDRESS_FONT, 10)
        sx = 0.5 * inch
        sy = page_h - 0.5 * inch - 10
        for line in sender_lines:
            c.drawString(sx, sy, line)
            sy -= 12

    # Recipient block: width = max line width across IMb / human-readable
    # / address lines (so block_align=center can position the bounding
    # box correctly).
    addr_lines = [ln for ln in (piece.recipient_block or "").split("\n") if ln.strip()]
    imb_pt = 17  # USPS-compliant IMb size on envelope
    hr_pt = 9
    addr_pt = 12
    line_w = [
        pdfmetrics.stringWidth(piece.imb_letters, IMB_FONT, imb_pt),
        pdfmetrics.stringWidth(piece.human_readable_imb(), "Helvetica", hr_pt),
        *(pdfmetrics.stringWidth(ln, ADDRESS_FONT, addr_pt) for ln in addr_lines),
    ]
    block_width = max(line_w) if line_w else 0.0

    # Right-half region: x in [4 in, page_w - 0.25 in margin] center.
    right_zone_left = 4.0 * inch
    right_zone_right = page_w - 0.25 * inch
    if block == "center":
        block_left_x = (right_zone_left + right_zone_right - block_width) / 2
    else:
        block_left_x = right_zone_left
    line_anchor_x = block_left_x + block_width / 2 if text == "center" else block_left_x

    def draw(font: str, pt: float, text_str: str, y: float) -> None:
        c.setFont(font, pt)
        if text == "center":
            c.drawCentredString(line_anchor_x, y, text_str)
        else:
            c.drawString(line_anchor_x, y, text_str)

    cur_y = 2.6 * inch  # IMb baseline
    draw(IMB_FONT, imb_pt, piece.imb_letters, cur_y)
    cur_y -= 14
    draw("Helvetica", hr_pt, piece.human_readable_imb(), cur_y)
    cur_y -= 18
    for line in addr_lines:
        draw(ADDRESS_FONT, addr_pt, line, cur_y)
        cur_y -= 14

    c.showPage()
    c.save()
    return out.getvalue()
