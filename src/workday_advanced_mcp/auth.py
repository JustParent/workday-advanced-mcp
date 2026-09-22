"""Workday authentication: Integration System User, or OAuth 2.0.

ISU is the default. Workday authenticates SOAP with a WS-Security UsernameToken,
which costs no extra round trip and is the credential most tenants issue for an
integration.

OAuth is offered because Harriet's first-party Workday integration already stores
a client id, client secret and refresh token, so a customer who has done that
connect flow can reuse those values here. The token request mirrors that
integration exactly (basic auth on the token endpoint, refresh grant, the
``soap_api`` scope that SOAP calls require).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Settings
from .errors import WorkdayAuthError
from .soap import build_username_token

# Refresh a little early so a call never starts with a token about to expire.
TOKEN_EXPIRY_MARGIN_SECONDS = 300
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600
SOAP_SCOPE = "soap_api"


class WorkdayAuth:
    """How a request proves who it is. Subclasses supply one or both halves."""

    async def security_header(self) -> dict[str, Any] | None:
        """The SOAP header block, for credentials that travel inside the envelope."""
        return None

    async def http_headers(self) -> dict[str, str]:
        """Extra HTTP headers, for credentials that travel in the transport."""
        return {}

    async def aclose(self) -> None:
        return None


class IsuAuth(WorkdayAuth):
    """WS-Security UsernameToken for an Integration System User."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def security_header(self) -> dict[str, Any] | None:
        return build_username_token(
            self._settings.qualified_isu_username, self._settings.isu_password
        )


class OAuthRefreshAuth(WorkdayAuth):
    """Bearer token from the refresh-token grant, cached for the process."""

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._token = ""
        self._expires_at = 0.0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {"timeout": self._settings.http_timeout}
            if self._settings.proxy_url:
                kwargs["proxy"] = self._settings.proxy_url
            self._client = httpx.AsyncClient(**kwargs)
            self._owns_client = True
        return self._client

    def invalidate(self) -> None:
        """Drop the cached token so the next call mints a fresh one."""
        self._token = ""
        self._expires_at = 0.0

    async def _fetch_token(self) -> str:
        settings = self._settings
        response = await self._get_client().post(
            settings.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": settings.refresh_token,
                "scope": SOAP_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(settings.client_id, settings.client_secret),
        )
        if response.status_code >= 400:
            raise WorkdayAuthError(
                "Workday refused the OAuth refresh grant "
                f"({response.status_code}). Check WORKDAY_CLIENT_ID, "
                "WORKDAY_CLIENT_SECRET and WORKDAY_REFRESH_TOKEN, and that the API "
                "client grants the 'soap_api' scope. Do not retry.",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WorkdayAuthError(
                "Workday returned a non-JSON response from the token endpoint."
            ) from exc

        token = str(payload.get("access_token") or "")
        if not token:
            raise WorkdayAuthError(
                "Workday's token response contained no access_token. Check that the "
                "refresh token is still valid and has not been rotated."
            )
        lifetime = payload.get("expires_in") or DEFAULT_TOKEN_LIFETIME_SECONDS
        try:
            lifetime_seconds = float(lifetime)
        except (TypeError, ValueError):
            lifetime_seconds = DEFAULT_TOKEN_LIFETIME_SECONDS

        self._token = token
        self._expires_at = time.monotonic() + max(
            lifetime_seconds - TOKEN_EXPIRY_MARGIN_SECONDS, 0.0
        )
        return token

    async def access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        return await self._fetch_token()

    async def http_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.access_token()}"}

    async def aclose(self) -> None:
        if (
            self._owns_client
            and self._client is not None
            and not self._client.is_closed
        ):
            await self._client.aclose()
        self._client = None


def build_auth(settings: Settings) -> WorkdayAuth:
    """The authentication strategy this configuration calls for."""
    from .config import AUTH_MODE_OAUTH

    if settings.auth_mode == AUTH_MODE_OAUTH:
        return OAuthRefreshAuth(settings)
    return IsuAuth(settings)
