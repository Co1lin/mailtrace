"""Lob (lob.com) US address verification client.

Backs the in-page "Validate" button on piece / address forms. Replaces the
USPS Addresses API, which USPS is discontinuing on 2026-07-12 in favour of a
paid, licensed product. Lob's US Verifications API is CASS-certified and —
crucially for us — returns the ZIP+4 *and* the 2-digit delivery point, which
together form the 11-digit routing code we encode into the IMb.

Free tier: 300 verifications/month, which dwarfs our usage. The key is
per-user (User.lob_api_key), set on the Account page.

Docs: https://docs.lob.com/#tag/US-Verifications
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urljoin

import httpx

from .models import User
from .usps import StandardizedAddress

LOB_API_BASE = "https://api.lob.com/v1/"


class LobError(RuntimeError):
    """Raised when Lob returns an error or is unreachable."""


def _auth_header(api_key: str) -> str:
    # Lob uses HTTP Basic with the API key as the username and an empty
    # password (key + ":").
    return "Basic " + base64.b64encode((api_key + ":").encode()).decode()


def _error_message(response: httpx.Response) -> str:
    """Pull Lob's human-readable error out of a non-2xx JSON body."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return f"HTTP {response.status_code}"


def _to_standardized(data: dict[str, Any], *, firmname: str) -> StandardizedAddress:
    comp = data.get("components") or {}
    # delivery_point_barcode is ZIP5(5) + ZIP4(4) + delivery-point(2) + check(1).
    # We want the 2-digit delivery point so zip5+zip4+dp = the 11-digit routing
    # code the IMb encoder expects.
    barcode = comp.get("delivery_point_barcode") or ""
    dp = barcode[9:11] if len(barcode) >= 11 else ""
    return StandardizedAddress(
        # Lob doesn't validate/return a firm name — preserve what the user typed.
        firmname=firmname,
        street_address=data.get("primary_line", "") or "",
        address2=data.get("secondary_line", "") or "",
        city=comp.get("city", "") or "",
        state=comp.get("state", "") or "",
        zip5=comp.get("zip_code", "") or "",
        zip4=comp.get("zip_code_plus_4", "") or "",
        dp=dp,
        deliverability=data.get("deliverability", "") or "",
    )


class LobClient:
    """Per-process; methods take the calling user so the key stays per-tenant."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=15)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post_verification(self, api_key: str, payload: dict[str, str]) -> dict[str, Any]:
        url = urljoin(LOB_API_BASE, "us_verifications")
        try:
            response = await self._http.post(
                url, json=payload, headers={"Authorization": _auth_header(api_key)}
            )
        except httpx.HTTPError as err:
            raise LobError(f"address verification failed: {err}") from err
        if response.status_code >= 400:
            raise LobError(f"address verification failed: {_error_message(response)}")
        try:
            data: dict[str, Any] = response.json()
        except ValueError as err:
            raise LobError(f"address verification returned a non-JSON response: {err}") from err
        return data

    async def verify(self, user: User, address: dict[str, str]) -> StandardizedAddress:
        api_key = (user.lob_api_key or "").strip()
        if not api_key:
            raise LobError(
                "Lob API key not configured — set it on your Account page (/auth/account)"
            )
        payload = {
            "primary_line": address.get("street_address", ""),
            "secondary_line": address.get("address2", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "zip_code": address.get("zip5", ""),
        }
        payload = {k: v for k, v in payload.items() if v}
        data = await self._post_verification(api_key, payload)
        return _to_standardized(data, firmname=address.get("firmname", ""))

    async def probe(self, user: User) -> None:
        """Confirm the user's Lob key works by verifying a known address.
        Raises LobError on auth/billing/connectivity failure. Used by the
        per-user Test button."""
        api_key = (user.lob_api_key or "").strip()
        if not api_key:
            raise LobError("Lob API key not configured")
        # Lob's own HQ — a stable, deliverable address. We only care that the
        # call authenticates and returns 200.
        await self._post_verification(
            api_key,
            {
                "primary_line": "210 King St",
                "city": "San Francisco",
                "state": "CA",
                "zip_code": "94107",
            },
        )
