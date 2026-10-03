"""Write tools, end to end over a mocked tenant.

These assert the safety behaviour, not just the happy path: what gets merged,
what is refused, and what is never sent.
"""

from __future__ import annotations

import httpx
import respx

from conftest import call_tool, service_url, soap_response
from workday_advanced_mcp.client import (
    SERVICE_CASH_MANAGEMENT,
    SERVICE_HUMAN_RESOURCES,
    SERVICE_RECRUITING,
    SERVICE_STAFFING,
)

HR_URL = service_url(SERVICE_HUMAN_RESOURCES)
STAFFING_URL = service_url(SERVICE_STAFFING)

EVENT_XML = (
    '<wd:Event_Reference><wd:ID wd:type="WID">event-1</wd:ID></wd:Event_Reference>'
)


def staffing_response(operation: str) -> str:
    return soap_response(operation, EVENT_XML)


async def test_organization_assignments_carry_forward_what_was_not_changed(
    server_factory, worker_response
) -> None:
    """Workday replaces every type in the request, so the rest must be resent."""
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Change_Organization_Assignments")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_change_organization_assignments",
            {
                "changes": [
                    {"worker_id": "21001", "assignments": {"Cost_Center": ["CC_200"]}}
                ],
                "effective_date": "2026-04-01",
            },
        )

    body = staffing.calls[0].request.content.decode()
    assert "CC_200" in body
    # The company the worker was already in is resent, not dropped.
    assert "CO_1" in body
    # The supervisory organization is not assignable here; sending it would fault.
    assert "SUP-ENG" not in body

    outcome = result["results"][0]
    assert outcome["submitted"] is True
    assert outcome["event"]["id"] == "event-1"
    assert outcome["changes"]["Cost_Center"] == {"from": ["CC_100"], "to": ["CC_200"]}


async def test_an_assignment_that_already_matches_is_not_submitted(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Change_Organization_Assignments")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_change_organization_assignments",
            {
                "changes": [
                    {"worker_id": "21001", "assignments": {"Cost_Center": ["CC_100"]}}
                ],
                "effective_date": "2026-04-01",
            },
        )

    assert result["results"][0]["no_change"] is True
    assert staffing.call_count == 0


async def test_merge_can_be_turned_off_explicitly(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Change_Organization_Assignments")
            )
        )
        await call_tool(
            server_factory(),
            "workday_change_organization_assignments",
            {
                "changes": [
                    {"worker_id": "21001", "assignments": {"Cost_Center": ["CC_200"]}}
                ],
                "effective_date": "2026-04-01",
                "merge_with_current": False,
            },
        )

    body = staffing.calls[0].request.content.decode()
    assert "CC_200" in body
    assert "CO_1" not in body


async def test_supervisory_moves_are_pointed_at_the_job_change_tool(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        result = await call_tool(
            server_factory(),
            "workday_change_organization_assignments",
            {
                "changes": [
                    {"worker_id": "21001", "assignments": {"Supervisory": ["SUP-NEW"]}}
                ],
                "effective_date": "2026-04-01",
            },
        )
    assert isinstance(result, str)
    assert "workday_change_job" in result


async def test_change_job_sends_the_supervisory_organization_and_reason(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        result = await call_tool(
            server_factory(),
            "workday_change_job",
            {
                "changes": [
                    {"worker_id": "21001", "supervisory_organization_id": "SUP-NEW"}
                ],
                "effective_date": "2026-04-01",
                "reason_id": "Transfer",
            },
        )

    body = staffing.calls[0].request.content.decode()
    assert "Supervisory_Organization_Reference" in body
    assert "SUP-NEW" in body
    assert "Transfer" in body

    outcome = result["results"][0]
    assert outcome["changes"]["supervisory_organization_id"] == {
        "from": "SUP-ENG",
        "to": "SUP-NEW",
    }
    # Submitted without auto-complete, so the worker has not moved yet.
    assert "workday_get_event" in outcome["verification"]["note"]


async def test_change_job_without_a_reason_is_refused(server_factory) -> None:
    result = await call_tool(
        server_factory(),
        "workday_change_job",
        {
            "changes": [
                {"worker_id": "21001", "supervisory_organization_id": "SUP-NEW"}
            ],
            "effective_date": "2026-04-01",
            "reason_id": "",
        },
    )
    assert isinstance(result, str)
    assert "workday_list_reference_ids" in result


async def test_a_batch_larger_than_the_limit_is_refused(server_factory) -> None:
    result = await call_tool(
        server_factory(),
        "workday_change_job",
        {
            "changes": [
                {"worker_id": str(i), "business_title": "x"} for i in range(30)
            ],
            "effective_date": "2026-04-01",
            "reason_id": "Transfer",
        },
    )
    assert isinstance(result, str)
    assert "smaller batches" in result


async def test_terminating_an_already_terminated_worker_is_refused(
    server_factory, worker_response
) -> None:
    terminated = worker_response.replace(
        "<wd:Terminated>false</wd:Terminated>",
        "<wd:Terminated>true</wd:Terminated>"
        "<wd:Termination_Date>2025-12-31</wd:Termination_Date>",
    )
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=terminated))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Terminate_Employee")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_terminate_employee",
            {
                "worker_id": "21001",
                "termination_date": "2026-05-31",
                "primary_reason_id": "Voluntary",
            },
        )

    assert isinstance(result, str)
    assert "already terminated" in result
    assert staffing.call_count == 0


