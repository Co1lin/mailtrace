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


_STYLES: dict[str, LabelStyle] = {
    "5163": LabelStyle(
        imb_pt=16, human_readable_pt=9, address_pt=11, address_line_pt=13, padding_in=0.1
    ),
    "5162": LabelStyle(
        imb_pt=16, human_readable_pt=8, address_pt=10, address_line_pt=11.5, padding_in=0.08
    ),
    "5161": LabelStyle(
        imb_pt=14, human_readable_pt=0, address_pt=8.5, address_line_pt=10, padding_in=0.06
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


def _normalize_alignment(alignment: str) -> str:
    if alignment not in _ALIGNMENTS:
        return "left"
    return alignment


def render_label_sheet(
    *,
    layout: dict[str, Any],
    pieces: Sequence[_Piece],
    start_row: int = 1,
    start_col: int = 1,
    alignment: str = "left",
) -> bytes:
    """Render `pieces` onto an Avery sheet matching `layout`.

    Cells before (start_row, start_col) on page 1 are marked as
    already-used and skipped. Page 2+ always start at (1, 1).
    `alignment`: "left" (default) or "center" — horizontal alignment
    of IMb + address content within each label.
    """
    align = _normalize_alignment(alignment)
    style = _STYLES.get(layout["model"], _DEFAULT_STYLE)
    spec = _spec_for_layout(layout)

    def draw_one(label: shapes.Group, width_mm: float, height_mm: float, piece: _Piece) -> None:
        _draw_piece(label, width_mm, height_mm, piece, style, align)

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
    alignment: str = "left",
) -> bytes:
    """Render one piece into a single cell of an otherwise-empty sheet."""
    return render_label_sheet(
        layout=layout,
        pieces=[piece],
        start_row=row,
        start_col=col,
        alignment=alignment,
    )


def _draw_piece(
    label: shapes.Group,
    width_pt: float,
    height_pt: float,
    piece: _Piece,
    style: LabelStyle,
    alignment: str,
) -> None:
    """Draw IMb + human-readable + recipient block on one label cell.

    Uses real font metrics (ascent/descent from the TTF) instead of
    guessing — the IMb font is special: at 16pt it has ascent=+8pt and
    descent=-8pt (the bars span the full em-square), so a naive 1.0x
    fontSize line height stacks the next line on top of the bars.

    NB: width_pt and height_pt are in REPORTLAB POINTS (1/72 in), even
    though we declared the labels.Specification in mm — the labels lib
    converts to pts before invoking this callback.
    """
    pad_pt = style.padding_in * _PT_PER_IN

    if alignment == "center":
        x_anchor = width_pt / 2
        text_anchor = "middle"
    else:
        x_anchor = pad_pt
        text_anchor = "start"

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
                textAnchor=text_anchor,
            )
        )
        cur_top[0] -= ascent + descent + gap_after_pt

    # IMb barcode (extra gap after — it has tall descender bars).
    draw_line(piece.imb_letters, IMB_FONT, style.imb_pt, gap_after_pt=2.0)

    if style.human_readable_pt > 0:
        draw_line(
            piece.human_readable_imb(),
            "Helvetica",
            style.human_readable_pt,
            gap_after_pt=3.0,  # section break before address block
        )
    else:
        cur_top[0] -= 3.0  # pts of breathing room before address when no h/r

    for line in piece.recipient_block.split("\n"):
        if not line.strip():
            continue
        draw_line(line, ADDRESS_FONT, style.address_pt, gap_after_pt=1.0)


# ---------------------------------------------------------------------------
# Envelope rendering (#10 business envelope, 9.5 x 4.125 in)
# ---------------------------------------------------------------------------


def render_envelope(piece: _Piece, *, alignment: str = "left") -> bytes:
    """Render a #10 business envelope: sender top-left, recipient block
    center-right, IMb directly above the recipient block.

    USPS placement: the IMb sits in the OCR clear zone above the
    delivery address line, with the address block in the lower-right
    quadrant where AFCS scanners look. The address block is left-aligned
    by default (most common); pass alignment="center" to center it.
    """
    align = _normalize_alignment(alignment)
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

    # Recipient block + IMb: lower-right region. We anchor the IMb at a
    # fixed position and cascade downward.
    recip_x = 4.0 * inch
    recip_top_y = 2.6 * inch  # IMb baseline

    c.setFont(IMB_FONT, 16)
    if align == "center":
        c.drawCentredString((page_w + recip_x) / 2, recip_top_y, piece.imb_letters)
    else:
        c.drawString(recip_x, recip_top_y, piece.imb_letters)

    cur_y = recip_top_y - 14
    c.setFont("Helvetica", 9)
    if align == "center":
        c.drawCentredString((page_w + recip_x) / 2, cur_y, piece.human_readable_imb())
    else:
        c.drawString(recip_x, cur_y, piece.human_readable_imb())
    cur_y -= 18

    c.setFont(ADDRESS_FONT, 12)
    for line in (piece.recipient_block or "").split("\n"):
        if not line.strip():
            continue
        if align == "center":
            c.drawCentredString((page_w + recip_x) / 2, cur_y, line)
        else:
            c.drawString(recip_x, cur_y, line)
        cur_y -= 14

    c.showPage()
    c.save()
    return out.getvalue()
