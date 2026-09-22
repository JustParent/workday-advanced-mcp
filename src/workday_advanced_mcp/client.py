"""HTTP transport for Workday SOAP calls.

Reads are retried on transient failures. Writes never are: every staffing write
submits a business process that is not idempotent, so a blind retry risks a
duplicate hire or a second termination event. A write that times out is reported
as ambiguous and the caller is told to check with ``workday_get_event`` first.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import OAuthRefreshAuth, WorkdayAuth, build_auth
from .config import Settings, load_settings
from .errors import (
    WorkdayApiError,
    WorkdayTimeoutError,
    fault_to_error,
    raise_for_http_error,
)
from .redact import redact_xml
from .soap import build_envelope, parse_envelope

# parse_envelope reports a body it could not read as a fault with this code.
PARSE_ERROR_CODE = "ParseError"

logger = logging.getLogger("workday_advanced_mcp")

SERVICE_STAFFING = "Staffing"
SERVICE_HUMAN_RESOURCES = "Human_Resources"
SERVICE_CASH_MANAGEMENT = "Cash_Management"
SERVICE_INTEGRATIONS = "Integrations"
SERVICE_RECRUITING = "Recruiting"

MAX_READ_RETRIES = 2
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 10.0

SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class CallResult:
    """A completed Workday call: the parsed body and what was sent to get it."""

    body: dict[str, Any]
    request_xml: str
    """The envelope sent, with bank details masked. Safe to return to a caller."""

    def response(self, operation: str) -> dict[str, Any]:
        """The ``<operation>_Response`` payload, or an empty mapping."""
        value = self.body.get(f"{operation}_Response")
        return value if isinstance(value, dict) else {}


class WorkdayClient:
    """Async SOAP client for one Workday tenant."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        auth: WorkdayAuth | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._auth = auth or build_auth(self._settings)
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._client: httpx.AsyncClient | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    def _get_client(self) -> httpx.AsyncClient:
        """Build the shared client lazily, so startup never touches the network."""
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": self._settings.http_timeout,
                "headers": {"Content-Type": "text/xml; charset=utf-8"},
            }
            if self._settings.proxy_url:
                kwargs["proxy"] = self._settings.proxy_url
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        await self._auth.aclose()

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                pass
        return min(float(2**attempt), MAX_RETRY_DELAY_SECONDS)

    async def _build_request(
        self, operation: str, request_data: dict[str, Any]
    ) -> tuple[str, dict[str, str]]:
        security = await self._auth.security_header()
        envelope = build_envelope(operation, request_data, security=security)
        headers = dict(await self._auth.http_headers())
        headers["SOAPAction"] = f"urn:com.workday/bsvc/{operation}"
        return envelope, headers

    async def call(
        self,
        service: str,
        operation: str,
        request_data: dict[str, Any],
        *,
        is_read: bool = False,
    ) -> CallResult:
        """Send one SOAP request and return its parsed body.

        Raises :class:`WorkdayFaultError` for a Workday validation failure,
        :class:`WorkdayAuthError` for a credential or permission problem, and
        :class:`WorkdayTimeoutError` when the call did not complete.
        """
        self._settings.require_ready()
        url = self._settings.service_url(service)
        client = self._get_client()

        attempt = 0
        reauthenticated = False
        while True:
            envelope, headers = await self._build_request(operation, request_data)
            try:
                response = await client.post(
                    url, content=envelope.encode("utf-8"), headers=headers
                )
            except httpx.TimeoutException as exc:
                raise WorkdayTimeoutError(
                    f"The {operation} call to Workday timed out after "
                    f"{self._settings.http_timeout:g}s."
                    + (
                        ""
                        if is_read
                        else " This write may still have committed - check with "
                        "workday_get_event or workday_find_worker before sending "
                        "it again."
                    )
                ) from exc
            except httpx.HTTPError as exc:
                raise WorkdayApiError(
                    f"Could not reach Workday for {operation}: {exc}"
                ) from exc

            # An expired bearer token is the one auth failure worth a second try,
            # and only once. An ISU 401 is never retried: Workday locks the account.
            if (
                response.status_code == 401
                and isinstance(self._auth, OAuthRefreshAuth)
                and not reauthenticated
            ):
                reauthenticated = True
                self._auth.invalidate()
                continue

            if (
                is_read
                and response.status_code in RETRYABLE_STATUSES
                and attempt < MAX_READ_RETRIES
            ):
                await self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue
            break

        # A credential or permission failure is more actionable than anything in
        # the body, and Workday often sends one with no body at all.
        if response.status_code in (401, 403):
            raise_for_http_error(response, service=service)

        parsed = parse_envelope(response.text)
        # Workday reports validation failures as a SOAP fault with HTTP 500, so a
        # real fault is preferred over the status code. A parse failure is not a
        # fault: it usually means the body was empty because the request never
        # reached the service, which the status code explains better.
        if parsed.fault is not None and parsed.fault.code != PARSE_ERROR_CODE:
            logger.warning("SOAP fault from %s (%s)", operation, response.status_code)
            raise fault_to_error(
                operation=operation,
                fault_code=parsed.fault.code,
                fault_string=parsed.fault.string,
                validation_errors=parsed.fault.validation_errors,
                status_code=response.status_code,
            )

        raise_for_http_error(response, service=service)

        if parsed.fault is not None:
            raise WorkdayApiError(
                f"Could not read Workday's response to {operation}: "
                f"{parsed.fault.string}",
                status_code=response.status_code,
            )

        return CallResult(body=parsed.body, request_xml=redact_xml(envelope))


_client: WorkdayClient | None = None


def get_client() -> WorkdayClient:
    """Return the process-wide client, creating it on first use."""
    global _client
    if _client is None:
        _client = WorkdayClient()
    return _client


def reset_client() -> None:
    """Drop the cached client so the next call re-reads the environment."""
    global _client
    _client = None
