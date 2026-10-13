"""Preflight, readback and recovery boundaries that are easy to miss in happy paths."""

from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
import respx

from conftest import call_tool, soap_fault
from test_lifecycle import DATE, HR, STAFFING, org, org_response
from test_regressions import JOB, PAYMENT, xml_response
from test_tools_write import staffing_response
from workday_advanced_mcp.auth import OAuthRefreshAuth, build_auth
from workday_advanced_mcp.client import WorkdayClient
from workday_advanced_mcp.config import load_settings
from workday_advanced_mcp.errors import (
    WorkdayApiError,
    WorkdayAuthError,
    fault_to_error,
    format_exception,
    raise_for_http_error,
)
from workday_advanced_mcp.execution import Execution, current_execution
from workday_advanced_mcp.operations import lifecycle, payment_elections, staffing
from workday_advanced_mcp.redact import redact_data
from workday_advanced_mcp.references import reference
from workday_advanced_mcp.validation import iso_moment, required


@pytest.mark.parametrize(
    "operation,args,needle",
    [
        ("workday_change_job", {**JOB, "changes": []}, "No job"),
        (
            "workday_change_job",
            {**JOB, "changes": [{"worker_id": "1"}] * 26},
            "more than",
        ),
        ("workday_change_job", {**JOB, "changes": [{}]}, "worker_id"),
        (
            "workday_change_job",
            {**JOB, "changes": [{"worker_id": "1"}]},
            "nothing to change",
        ),
        (
            "workday_change_organization_assignments",
            {"effective_date": DATE, "changes": [{}]},
            "worker_id",
        ),
        (
            "workday_change_organization_assignments",
            {"effective_date": DATE, "changes": [{"worker_id": "1"}]},
            "no assignments",
        ),
        (
            "workday_terminate_employee",
            {"worker_id": "1", "termination_date": DATE, "primary_reason_id": ""},
            "primary_reason",
        ),
        (
            "workday_create_pre_hire",
            {"first_name": "", "last_name": "P", "email": "p@example.com"},
            "first_name",
        ),
        (
            "workday_create_pre_hire",
            {"first_name": "P", "last_name": "P"},
            "contact method",
        ),
        (
            "workday_hire_employee",
            {"organization_id": "", "hire_date": DATE},
            "organization_id",
        ),
        ("workday_set_payment_elections", {**PAYMENT, "country": ""}, "country"),
        (
            "workday_set_payment_elections",
            {**PAYMENT, "rule_groups": []},
            "complete set",
        ),
        ("workday_get_event", {"event_id": ""}, "event id"),
        ("workday_find_worker", {"query": ""}, "Pass an"),
        (
            "workday_get_manager_organizations",
            {"worker_id": "1", "max_pages": 0},
            "max_pages",
        ),
        ("workday_list_positions", {"count": 0}, "count"),
        (
            "workday_list_organization_workers",
            {"organization_id": "O", "page": 0},
            "page",
        ),
        (
            "workday_recover_event",
            {"event_id": "E", "action": "delete", "comment": "reason"},
            "action",
        ),
    ],
)
async def test_invalid_or_incomplete_inputs_never_submit(
    server_factory, worker_response, operation, args, needle
):
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=worker_response))
        result = await call_tool(server_factory(), operation, args)
        assert all(
            "Get_Workers_Request" in c.request.content.decode() for c in respx.calls
        )
    assert needle in result


@pytest.mark.parametrize(
    "operation,args,replacement",
    [
        ("workday_change_job", {**JOB, "auto_complete": True}, ("SUP-ENG", "SUP-NEW")),
        (
            "workday_change_organization_assignments",
            {
                "changes": [
                    {"worker_id": "21001", "assignments": {"Cost_Center": ["CC_200"]}}
                ],
                "effective_date": DATE,
                "auto_complete": True,
            },
            ("CC_100", "CC_200"),
        ),
    ],
)
async def test_readback_verifies_expected_values_at_requested_effective_date(
    server_factory, worker_response, set_env, operation, args, replacement
):
    set_env(WORKDAY_ALLOW_AUTO_COMPLETE="true")
    with respx.mock:
        reads = respx.post(HR).mock(
            side_effect=[
                httpx.Response(200, text=worker_response),
                httpx.Response(200, text=worker_response.replace(*replacement)),
            ]
        )
        api_operation = (
            "Change_Job"
            if operation.endswith("job")
            else "Change_Organization_Assignments"
        )
        respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response(api_operation))
        )
        result = await call_tool(server_factory(), operation, args)
    assert result["results"][0]["verification"]["verified"] is True
    assert all(
        f"<bsvc:As_Of_Effective_Date>{DATE}" in c.request.content.decode()
        for c in reads.calls
    )


