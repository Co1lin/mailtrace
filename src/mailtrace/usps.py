"""USPS API client.

This module is multi-tenant by design: every method takes the calling
`User` and uses *that user's* USPS credentials, with a per-user token
cache in Redis. Two reasons:

  - apis.usps.com (modern, OAuth2 client_credentials) credentials come
    from a user's own developer.usps.com app — quota and rate limits are
    per-app, so users never starve each other.
  - iv.usps.com (legacy IV-MTR) credentials are a user's literal BCG
    login; that account's MID and visibility scope is the user's, not
    the platform's.

Tokens cache in Redis under per-user keys so rotating one user's app
keys never invalidates anyone else's.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx

from .store import Store

if TYPE_CHECKING:
    from .models import User

log = logging.getLogger(__name__)

USPS_API_BASE = "https://apis.usps.com/"
USPS_LEGACY_OAUTH = "https://services.usps.com"
USPS_LEGACY_IV_BASE = "https://iv.usps.com/ivws_api/informedvisapi/"


def _modern_token_keys(user_id: int) -> tuple[str, str, str]:
    base = f"mailtrace:usps:user:{user_id}:modern"
    return f"{base}:token", f"{base}:type", f"{base}:expiry"


def _legacy_token_keys(user_id: int) -> tuple[str, str, str]:
    base = f"mailtrace:usps:user:{user_id}:iv"
    return f"{base}:token", f"{base}:type", f"{base}:expiry"


class USPSError(RuntimeError):
    """Raised when USPS returns an error or is unreachable."""


@dataclass
class StandardizedAddress:
    firmname: str
    street_address: str
    address2: str
    city: str
    state: str
    zip5: str
    zip4: str
    dp: str

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


class USPSClient:
    def __init__(
        self,
        *,
        store: Store,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._http = http_client or httpx.AsyncClient(timeout=15)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- modern client_credentials (apis.usps.com) ------------------------

    async def _ensure_modern_token(self, user: User) -> tuple[str, str]:
        token_key, type_key, expiry_key = _modern_token_keys(user.id)
        expiry = await self._store.get_str(expiry_key)
        now = time.time()
        if expiry is None or now >= float(expiry):
            await self._refresh_modern_token(user)
        token = await self._store.get_str(token_key)
        token_type = await self._store.get_str(type_key) or "Bearer"
        if not token:
            raise USPSError("USPS access token unavailable")
        return token_type, token

    async def _refresh_modern_token(self, user: User) -> None:
        if not user.usps_client_id or not user.usps_client_secret:
            raise USPSError(
                "USPS API credentials not configured "
                "— set them on your Account page (/auth/account)"
            )
        url = urljoin(USPS_API_BASE, "/oauth2/v3/token")
        try:
            response = await self._http.post(
                url,
                json={
                    "client_id": user.usps_client_id,
                    "client_secret": user.usps_client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as err:
            raise USPSError(f"failed to obtain USPS token: {err}") from err

        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise USPSError("USPS token response missing access_token")
        expires_in = int(payload.get("expires_in", 1800))
        token_type = payload.get("token_type", "Bearer")
        refresh_at = time.time() + expires_in / 2
        token_key, type_key, expiry_key = _modern_token_keys(user.id)
        await self._store.set_str(token_key, access_token, ttl_seconds=expires_in)
        await self._store.set_str(type_key, token_type, ttl_seconds=expires_in)
        await self._store.set_str(expiry_key, str(refresh_at), ttl_seconds=expires_in)

    async def _modern_auth_headers(self, user: User) -> dict[str, str]:
        token_type, token = await self._ensure_modern_token(user)
        return {"Authorization": f"{token_type} {token}"}

    # --- legacy IV-MTR password OAuth (iv.usps.com) -----------------------

    async def _ensure_legacy_token(self, user: User) -> tuple[str, str]:
        token_key, type_key, expiry_key = _legacy_token_keys(user.id)
        expiry = await self._store.get_str(expiry_key)
        now = time.time()
        if expiry is None or now >= float(expiry):
            await self._refresh_legacy_token(user)
        token = await self._store.get_str(token_key)
        token_type = await self._store.get_str(type_key) or "Bearer"
        if not token:
            raise USPSError("USPS IV access token unavailable")
        return token_type, token

    async def _refresh_legacy_token(self, user: User) -> None:
        if not user.bcg_username or not user.bcg_password:
            raise USPSError(
                "Business Customer Gateway credentials not configured "
                "— set them on your Account page (/auth/account)"
            )
        url = urljoin(USPS_LEGACY_OAUTH, "oauth/authenticate")
        data = {
            "username": user.bcg_username,
            "password": user.bcg_password,
            "grant_type": "authorization",
            "response_type": "token",
            "scope": "user.info.ereg,iv1.apis",
            "client_id": "687b8a36-db61-42f7-83f7-11c79bf7785e",
        }
        try:
            response = await self._http.post(url, json=data)
            response.raise_for_status()
        except httpx.HTTPError as err:
            raise USPSError(f"failed to obtain IV token: {err}") from err
        payload = response.json()
        access_token = payload["access_token"]
        token_type = payload.get("token_type", "Bearer")
        expires_in = int(payload.get("expires_in", 1800))
        refresh_at = time.time() + expires_in / 2
        token_key, type_key, expiry_key = _legacy_token_keys(user.id)
        await self._store.set_str(token_key, access_token, ttl_seconds=expires_in)
        await self._store.set_str(type_key, token_type, ttl_seconds=expires_in)
        await self._store.set_str(expiry_key, str(refresh_at), ttl_seconds=expires_in)

    # --- public API -------------------------------------------------------

    async def get_piece_tracking(self, user: User, imb: str) -> dict[str, Any]:
        """Fetch on-demand tracking data from the IV piece API for `user`'s piece."""
        token_type, token = await self._ensure_legacy_token(user)
        url = urljoin(USPS_LEGACY_IV_BASE, f"api/mt/get/piece/imb/{imb}")
        try:
            response = await self._http.get(url, headers={"Authorization": f"{token_type} {token}"})
            response.raise_for_status()
        except httpx.HTTPError as err:
            raise USPSError(f"piece tracking failed: {err}") from err
        result: dict[str, Any] = response.json()
        return result

    async def standardize_address(self, user: User, address: dict[str, str]) -> StandardizedAddress:
        params = {
            "firm": address.get("firmname", ""),
            "streetAddress": address.get("street_address", ""),
            "secondaryAddress": address.get("address2", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "ZIPCode": address.get("zip5", ""),
            "ZIPPlus4": address.get("zip4", ""),
        }
        params = {k: v for k, v in params.items() if v}
        headers = await self._modern_auth_headers(user)
        headers["accept"] = "application/json"
        url = urljoin(USPS_API_BASE, "/addresses/v3/address")
        try:
            response = await self._http.get(url, headers=headers, params=params)
            if response.status_code == 401:
                await self._refresh_modern_token(user)
                headers = await self._modern_auth_headers(user)
                headers["accept"] = "application/json"
                response = await self._http.get(url, headers=headers, params=params)
            response.raise_for_status()
        except httpx.HTTPError as err:
            raise USPSError(f"address standardization failed: {err}") from err
        data = response.json()
        if "errors" in data:
            raise USPSError(str(data["errors"]))
        info = data.get("address", {})
        extra = data.get("additionalInfo", {})
        return StandardizedAddress(
            firmname=data.get("firm", "") or "",
            street_address=info.get("streetAddress", "") or "",
            address2=info.get("secondaryAddress", "") or "",
            city=info.get("city", "") or "",
            state=info.get("state", "") or "",
            zip5=info.get("ZIPCode", "") or "",
            zip4=info.get("ZIPPlus4", "") or "",
            dp=extra.get("deliveryPoint", "") or "",
        )

    async def probe_modern_creds(self, user: User) -> None:
        """Test the user's USPS API creds by forcing a fresh token grant.
        Raises USPSError on failure. Used by the per-user Test button."""
        await self._refresh_modern_token(user)

    async def probe_legacy_creds(self, user: User) -> None:
        """Test the user's BCG creds by forcing a fresh IV token grant.
        Raises USPSError on failure. Used by the per-user Test button."""
        await self._refresh_legacy_token(user)
