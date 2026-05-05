"""PDF rendering helpers backed by wkhtmltopdf via pdfkit."""

from __future__ import annotations

from typing import Any

import pdfkit

ENVELOPE_OPTIONS: dict[str, Any] = {
    "page-height": "4.125in",
    "page-width": "9.5in",
    "margin-bottom": "0in",
    "margin-top": "0in",
    "margin-left": "0in",
    "margin-right": "0in",
    "disable-smart-shrinking": "",
    "enable-local-file-access": "",
    "encoding": "utf-8",
    "quiet": "",
}

LABEL_OPTIONS: dict[str, Any] = {
    "page-height": "11in",
    "page-width": "8.5in",
    "margin-bottom": "0in",
    "margin-top": "0in",
    "margin-left": "0in",
    "margin-right": "0in",
    "disable-smart-shrinking": "",
    "enable-local-file-access": "",
    "encoding": "utf-8",
    "quiet": "",
}


def render(html: str, *, options: dict[str, Any]) -> bytes:
    result: Any = pdfkit.from_string(html, False, options=options)
    if isinstance(result, str):
        return result.encode("utf-8")
    assert isinstance(result, bytes)
    return result
