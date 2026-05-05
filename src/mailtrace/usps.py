"""USPS API client (apis.usps.com).

Covers OAuth2 token management, the IMb piece-tracking endpoint, and
address standardization. Tokens are cached in Redis (via ``Store``) so they
survive worker restarts and are shared across processes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .store import Store

log = logging.getLogger(__name__)

USPS_API_BASE = "https://apis.usps.com/"
USPS_LEGACY_OAUTH = "https://services.usps.com"
USPS_LEGACY_IV_BASE = "https://iv.usps.com/ivws_api/informedvisapi/"

_TOKEN_KEY = "mailtrace:usps:access_token"
_TOKEN_TYPE_KEY = "mailtrace:usps:token_type"
_TOKEN_EXPIRY_KEY = "mailtrace:usps:token_expiry"

_LEGACY_TOKEN_KEY = "mailtrace:usps:iv_access_token"
_LEGACY_REFRESH_KEY = "mailtrace:usps:iv_refresh_token"
_LEGACY_TYPE_KEY = "mailtrace:usps:iv_token_type"
_LEGACY_EXPIRY_KEY = "mailtrace:usps:iv_token_expiry"


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
        client_id: str,
        client_secret: str,
        bcg_username: str = "",
        bcg_password: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._client_id = client_id
        self._client_secret = client_secret
        self._bcg_username = bcg_username
        self._bcg_password = bcg_password
        self._http = http_client or httpx.AsyncClient(timeout=15)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- token management -------------------------------------------------

    async def _ensure_token(self) -> tuple[str, str]:
        expiry = await self._store.get_str(_TOKEN_EXPIRY_KEY)
        now = time.time()
        if expiry is None or now >= float(expiry):
            await self._refresh_token()
        token = await self._store.get_str(_TOKEN_KEY)
        token_type = await self._store.get_str(_TOKEN_TYPE_KEY) or "Bearer"
        if not token:
            raise USPSError("USPS access token unavailable")
        return token_type, token

    async def _refresh_token(self) -> None:
        if not self._client_id or not self._client_secret:
            raise USPSError("USPS credentials not configured")
        url = urljoin(USPS_API_BASE, "/oauth2/v3/token")
        try:
            response = await self._http.post(
                url,
                json={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
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
        await self._store.set_str(_TOKEN_KEY, access_token, ttl_seconds=expires_in)
        await self._store.set_str(_TOKEN_TYPE_KEY, token_type, ttl_seconds=expires_in)
        await self._store.set_str(_TOKEN_EXPIRY_KEY, str(refresh_at), ttl_seconds=expires_in)

    async def _auth_headers(self) -> dict[str, str]:
        token_type, token = await self._ensure_token()
        return {"Authorization": f"{token_type} {token}"}

    # --- legacy IV oauth (for piece tracking) -----------------------------

    async def _ensure_legacy_token(self) -> tuple[str, str]:
        expiry = await self._store.get_str(_LEGACY_EXPIRY_KEY)
        now = time.time()
        if expiry is None or now >= float(expiry):
            await self._refresh_legacy_token()
        token = await self._store.get_str(_LEGACY_TOKEN_KEY)
        token_type = await self._store.get_str(_LEGACY_TYPE_KEY) or "Bearer"
        if not token:
            raise USPSError("USPS IV access token unavailable")
        return token_type, token

    async def _refresh_legacy_token(self) -> None:
        if not self._bcg_username or not self._bcg_password:
            raise USPSError("Business Customer Gateway credentials not configured")
        url = urljoin(USPS_LEGACY_OAUTH, "oauth/authenticate")
        data = {
            "username": self._bcg_username,
            "password": self._bcg_password,
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
        refresh_token = payload.get("refresh_token", "")
        token_type = payload.get("token_type", "Bearer")
        expires_in = int(payload.get("expires_in", 1800))
        refresh_at = time.time() + expires_in / 2
        await self._store.set_str(_LEGACY_TOKEN_KEY, access_token, ttl_seconds=expires_in)
        await self._store.set_str(_LEGACY_TYPE_KEY, token_type, ttl_seconds=expires_in)
        await self._store.set_str(_LEGACY_EXPIRY_KEY, str(refresh_at), ttl_seconds=expires_in)
        if refresh_token:
            await self._store.set_str(
                _LEGACY_REFRESH_KEY, refresh_token, ttl_seconds=expires_in * 4
            )

    # --- public API -------------------------------------------------------

    async def get_piece_tracking(self, imb: str) -> dict[str, Any]:
        """Fetch on-demand tracking data from the IV piece API."""
        token_type, token = await self._ensure_legacy_token()
        url = urljoin(USPS_LEGACY_IV_BASE, f"api/mt/get/piece/imb/{imb}")
        try:
            response = await self._http.get(url, headers={"Authorization": f"{token_type} {token}"})
            response.raise_for_status()
        except httpx.HTTPError as err:
            raise USPSError(f"piece tracking failed: {err}") from err
        result: dict[str, Any] = response.json()
        return result

    async def standardize_address(self, address: dict[str, str]) -> StandardizedAddress:
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
        headers = await self._auth_headers()
        headers["accept"] = "application/json"
        url = urljoin(USPS_API_BASE, "/addresses/v3/address")
        try:
            response = await self._http.get(url, headers=headers, params=params)
            if response.status_code == 401:
                await self._refresh_token()
                headers = await self._auth_headers()
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