async def test_termination_submits_and_reports_the_event(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Terminate_Employee")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_terminate_employee",
            {
                "worker_id": "21001",
                "termination_date": "2026-05-31",
                "primary_reason_id": "Voluntary",
                "last_day_of_work": "2026-05-31",
            },
        )

    assert result["submitted"] is True
    assert result["event"]["id"] == "event-1"
    assert "Last_Day_of_Work" in staffing.calls[0].request.content.decode()


async def test_auto_complete_is_refused_unless_the_deployment_allows_it(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Terminate_Employee")
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_terminate_employee",
            {
                "worker_id": "21001",
                "termination_date": "2026-05-31",
                "primary_reason_id": "Voluntary",
                "auto_complete": True,
            },
        )

    assert isinstance(result, str)
    assert "WORKDAY_ALLOW_AUTO_COMPLETE" in result
    assert staffing.call_count == 0


async def test_auto_complete_verifies_by_reading_the_worker_back(
    server_factory, worker_response, set_env
) -> None:
    set_env(WORKDAY_ALLOW_AUTO_COMPLETE="true")
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        result = await call_tool(
            server_factory(),
            "workday_change_job",
            {
                "changes": [
                    {"worker_id": "21001", "supervisory_organization_id": "SUP-NEW"}
                ],
                "effective_date": "2026-04-01",
                "reason_id": "Transfer",
                "auto_complete": True,
            },
        )

    outcome = result["results"][0]
    assert outcome["verification"]["verified"] is False
    assert (
        outcome["verification"]["mismatches"]["supervisory_organization_id"]["actual"]
        == "SUP-ENG"
    )
    assert "supervisory_organization" in outcome["verification"]


async def test_hiring_somebody_who_already_exists_is_refused(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(200, text=staffing_response("Hire_Employee"))
        )
        result = await call_tool(
            server_factory(),
            "workday_hire_employee",
            {
                "organization_id": "SUP-ENG",
                "hire_date": "2026-06-01",
                "pre_hire_wid": "prehire-1",
                "employee_id": "21001",
            },
        )

    assert result["duplicate"] is True
    assert result["submitted"] is False
    assert staffing.call_count == 0


