"""Tests for the Lob address-verification client and the deliverability
warning mapping used by the /addresses/validate route."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from mailtrace.lob import LobClient, LobError
from mailtrace.models import User
from mailtrace.routes.addresses import _deliverability_warning


def _user(key: str = "live_testkey") -> User:
    return User(id=1, email="u@example.com", password_hash="x", lob_api_key=key)


def _lob_payload(
    *,
    primary="434 W 120TH ST",
    secondary="APT 8H",
    barcode="100276721888",
    deliverability="deliverable",
) -> bytes:
    return json.dumps(
        {
            "primary_line": primary,
            "secondary_line": secondary,
            "last_line": "NEW YORK NY 10027-6721",
            "deliverability": deliverability,
            "components": {
                "city": "NEW YORK",
                "state": "NY",
                "zip_code": "10027",
                "zip_code_plus_4": "6721",
                "delivery_point_barcode": barcode,
            },
        }
    ).encode()


def _client_with(handler) -> LobClient:  # type: ignore[no-untyped-def]
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LobClient(http_client=http)


async def test_verify_maps_fields_and_delivery_point() -> None:
    """zip5+zip4+dp must come out as 10027 / 6721 / 88 (the 11-digit routing
    code the IMb encoder expects), and the input firm name is preserved."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/us_verifications")
        assert request.method == "POST"
        # Basic auth: base64(key + ":")
        expected = "Basic " + base64.b64encode(b"live_testkey:").decode()
        assert request.headers["Authorization"] == expected
        return httpx.Response(200, content=_lob_payload())

    client = _client_with(handler)
    std = await client.verify(
        _user(),
        {
            "firmname": "Acme Co",
            "street_address": "434 w 120th st",
            "address2": "apt 8h",
            "city": "new york",
            "state": "ny",
            "zip5": "10027",
        },
    )
    assert std.street_address == "434 W 120TH ST"
    assert std.address2 == "APT 8H"
    assert std.city == "NEW YORK"
    assert std.state == "NY"
    assert (std.zip5, std.zip4, std.dp) == ("10027", "6721", "88")
    assert std.firmname == "Acme Co"  # Lob doesn't echo firm; we keep the input
    assert std.deliverability == "deliverable"
    await client.aclose()


async def test_verify_splits_folded_secondary_unit() -> None:
    """Lob often returns the whole line in primary_line with secondary_line
    empty (highrises). We must split the unit back into address2 using
    components so it doesn't all land in the street field."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "primary_line": "434 W 120TH ST APT 8H",
                    "secondary_line": "",
                    "deliverability": "deliverable",
                    "components": {
                        "secondary_designator": "APT",
                        "secondary_number": "8H",
                        "city": "NEW YORK",
                        "state": "NY",
                        "zip_code": "10027",
                        "zip_code_plus_4": "6721",
                        "delivery_point_barcode": "100276721888",
                    },
                }
            ).encode(),
        )

    client = _client_with(handler)
    std = await client.verify(_user(), {"street_address": "434 w 120th st apt 8h"})
    assert std.street_address == "434 W 120TH ST"
    assert std.address2 == "APT 8H"
    await client.aclose()


async def test_verify_po_box_left_intact() -> None:
    """No secondary in components → primary_line is used verbatim (don't
    mangle PO boxes / rural routes)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "primary_line": "PO BOX 123",
                    "secondary_line": "",
                    "deliverability": "deliverable",
                    "components": {
                        "city": "RENO",
                        "state": "NV",
                        "zip_code": "89501",
                        "zip_code_plus_4": "0123",
                        "delivery_point_barcode": "895010123992",
                    },
                }
            ).encode(),
        )

    client = _client_with(handler)
    std = await client.verify(_user(), {"street_address": "po box 123"})
    assert std.street_address == "PO BOX 123"
    assert std.address2 == ""
    await client.aclose()


async def test_verify_without_key_raises() -> None:
    client = _client_with(lambda request: httpx.Response(200, content=_lob_payload()))
    with pytest.raises(LobError, match="not configured"):
        await client.verify(_user(key=""), {"street_address": "x"})
    await client.aclose()


async def test_verify_surfaces_lob_error_message() -> None:
    """A 4xx with Lob's JSON error must surface the message, not a 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=json.dumps(
                {"error": {"message": "billing address required", "status_code": 403}}
            ).encode(),
        )

    client = _client_with(handler)
    with pytest.raises(LobError, match="billing address required"):
        await client.verify(_user(), {"street_address": "x", "zip5": "10027"})
    await client.aclose()


async def test_verify_non_json_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    client = _client_with(handler)
    with pytest.raises(LobError, match="non-JSON"):
        await client.verify(_user(), {"street_address": "x"})
    await client.aclose()


async def test_verify_short_barcode_yields_empty_dp() -> None:
    """If Lob returns no usable delivery-point barcode, dp is empty (the IMb
    just falls back to ZIP5/ZIP+4) rather than slicing garbage."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_lob_payload(barcode=""))

    client = _client_with(handler)
    std = await client.verify(_user(), {"street_address": "x", "zip5": "10027"})
    assert std.dp == ""
    assert (std.zip5, std.zip4) == ("10027", "6721")
    await client.aclose()


def test_deliverability_warning_mapping() -> None:
    assert _deliverability_warning("deliverable") is None
    assert _deliverability_warning("") is None
    assert "apartment" in (_deliverability_warning("deliverable_incorrect_unit") or "")
    assert _deliverability_warning("undeliverable")
