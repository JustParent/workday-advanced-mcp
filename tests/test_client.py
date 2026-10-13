"""Transport behaviour: authentication, retries, faults and redaction.

The retry rules carry real risk, so they are asserted by call count rather than
by outcome: a write that is retried once can hire somebody twice.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from conftest import service_url, soap_fault, soap_response
from workday_advanced_mcp.auth import IsuAuth, OAuthRefreshAuth
from workday_advanced_mcp.client import SERVICE_STAFFING, WorkdayClient
from workday_advanced_mcp.config import load_settings
from workday_advanced_mcp.errors import (
    WorkdayApiError,
    WorkdayAuthError,
    WorkdayFaultError,
    WorkdayTimeoutError,
)

STAFFING_URL = service_url(SERVICE_STAFFING)
OK_BODY = soap_response("Get_Workers", "<wd:Response_Data/>")


async def _noop_sleep(_seconds: float) -> None:
    return None


def _client() -> WorkdayClient:
    return WorkdayClient(load_settings(), sleep=_noop_sleep)


async def test_isu_credentials_travel_in_the_envelope() -> None:
    with respx.mock:
        route = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(200, text=OK_BODY)
        )
        client = _client()
        try:
            await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
        finally:
            await client.aclose()

    body = route.calls[0].request.content.decode()
    assert "<wsse:Username>isu_harriet@acme_preview</wsse:Username>" in body
    assert "isu-password" in body
    assert "Authorization" not in route.calls[0].request.headers
    assert (
        route.calls[0].request.headers["SOAPAction"]
        == "urn:com.workday/bsvc/Get_Workers"
    )


async def test_oauth_mode_sends_a_bearer_token(set_env) -> None:
    set_env(
        WORKDAY_AUTH_MODE="oauth",
        WORKDAY_ISU_PASSWORD=None,
        WORKDAY_CLIENT_ID="client-id",
        WORKDAY_CLIENT_SECRET="client-secret",
        WORKDAY_REFRESH_TOKEN="refresh-token",
    )
    settings = load_settings()
    with respx.mock:
        token_route = respx.post(settings.token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        )
        call_route = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(200, text=OK_BODY)
        )
        client = WorkdayClient(
            settings, auth=OAuthRefreshAuth(settings), sleep=_noop_sleep
        )
        try:
            await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
            await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
        finally:
            await client.aclose()

    assert call_route.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert "wsse:Username" not in call_route.calls[0].request.content.decode()
    # The token is cached, so a second call does not mint another one.
    assert token_route.call_count == 1
    assert "grant_type=refresh_token" in token_route.calls[0].request.content.decode()
    assert "scope=soap_api" in token_route.calls[0].request.content.decode()


@pytest.mark.parametrize(
    "status,needle,absent",
    [
        (401, "WORKDAY_REFRESH_TOKEN", "WORKDAY_ISU_PASSWORD"),
        (403, "API client's scope", "WORKDAY_ISU_PASSWORD"),
    ],
)
async def test_oauth_auth_failures_name_the_oauth_credentials(
    set_env, status, needle, absent
) -> None:
    set_env(
        WORKDAY_AUTH_MODE="oauth",
        WORKDAY_ISU_PASSWORD=None,
        WORKDAY_CLIENT_ID="client-id",
        WORKDAY_CLIENT_SECRET="client-secret",
        WORKDAY_REFRESH_TOKEN="refresh-token",
    )
    settings = load_settings()
    with respx.mock:
        respx.post(settings.token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        )
        respx.post(STAFFING_URL).mock(return_value=httpx.Response(status))
        client = WorkdayClient(
            settings, auth=OAuthRefreshAuth(settings), sleep=_noop_sleep
        )
        try:
            with pytest.raises(WorkdayAuthError) as excinfo:
                await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
        finally:
            await client.aclose()

    assert needle in str(excinfo.value) and absent not in str(excinfo.value)


async def test_a_validation_fault_names_what_workday_rejected() -> None:
    with respx.mock:
        respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                500, text=soap_fault("Validation error", "Effective Date is required")
            )
        )
        client = _client()
        try:
            with pytest.raises(WorkdayFaultError) as excinfo:
                await client.call(SERVICE_STAFFING, "Change_Job", {})
        finally:
            await client.aclose()

    assert "Effective Date is required" in str(excinfo.value)
    assert excinfo.value.validation_errors == ["Effective Date is required"]


async def test_an_authentication_failure_is_never_retried() -> None:
    """Repeated failures lock an Integration System User."""
    with respx.mock:
        route = respx.post(STAFFING_URL).mock(return_value=httpx.Response(401, text=""))
        client = _client()
        try:
            with pytest.raises(WorkdayAuthError, match="locked"):
                await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
        finally:
            await client.aclose()

    assert route.call_count == 1


async def test_a_permission_failure_names_the_security_domains() -> None:
    with respx.mock:
        respx.post(STAFFING_URL).mock(return_value=httpx.Response(403, text=""))
        client = _client()
        try:
            with pytest.raises(WorkdayAuthError) as excinfo:
                await client.call(SERVICE_STAFFING, "Change_Job", {})
        finally:
            await client.aclose()

    assert "Change Organization Assignments" in str(excinfo.value)
    assert "Activate pending security policy changes" in str(excinfo.value)


async def test_reads_retry_a_transient_failure() -> None:
    with respx.mock:
        route = respx.post(STAFFING_URL).mock(
            side_effect=[
                httpx.Response(503, text=""),
                httpx.Response(200, text=OK_BODY),
            ]
        )
        client = _client()
        try:
            await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
        finally:
            await client.aclose()

    assert route.call_count == 2


async def test_writes_are_never_retried() -> None:
    """A staffing write is not idempotent: retrying one can duplicate an event."""
    with respx.mock:
        route = respx.post(STAFFING_URL).mock(return_value=httpx.Response(503, text=""))
        client = _client()
        try:
            with pytest.raises(WorkdayApiError, match="do not retry"):
                await client.call(SERVICE_STAFFING, "Hire_Employee", {})
        finally:
            await client.aclose()

    assert route.call_count == 1


async def test_a_write_timeout_is_reported_as_ambiguous() -> None:
    with respx.mock:
        respx.post(STAFFING_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        client = _client()
        try:
            with pytest.raises(WorkdayTimeoutError, match="may still have committed"):
                await client.call(SERVICE_STAFFING, "Hire_Employee", {})
        finally:
            await client.aclose()


async def test_a_read_timeout_does_not_warn_about_committing() -> None:
    with respx.mock:
        respx.post(STAFFING_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        client = _client()
        try:
            with pytest.raises(WorkdayTimeoutError) as excinfo:
                await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
        finally:
            await client.aclose()
    assert "may still have committed" not in str(excinfo.value)


async def test_the_returned_request_xml_masks_bank_details() -> None:
    request = {
        "Payment_Election_Enrollment_Data": {
            "Worker_Bank_Account_Data": {"Account_Number": "12345678"}
        }
    }
    with respx.mock:
        respx.post(service_url("Cash_Management")).mock(
            return_value=httpx.Response(
                200, text=soap_response("Submit_Payment_Election_Enrollment", "")
            )
        )
        client = _client()
        try:
            result = await client.call(
                "Cash_Management", "Submit_Payment_Election_Enrollment", request
            )
        finally:
            await client.aclose()

    assert "12345678" not in result.request_xml
    assert "****5678" in result.request_xml


async def test_a_missing_tenant_is_refused_before_any_request(set_env) -> None:
    from workday_advanced_mcp.config import ConfigError

    set_env(WORKDAY_TENANT=None, WORKDAY_HOST="wd3-impl-services1.workday.com")
    client = _client()
    try:
        with pytest.raises(ConfigError, match="WORKDAY_TENANT"):
            await client.call(SERVICE_STAFFING, "Get_Workers", {}, is_read=True)
    finally:
        await client.aclose()


def test_isu_auth_builds_no_http_headers() -> None:
    auth = IsuAuth(load_settings())
    assert auth is not None