async def test_termination_readback_matches_date_and_state(
    server_factory, worker_response, set_env
):
    set_env(WORKDAY_ALLOW_AUTO_COMPLETE="true")
    terminated = worker_response.replace(
        "<wd:Terminated>false", "<wd:Terminated>true"
    ).replace(
        "</wd:Worker_Status_Data>",
        f"<wd:Termination_Date>{DATE}</wd:Termination_Date></wd:Worker_Status_Data>",
    )
    with respx.mock:
        respx.post(HR).mock(
            side_effect=[
                httpx.Response(200, text=worker_response),
                httpx.Response(200, text=terminated),
            ]
        )
        respx.post(STAFFING).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Terminate_Employee")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_terminate_employee",
            {
                "worker_id": "21001",
                "termination_date": DATE,
                "primary_reason_id": "Voluntary",
                "auto_complete": True,
            },
        )
    assert result["verification"]["verified"] is True


async def test_readback_failure_never_hides_a_successful_submission(
    server_factory, worker_response, set_env
):
    set_env(WORKDAY_ALLOW_AUTO_COMPLETE="true")
    with respx.mock:
        respx.post(HR).mock(
            side_effect=[
                httpx.Response(200, text=worker_response),
                httpx.ConnectError("read failed"),
            ]
        )
        respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        result = await call_tool(
            server_factory(), "workday_change_job", {**JOB, "auto_complete": True}
        )
    assert result["results"][0]["submitted"] is True
    assert result["results"][0]["event"]["id"] == "event-1"
    assert "reading it back failed" in result["results"][0]["verification_error"]


async def test_rehire_uses_former_worker_identity_and_explicit_position(
    server_factory, worker_response
):
    inactive = worker_response.replace("<wd:Active>true", "<wd:Active>false").replace(
        "<wd:Terminated>false", "<wd:Terminated>true"
    )
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=inactive))
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Hire_Employee"))
        )
        result = await call_tool(
            server_factory(),
            "workday_hire_employee",
            {
                "organization_id": "SUP-ENG",
                "hire_date": DATE,
                "former_worker_id": "21001",
                "employee_id": "21001",
                "position_id": "P1",
            },
        )
    assert result["submitted"] is True
    xml = route.calls[0].request.content.decode()
    assert (
        "Former_Worker_Reference" in xml
        and "Position_Reference" in xml
        and "Applicant_Data" not in xml
    )


async def test_inactive_worker_requires_explicit_rehire_identity(
    server_factory, worker_response
):
    with respx.mock:
        respx.post(HR).mock(
            return_value=httpx.Response(
                200, text=worker_response.replace("<wd:Active>true", "<wd:Active>false")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_hire_employee",
            {
                "organization_id": "SUP-ENG",
                "hire_date": DATE,
                "pre_hire_wid": "APP",
                "employee_id": "21001",
            },
        )
    assert result["duplicate"] is True


async def test_list_positions_and_members_return_page_metadata(
    server_factory, worker_response
):
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=worker_response))
        respx.post(STAFFING).mock(
            return_value=httpx.Response(
                200,
                text=xml_response(
                    "Get_Positions",
                    {
                        "Response_Results": {"Page": 1, "Total_Pages": 2},
                        "Response_Data": {
                            "Position": {
                                "Position_Reference": reference("Position_ID", "P1"),
                                "Position_Data": {"Job_Posting_Title": "Analyst"},
                            }
                        },
                    },
                ),
            )
        )
        server = server_factory()
        positions = await call_tool(
            server,
            "workday_list_positions",
            {"organization_id": "TOP", "as_of_effective_date": DATE},
        )
        members = await call_tool(
            server,
            "workday_list_organization_workers",
            {"organization_id": "TOP", "as_of_effective_date": DATE},
        )
    assert positions["positions"][0]["id"] == "P1" and positions["total_pages"] == 2
    assert members["workers"][0]["worker_id"] == "21001"


@pytest.mark.parametrize(
    "entries,needle", [([], "not found"), ([org(), org()], "more than one")]
)
async def test_exact_organization_read_never_guesses(server_factory, entries, needle):
    with respx.mock:
        respx.post(HR).mock(
            return_value=httpx.Response(200, text=org_response(*entries))
        )
        result = await call_tool(
            server_factory(), "workday_get_organization", {"organization_id": "TOP"}
        )
    assert needle in result


async def test_validation_fault_can_be_corrected_and_resubmitted(
    server_factory, worker_response
):
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=worker_response))
        route = respx.post(STAFFING).mock(
            side_effect=[
                httpx.Response(
                    500, text=soap_fault("Validation error", "Bad reference")
                ),
                httpx.Response(200, text=staffing_response("Change_Job")),
            ]
        )
        server = server_factory()
        first = await call_tool(server, "workday_change_job", JOB)
        second = await call_tool(server, "workday_change_job", JOB)
    assert first["results"][0]["outcome_unknown"] is False
    assert second["results"][0]["submitted"] is True and route.call_count == 2


