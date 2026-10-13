"""Behavioral regression coverage for write safety and reconciliation."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
import respx

from conftest import call_tool, service_url, soap_response
from test_tools_write import staffing_response
from workday_advanced_mcp.client import reset_client
from workday_advanced_mcp.operations import payment_elections
from workday_advanced_mcp.redact import account_fingerprint, redact_data, redact_xml
from workday_advanced_mcp.soap import build_envelope

HR = service_url("Human_Resources")
STAFFING = service_url("Staffing")
CASH = service_url("Cash_Management")
JOB = {
    "changes": [{"worker_id": "21001", "supervisory_organization_id": "SUP-NEW"}],
    "effective_date": "2026-10-01",
    "reason_id": "Transfer",
}
PAYMENT = {
    "worker_id": "21001",
    "country": "GB",
    "currency": "GBP",
    "rule_groups": [
        {
            "payment_election_rule_id": "expenses",
            "elections": [
                {
                    "order": 1,
                    "payment_type_id": "Direct_Deposit",
                    "distribution_balance": True,
                    "bank_account": {
                        "account_number": "12345678",
                        "bank_id_number": "123456",
                    },
                }
            ],
        }
    ],
}


def xml_response(operation, data, root=None):
    xml = build_envelope(operation, data)
    return xml.replace(f"{operation}_Request", root or f"{operation}_Response")


def payment_response(arguments=None):
    args = copy.deepcopy(arguments or PAYMENT)
    args.pop("worker_id")
    request = payment_elections.build_submit_enrollment_request(
        worker_wid="worker-wid-1", **args
    )
    return xml_response(
        payment_elections.GET_ENROLLMENTS,
        {
            "Payment_Election_Enrollment_Response_Data": {
                "Payment_Election_Enrollment": {
                    "Payment_Election_Enrollment_Data": request[
                        "Payment_Election_Enrollment_Data"
                    ]
                }
            }
        },
    )


def mock_worker(worker_response):
    return respx.post(HR).mock(return_value=httpx.Response(200, text=worker_response))


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "routing",
        "rule",
        "currency",
        "amount",
        "percentage",
        "order",
        "bank_name",
        "account_name",
    ],
)
async def test_payment_changes_never_disappear_as_noop(
    server_factory, worker_response, change
):
    args = copy.deepcopy(PAYMENT)
    election = args["rule_groups"][0]["elections"][0]
    if change == "account":
        election["bank_account"]["account_number"] = (
            "99995678"  # same length and last four
        )
    elif change == "routing":
        election["bank_account"]["bank_id_number"] = "654321"
    elif change == "rule":
        args["rule_groups"][0]["payment_election_rule_id"] = "payroll"
    elif change == "currency":
        args["currency"] = "EUR"
    elif change in {"amount", "percentage"}:
        election.pop("distribution_balance")
        election[f"distribution_{change}"] = "10"
    elif change == "order":
        election["order"] = 2
    elif change == "bank_name":
        election["bank_account"]["bank_name"] = "Changed Bank"
    else:
        election["bank_account"]["account_name"] = "Changed Account Holder"
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(CASH).mock(
            return_value=httpx.Response(200, text=payment_response())
        )
        result = await call_tool(
            server_factory(), "workday_set_payment_elections", args
        )
    assert result["submitted"] is False
    assert result["requires_confirmation"] is True
    assert route.call_count == 1


async def test_exact_complete_payment_set_is_a_noop(server_factory, worker_response):
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(CASH).mock(
            return_value=httpx.Response(200, text=payment_response())
        )
        result = await call_tool(
            server_factory(), "workday_set_payment_elections", PAYMENT
        )
    assert result["no_change"] is True
    assert route.call_count == 1


async def test_masked_bank_values_never_establish_equality(
    server_factory, worker_response
):
    with respx.mock:
        mock_worker(worker_response)
        respx.post(CASH).mock(
            return_value=httpx.Response(
                200, text=payment_response().replace("12345678", "****5678")
            )
        )
        result = await call_tool(
            server_factory(), "workday_set_payment_elections", PAYMENT
        )
    assert result["requires_confirmation"] is True


async def test_incomplete_bank_scan_blocks_replacement_even_if_confirmed(
    server_factory, worker_response
):
    other = payment_response().replace("worker-wid-1", "somebody-else")
    other = other.replace(
        "<bsvc:Payment_Election_Enrollment_Response_Data>",
        "<bsvc:Response_Results><bsvc:Page>1</bsvc:Page><bsvc:Total_Pages>10</bsvc:Total_Pages></bsvc:Response_Results><bsvc:Payment_Election_Enrollment_Response_Data>",
    )
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(CASH).mock(
            side_effect=[
                httpx.Response(
                    200, text=soap_response(payment_elections.GET_ENROLLMENTS, "")
                )
            ]
            + [httpx.Response(200, text=other)] * 5
        )
        result = await call_tool(
            server_factory(),
            "workday_set_payment_elections",
            {**PAYMENT, "confirm_replaces_existing": True},
        )
    assert "incomplete" in result
    assert route.call_count == 6
    assert all("Submit_Payment" not in c.request.content.decode() for c in route.calls)


@pytest.mark.parametrize("masked", [False, True])
async def test_bank_merge_preserves_other_rule_group_or_refuses_masked_values(
    server_factory, worker_response, masked
):
    old = copy.deepcopy(PAYMENT)
    old["rule_groups"][0]["payment_election_rule_id"] = "payroll"
    response = payment_response(old)
    if masked:
        response = response.replace("12345678", "****5678")
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(CASH).mock(
            side_effect=[
                httpx.Response(200, text=response),
                httpx.Response(
                    200, text=staffing_response(payment_elections.SUBMIT_ENROLLMENT)
                ),
            ]
        )
        result = await call_tool(
            server_factory(),
            "workday_set_payment_elections",
            {**PAYMENT, "merge_with_current": True, "confirm_replaces_existing": True},
        )
    if masked:
        assert "masked" in result
        assert route.call_count == 1
    else:
        assert result["submitted"] is True
        payload = route.calls[1].request.content.decode()
        assert "payroll" in payload and "expenses" in payload
        assert "12345678" not in json.dumps(result)
        assert "Run_Now" not in payload


async def test_partial_batch_keeps_successful_event_and_stops(
    server_factory, worker_response
):
    fault = '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"><env:Body><env:Fault><faultcode>Validation</faultcode><faultstring>Invalid organization</faultstring></env:Fault></env:Body></env:Envelope>'
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(STAFFING).mock(
            side_effect=[
                httpx.Response(200, text=staffing_response("Change_Job")),
                httpx.Response(500, text=fault),
            ]
        )
        result = await call_tool(
            server_factory(),
            "workday_change_job",
            {
                **JOB,
                "changes": [
                    {"worker_id": str(i), "supervisory_organization_id": "SUP-NEW"}
                    for i in (1, 2, 3)
                ],
            },
        )
    assert route.call_count == 2
    assert result["results"][0]["event"]["id"] == "event-1"
    assert result["results"][1]["submitted"] is False
    assert "Invalid organization" in result["results"][1]["error"]
    assert result["results"][2]["not_attempted"] is True


async def test_preview_token_is_accepted_by_a_fresh_sandbox(
    server_factory, worker_response
):
    """Harriet may preview in one sandbox and submit the approval in another."""
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        preview = await call_tool(
            server_factory(),
            "workday_preview_write",
            {"operation": "workday_change_job", "arguments": JOB},
        )
        reset_client()
        result = await call_tool(
            server_factory(),
            "workday_change_job",
            {**JOB, "expected_state_token": preview["state_token"]},
        )
    assert result["results"][0]["submitted"] is True and route.call_count == 1


async def test_rotated_credential_invalidates_a_preview(
    server_factory, worker_response, set_env
):
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        preview = await call_tool(
            server_factory(),
            "workday_preview_write",
            {"operation": "workday_change_job", "arguments": JOB},
        )
        set_env(WORKDAY_ISU_PASSWORD="rotated")
        result = await call_tool(
            server_factory(),
            "workday_change_job",
            {**JOB, "expected_state_token": preview["state_token"]},
        )
    assert "preview and approve again" in result["results"][0]["error"]
    assert route.call_count == 0


async def test_server_keeps_no_write_record_between_calls(
    server_factory, worker_response
):
    """Duplicate suppression belongs to the host; the server holds no state."""
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        server = server_factory()
        for _ in range(2):
            result = await call_tool(server, "workday_change_job", JOB)
            assert result["results"][0]["submitted"] is True
    assert route.call_count == 2
    assert "tracking_id" not in result["results"][0]


async def test_timeout_is_reported_unknown_and_never_retried(
    server_factory, worker_response
):
    with respx.mock:
        mock_worker(worker_response)
        route = respx.post(STAFFING).mock(
            side_effect=httpx.ReadTimeout("Lost response")
        )
        result = await call_tool(server_factory(), "workday_change_job", JOB)
    outcome = result["results"][0]
    assert outcome["outcome_unknown"] is True and route.call_count == 1
    assert "event history" in outcome["error"]


@pytest.mark.parametrize("change", ["none", "state", "arguments", "query_clock"])
async def test_approved_preview_is_bound_to_state_and_arguments(
    server_factory, worker_response, change
):
    with respx.mock:

        def with_query_clock(moment):
            return worker_response.replace(
                "<wd:Response_Data>",
                "<wd:Response_Filter><wd:As_Of_Entry_DateTime>"
                f"{moment}</wd:As_Of_Entry_DateTime></wd:Response_Filter>"
                "<wd:Response_Data>",
            )

        read = mock_worker(with_query_clock("2026-10-01T08:00:00Z"))
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        server = server_factory()
        preview = await call_tool(
            server,
            "workday_preview_write",
            {"operation": "workday_change_job", "arguments": JOB},
        )
        args = copy.deepcopy(JOB)
        if change == "state":
            read.mock(
                return_value=httpx.Response(
                    200, text=worker_response.replace("SUP-ENG", "SUP-OTHER")
                )
            )
        elif change == "arguments":
            args["changes"][0]["supervisory_organization_id"] = "UNAPPROVED"
        elif change == "query_clock":
            read.mock(
                return_value=httpx.Response(
                    200, text=with_query_clock("2026-10-01T08:05:00Z")
                )
            )
        result = await call_tool(
            server,
            "workday_change_job",
            {**args, "expected_state_token": preview["state_token"]},
        )
    unchanged = change in {"none", "query_clock"}
    assert result["results"][0]["submitted"] is unchanged
    assert route.call_count == (1 if unchanged else 0)


@pytest.mark.parametrize(
    "operation,args",
    [
        (
            "workday_create_pre_hire",
            {
                "first_name": "Test",
                "last_name": "Person",
                "email": "test@example.com",
                "force": True,
            },
        ),
        (
            "workday_hire_employee",
            {
                "organization_id": "SUP",
                "hire_date": "2026-10-01",
                "pre_hire_wid": "APP",
            },
        ),
        (
            "workday_terminate_employee",
            {
                "worker_id": "21001",
                "termination_date": "2026-10-01",
                "primary_reason_id": "Voluntary",
            },
        ),
        ("workday_set_payment_elections", {**PAYMENT, "currency": "EUR"}),
    ],
)
async def test_preview_uses_real_builder_without_sending_a_write(
    server_factory, worker_response, operation, args
):
    with respx.mock:
        mock_worker(worker_response)
        respx.post(CASH).mock(return_value=httpx.Response(200, text=payment_response()))
        result = await call_tool(
            server_factory(),
            "workday_preview_write",
            {"operation": operation, "arguments": args},
        )
        calls = list(respx.calls)
    assert result["would_submit"] is True
    assert all(
        "_Request" in c.request.content.decode()
        and (
            "Get_Workers" in c.request.content.decode()
            or "Get_Payment_Election_Enrollments" in c.request.content.decode()
        )
        for c in calls
    )
    assert "isu-password" not in json.dumps(result)
    assert "12345678" not in json.dumps(result)


async def test_preview_reports_no_change(server_factory, worker_response):
    args = {
        **JOB,
        "changes": [{"worker_id": "21001", "supervisory_organization_id": "SUP-ENG"}],
    }
    with respx.mock:
        mock_worker(worker_response)
        result = await call_tool(
            server_factory(),
            "workday_preview_write",
            {"operation": "workday_change_job", "arguments": args},
        )
    assert result["would_submit"] is False


async def test_preview_cannot_bypass_tool_allowlist(server_factory, set_env):
    set_env(WORKDAY_ENABLED_TOOLS="workday_preview_write")
    result = await call_tool(
        server_factory(),
        "workday_preview_write",
        {"operation": "workday_hire_employee", "arguments": {}},
    )
    assert "disabled" in result


@pytest.mark.parametrize("effective_date", ["", "2026-02-30", "20261001", "yesterday"])
async def test_dates_fail_before_network(server_factory, effective_date):
    with respx.mock:
        result = await call_tool(
            server_factory(),
            "workday_change_job",
            {**JOB, "effective_date": effective_date},
        )
    assert "YYYY-MM-DD" in result


def test_credentials_are_fully_redacted_and_account_suffix_collisions_are_distinct():
    xml = "<wsse:Username>isu-user</wsse:Username><wsse:Password>super-secret</wsse:Password><b:Roll_Number>123456789</b:Roll_Number>"
    result = redact_xml(xml)
    assert (
        "isu-user" not in result
        and "super-secret" not in result
        and "123456789" not in result
    )
    assert redact_data({"Password": "secret"}) == {"Password": "[REDACTED]"}
    assert account_fingerprint("12345678") != account_fingerprint("99995678")
    assert account_fingerprint("****5678") == ""