async def test_creating_a_duplicate_pre_hire_is_refused(server_factory) -> None:
    applicants = soap_response(
        "Get_Applicants",
        """
        <wd:Response_Data>
          <wd:Applicant>
            <wd:Applicant_Reference wd:Descriptor="Ada Lovelace">
              <wd:ID wd:type="WID">prehire-1</wd:ID>
            </wd:Applicant_Reference>
            <wd:Applicant_Data><wd:Personal_Data><wd:Contact_Data>
              <wd:Email_Address_Data><wd:Email_Address>ada@example.com</wd:Email_Address></wd:Email_Address_Data>
            </wd:Contact_Data></wd:Personal_Data></wd:Applicant_Data>
          </wd:Applicant>
        </wd:Response_Data>
        """,
    )
    with respx.mock:
        route = respx.post(service_url(SERVICE_RECRUITING)).mock(
            return_value=httpx.Response(200, text=applicants)
        )
        result = await call_tool(
            server_factory(),
            "workday_create_pre_hire",
            {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
        )

    assert result["duplicate"] is True
    assert result["created"] is False
    # Only the lookup happened; nothing was written.
    assert route.call_count == 1


async def test_payment_elections_refuse_to_drop_an_account_without_confirmation(
    server_factory, worker_response
) -> None:
    from test_tools_read import ENROLLMENTS

    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        cash = respx.post(service_url(SERVICE_CASH_MANAGEMENT)).mock(
            return_value=httpx.Response(200, text=ENROLLMENTS)
        )
        result = await call_tool(
            server_factory(),
            "workday_set_payment_elections",
            {
                "worker_id": "21001",
                "country": "GB",
                "currency": "GBP",
                "rule_groups": [
                    {
                        "payment_election_rule_id": "rule-1",
                        "elections": [
                            {
                                "order": 1,
                                "payment_type_id": "Direct_Deposit",
                                "distribution_balance": True,
                                "bank_account": {
                                    "account_number": "87654321",
                                    "bank_id_number": "40-47-84",
                                },
                            }
                        ],
                    }
                ],
            },
        )

    assert result["submitted"] is False
    assert result["requires_confirmation"] is True
    assert result["accounts_removed"]
    # Only the read happened.
    assert cash.call_count == 1


async def test_payment_elections_submit_once_the_replacement_is_confirmed(
    server_factory, worker_response
) -> None:
    from test_tools_read import ENROLLMENTS

    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        cash = respx.post(service_url(SERVICE_CASH_MANAGEMENT)).mock(
            side_effect=[
                httpx.Response(200, text=ENROLLMENTS),
                httpx.Response(
                    200,
                    text=soap_response("Submit_Payment_Election_Enrollment", EVENT_XML),
                ),
            ]
        )
        result = await call_tool(
            server_factory(),
            "workday_set_payment_elections",
            {
                "worker_id": "21001",
                "country": "GB",
                "currency": "GBP",
                "confirm_replaces_existing": True,
                "rule_groups": [
                    {
                        "payment_election_rule_id": "rule-1",
                        "elections": [
                            {
                                "order": 1,
                                "payment_type_id": "Direct_Deposit",
                                "distribution_balance": True,
                                "bank_account": {
                                    "account_number": "87654321",
                                    "bank_id_number": "40-47-84",
                                },
                            }
                        ],
                    }
                ],
            },
        )

    import json

    assert result["submitted"] is True
    assert cash.call_count == 2
    # The account number goes to Workday but never comes back to the caller.
    assert "87654321" in cash.calls[1].request.content.decode()
    assert "87654321" not in json.dumps(result)


async def test_unchanged_payment_elections_are_not_resubmitted(
    server_factory, worker_response
) -> None:
    from test_tools_read import ENROLLMENTS

    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        cash = respx.post(service_url(SERVICE_CASH_MANAGEMENT)).mock(
            return_value=httpx.Response(200, text=ENROLLMENTS)
        )
        result = await call_tool(
            server_factory(),
            "workday_set_payment_elections",
            {
                "worker_id": "21001",
                "country": "GB",
                "currency": "GBP",
                "rule_groups": [
                    {
                        "payment_election_rule_id": "rule-1",
                        "elections": [
                            {
                                "order": 1,
                                "payment_type_id": "Direct_Deposit",
                                "distribution_balance": True,
                                "bank_account": {
                                    "account_number": "12345678",
                                    "bank_id_number": "40-47-84",
                                    "bank_name": "Example Bank",
                                },
                            }
                        ],
                    }
                ],
            },
        )

    assert result["no_change"] is True
    assert cash.call_count == 1


async def test_preview_sends_nothing_to_workday(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        staffing = respx.post(STAFFING_URL).mock(
            return_value=httpx.Response(200, text=staffing_response("Change_Job"))
        )
        result = await call_tool(
            server_factory(),
            "workday_preview_write",
            {
                "operation": "workday_change_job",
                "arguments": {
                    "changes": [
                        {"worker_id": "21001", "supervisory_organization_id": "SUP-NEW"}
                    ],
                    "effective_date": "2026-04-01",
                    "reason_id": "Transfer",
                },
            },
        )

    assert staffing.call_count == 0
    plan = result["result"]["results"][0]
    assert plan["changes"]["supervisory_organization_id"] == {
        "from": "SUP-ENG",
        "to": "SUP-NEW",
    }
    assert plan["would_submit"] is True


async def test_preview_rejects_an_operation_it_cannot_plan(server_factory) -> None:
    result = await call_tool(
        server_factory(),
        "workday_preview_write",
        {"operation": "workday_not_a_tool"},
    )
    assert isinstance(result, str)
    assert "unknown write" in result
