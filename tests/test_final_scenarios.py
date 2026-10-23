"""Complete lifecycle acceptance scenarios and operational recovery checks."""

from __future__ import annotations

import copy
import sys

import httpx
import pytest
import respx

from conftest import call_tool, service_url, soap_auth_fault, soap_response
from test_lifecycle import DATE, HOME, HR, org, org_response, with_addresses
from test_regressions import JOB, PAYMENT, payment_response
from test_schema_contracts import assert_schema
from test_tools_write import staffing_response
from workday_advanced_mcp.client import WorkdayClient
from workday_advanced_mcp.operations import organizations
from workday_advanced_mcp.references import reference
from workday_advanced_mcp.soap import parse_envelope


def inactive_org(inactive=False):
    entry = org("OLD", inactive=inactive)
    entry["Organization_Reference"]["ID"].append({"type": "WID", "value": "org-wid"})
    return entry


async def test_empty_org_inactivation_is_confirmed_immediate_and_verified(
    server_factory,
):
    inactive = False
    sent = []

    def handle(request):
        nonlocal inactive
        body = parse_envelope(request.content.decode()).body
        if "Organization_Inactivate" in body:
            inactive = True
            sent.append(body["Organization_Inactivate"])
            return httpx.Response(
                200,
                text='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>',
            )
        if "Get_Workers_Request" in body:
            return httpx.Response(200, text=soap_response("Get_Workers", ""))
        return httpx.Response(200, text=org_response(inactive_org(inactive)))

    with respx.mock:
        respx.post(HR).mock(side_effect=handle)
        server = server_factory()
        args = {"organization_id": "OLD", "effective_date": DATE}
        preview = await call_tool(
            server,
            "workday_preview_write",
            {
                "operation": "workday_inactivate_supervisory_organization",
                "arguments": args,
            },
        )
        assert preview["would_submit"] and not sent
        confirmation = await call_tool(
            server, "workday_inactivate_supervisory_organization", args
        )
        assert confirmation["requires_confirmation"] and not sent
        result = await call_tool(
            server,
            "workday_inactivate_supervisory_organization",
            {**args, "confirm": True, "expected_state_token": preview["state_token"]},
        )
    assert result["verification"]["verified"] is True
    assert sent[0]["Remove_All_Role_Assignments"] == "false"
    assert sent[0]["Keep_in_Hierarchy"] == "true"


@pytest.mark.parametrize("block", ["children", "members", "inactive", "wrong_type"])
async def test_inactivation_guards(server_factory, worker_response, block):
    entry = inactive_org(block == "inactive")
    if block == "children":
        entry["Organization_Data"]["Hierarchy_Data"] = {
            "Subordinate_Organization_Reference": reference(
                "Organization_Reference_ID", "CHILD"
            )
        }
    if block == "wrong_type":
        entry["Organization_Data"]["Organization_Type_Reference"] = reference(
            "Organization_Type_ID", "Company"
        )

    def handle(request):
        assert "Organization_Inactivate" not in request.content.decode()
        return httpx.Response(
            200,
            text=worker_response
            if "Get_Workers_Request" in request.content.decode()
            else org_response(entry),
        )

    with respx.mock:
        respx.post(HR).mock(side_effect=handle)
        result = await call_tool(
            server_factory(),
            "workday_inactivate_supervisory_organization",
            {"organization_id": "OLD", "effective_date": DATE, "confirm": True},
        )
    if block == "inactive":
        assert result["no_change"]
    else:
        assert result.startswith("Error:")


def test_inactivation_uses_published_legacy_schema():
    assert_schema(
        "Human_Resources",
        "Inactivate_Organization",
        organizations.build_inactivate_organization(
            "org-wid", DATE, keep_in_hierarchy=False, remove_role_assignments=True
        ),
    )


@pytest.mark.parametrize("matches", [True, False])
async def test_completed_payment_write_checks_all_election_fields(
    server_factory, worker_response, set_env, matches
):
    set_env(WORKDAY_ALLOW_AUTO_COMPLETE="true")
    target = copy.deepcopy(PAYMENT)
    target["rule_groups"][0]["elections"][0]["bank_account"]["account_number"] = (
        "87654321"
    )
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=worker_response))
        respx.post(service_url("Cash_Management")).mock(
            side_effect=[
                httpx.Response(200, text=payment_response()),
                httpx.Response(
                    200, text=staffing_response("Submit_Payment_Election_Enrollment")
                ),
                httpx.Response(
                    200, text=payment_response(target if matches else PAYMENT)
                ),
            ]
        )
        result = await call_tool(
            server_factory(),
            "workday_set_payment_elections",
            {**target, "auto_complete": True, "confirm_replaces_existing": True},
        )
    assert result["verification"]["verified"] is matches


async def test_public_home_address_is_made_private_even_if_fields_match(
    server_factory, worker_response
):
    with respx.mock:
        route = respx.post(HR).mock(
            side_effect=[
                httpx.Response(
                    200,
                    text=with_addresses(
                        worker_response, HOME.replace('Public="false"', 'Public="true"')
                    ),
                ),
                httpx.Response(
                    200,
                    text=staffing_response(
                        "Maintain_Contact_Information_for_Person_Event"
                    ),
                ),
            ]
        )
        result = await call_tool(
            server_factory(),
            "workday_update_home_address",
            {
                "worker_id": "21001",
                "effective_date": DATE,
                "address": {"postal_code": "N1 1AA"},
            },
        )
    assert (
        result["submitted"]
        and 'Public="false"' in route.calls[-1].request.content.decode()
    )


async def test_termination_refuses_hidden_employment_state(
    server_factory, worker_response
):
    hidden = worker_response.replace("<wd:Active>true</wd:Active>", "")
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=hidden))
        result = await call_tool(
            server_factory(),
            "workday_terminate_employee",
            {
                "worker_id": "21001",
                "termination_date": DATE,
                "primary_reason_id": "Voluntary",
            },
        )
    assert "not visible" in result


@pytest.mark.parametrize(
    "status,body",
    [(401, ""), (500, soap_auth_fault()), (200, "not XML")],
    ids=["http-401", "authentication-fault", "malformed-body"],
)
async def test_failed_response_preserves_write_safety(
    server_factory, worker_response, status, body
):
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=worker_response))
        route = respx.post(service_url("Staffing")).mock(
            return_value=httpx.Response(status, text=body)
        )
        result = await call_tool(server_factory(), "workday_change_job", JOB)
    assert not result["results"][0]["submitted"] and route.call_count == 1
    assert result["results"][0]["outcome_unknown"] is (status == 200)


@pytest.mark.parametrize("value,expected", [("bad", 1.0), ("-1", 0.0), ("200", 10.0)])
def test_retry_delay_is_bounded(value, expected):
    assert (
        WorkdayClient._retry_delay(
            httpx.Response(429, headers={"Retry-After": value}), 0
        )
        == expected
    )


def test_diagnostics_handles_missing_config_proxy_and_allowlist(
    monkeypatch, capsys, set_env
):
    from workday_advanced_mcp.server import main

    set_env(
        WORKDAY_HOST="",
        WORKDAY_TENANT="",
        WORKDAY_PROXY_URL="http://127.0.0.1:9999",
        WORKDAY_ENABLED_TOOLS="workday_get_worker",
    )
    monkeypatch.setattr(sys, "argv", ["workday-advanced-mcp", "--test"])
    main()
    text = capsys.readouterr().out
    assert (
        "Endpoint: NOT SET" in text
        and "Proxy: configured" in text
        and "Tool allowlist" in text
    )