async def test_task_cancellation_reaches_the_host(monkeypatch):
    """A cancelled write is the host's to reconcile, so it must not be swallowed."""
    token = current_execution.set(Execution(arguments=["test", {}]))
    client = WorkdayClient()
    try:

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(client, "_send", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await client.call("Staffing", "Change_Job", {})
    finally:
        current_execution.reset(token)
        await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_transport_errors_remain_actionable(status):
    with pytest.raises(WorkdayApiError):
        raise_for_http_error(httpx.Response(status), service="Staffing")


def test_invalid_business_data_is_not_misreported_as_bad_credentials():
    exc = fault_to_error(
        operation="Change_Job",
        fault_code="Validation",
        fault_string="Invalid organization",
        validation_errors=[],
        status_code=500,
    )
    assert "authenticate" not in str(exc)
    for error in [
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("connect"),
        RuntimeError("test"),
    ]:
        assert format_exception(error).startswith("Error:")
    assert (
        redact_data({"Password": {"@Type": "PasswordText", "#text": "secret"}})[
            "Password"
        ]
        == "[REDACTED]"
    )


@pytest.mark.parametrize(
    "response,needle",
    [
        (httpx.Response(401), "refresh grant"),
        (httpx.Response(200, text="not-json"), "non-JSON"),
        (httpx.Response(200, json={}), "no access_token"),
    ],
)
async def test_oauth_failure_never_discloses_credentials(set_env, response, needle):
    set_env(
        WORKDAY_AUTH_MODE="oauth",
        WORKDAY_CLIENT_ID="id",
        WORKDAY_CLIENT_SECRET="secret",
        WORKDAY_REFRESH_TOKEN="refresh",
    )
    settings = load_settings()
    auth = build_auth(settings)
    with respx.mock:
        respx.post(settings.token_url).mock(return_value=response)
        with pytest.raises(WorkdayAuthError, match=needle):
            await auth.http_headers()
    await auth.aclose()


async def test_oauth_bad_expiry_uses_default_without_closing_injected_client(set_env):
    set_env(
        WORKDAY_AUTH_MODE="oauth",
        WORKDAY_CLIENT_ID="id",
        WORKDAY_CLIENT_SECRET="secret",
        WORKDAY_REFRESH_TOKEN="refresh",
    )
    settings = load_settings()
    async with httpx.AsyncClient() as transport:
        auth = OAuthRefreshAuth(settings, client=transport)
        with respx.mock:
            respx.post(settings.token_url).mock(
                return_value=httpx.Response(
                    200, json={"access_token": "token", "expires_in": "bad"}
                )
            )
            assert await auth.access_token() == "token"
            auth.invalidate()
            assert await auth.access_token() == "token"
        await auth.aclose()
        assert not transport.is_closed


@pytest.mark.parametrize(
    "field,value",
    [
        ("distribution_amount", "-1"),
        ("distribution_amount", "NaN"),
        ("distribution_amount", "no"),
        ("distribution_percentage", "101"),
        ("distribution_percentage", "0"),
    ],
)
def test_invalid_bank_distributions_are_rejected(field, value):
    args = copy.deepcopy(PAYMENT)
    args.pop("worker_id")
    election = args["rule_groups"][0]["elections"][0]
    election.pop("distribution_balance")
    election[field] = value
    with pytest.raises(ValueError):
        payment_elections.build_submit_enrollment_request(worker_wid="W", **args)


@pytest.mark.parametrize(
    "change", [{"payment_election_rule_id": ""}, {"elections": []}]
)
def test_incomplete_bank_groups_are_rejected(change):
    args = copy.deepcopy(PAYMENT)
    args.pop("worker_id")
    args["rule_groups"][0].update(change)
    with pytest.raises(ValueError):
        payment_elections.build_submit_enrollment_request(worker_wid="W", **args)


def test_remaining_builder_choices_and_validation():
    with pytest.raises(ValueError):
        staffing.build_hire_employee_request(
            organization_id="O", hire_date=DATE, pre_hire_wid="A", former_worker_id="W"
        )
    with pytest.raises(ValueError):
        staffing.build_hire_employee_request(
            organization_id="O",
            hire_date=DATE,
            pre_hire_wid="A",
            position_id="P",
            job_requisition_id="R",
        )
    with pytest.raises(ValueError):
        iso_moment("2026-10-01T00:00:00", "updated_from")
    with pytest.raises(ValueError):
        required(" ", "id")
    with pytest.raises(ValueError):
        lifecycle.build_home_address(
            worker_id="W",
            effective_date=DATE,
            address={"country": "GB", "lines": {"wrong": "line"}},
        )
