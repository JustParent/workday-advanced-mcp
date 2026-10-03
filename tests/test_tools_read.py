"""Read tools, end to end over a mocked tenant."""

from __future__ import annotations

import httpx
import respx

from conftest import call_tool, service_url, soap_response
from workday_advanced_mcp.client import (
    SERVICE_CASH_MANAGEMENT,
    SERVICE_HUMAN_RESOURCES,
    SERVICE_INTEGRATIONS,
)

HR_URL = service_url(SERVICE_HUMAN_RESOURCES)

EMPTY_WORKERS = soap_response("Get_Workers", "<wd:Response_Data/>")

ORGANIZATIONS = soap_response(
    "Get_Organizations",
    """
    <wd:Response_Results><wd:Page>1</wd:Page><wd:Total_Pages>1</wd:Total_Pages></wd:Response_Results>
    <wd:Response_Data>
      <wd:Organization>
        <wd:Organization_Reference wd:Descriptor="R&amp;D">
          <wd:ID wd:type="Cost_Center_Reference_ID">CC_100</wd:ID>
        </wd:Organization_Reference>
        <wd:Organization_Data><wd:Organization_Name>R&amp;D</wd:Organization_Name></wd:Organization_Data>
      </wd:Organization>
      <wd:Organization>
        <wd:Organization_Reference wd:Descriptor="Sales">
          <wd:ID wd:type="Cost_Center_Reference_ID">CC_200</wd:ID>
        </wd:Organization_Reference>
        <wd:Organization_Data><wd:Organization_Name>Sales</wd:Organization_Name></wd:Organization_Data>
      </wd:Organization>
    </wd:Response_Data>
    """,
)

EVENT = soap_response(
    "Get_Event_Details",
    """
    <wd:Response_Data>
      <wd:Event>
        <wd:Event_Reference wd:Descriptor="Change Job: Jane Doe">
          <wd:ID wd:type="WID">event-1</wd:ID>
        </wd:Event_Reference>
        <wd:Event_Detail_Data>
          <wd:Creation_Date>2026-04-01</wd:Creation_Date>
          <wd:Event_State_Reference wd:Descriptor="In Progress">
            <wd:ID wd:type="WID">state-1</wd:ID>
          </wd:Event_State_Reference>
          <wd:Awaiting_Task_Data>
            <wd:Task_Reference wd:Descriptor="Approval by Manager">
              <wd:ID wd:type="WID">task-1</wd:ID>
            </wd:Task_Reference>
          </wd:Awaiting_Task_Data>
        </wd:Event_Detail_Data>
      </wd:Event>
    </wd:Response_Data>
    """,
)

ENROLLMENTS = soap_response(
    "Get_Payment_Election_Enrollments",
    """
    <wd:Payment_Election_Enrollment_Response_Data>
      <wd:Payment_Election_Enrollment>
        <wd:Payment_Election_Enrollment_Data>
          <wd:Role_ID_Reference wd:Descriptor="Jane Doe">
            <wd:ID wd:type="WID">worker-wid-1</wd:ID>
          </wd:Role_ID_Reference>
          <wd:Country_Reference><wd:ID wd:type="ISO_3166-1_Alpha-2_Code">GB</wd:ID></wd:Country_Reference>
          <wd:Currency_Reference><wd:ID wd:type="Currency_ID">GBP</wd:ID></wd:Currency_Reference>
          <wd:Payment_Election_Rule_Group_Data>
            <wd:Payment_Election_Rule_Reference><wd:ID wd:type="WID">rule-1</wd:ID></wd:Payment_Election_Rule_Reference>
            <wd:Payment_Election_Data>
              <wd:Order>1</wd:Order>
              <wd:Country_Reference><wd:ID wd:type="ISO_3166-1_Alpha-2_Code">GB</wd:ID></wd:Country_Reference>
              <wd:Currency_Reference><wd:ID wd:type="Currency_ID">GBP</wd:ID></wd:Currency_Reference>
              <wd:Payment_Type_Reference><wd:ID wd:type="Payment_Type_ID">Direct_Deposit</wd:ID></wd:Payment_Type_Reference>
              <wd:Distribution_Balance>true</wd:Distribution_Balance>
              <wd:Worker_Bank_Account_Data>
                <wd:Bank_Name>Example Bank</wd:Bank_Name>
                <wd:Bank_ID_Number>40-47-84</wd:Bank_ID_Number>
                <wd:Account_Number>12345678</wd:Account_Number>
              </wd:Worker_Bank_Account_Data>
            </wd:Payment_Election_Data>
          </wd:Payment_Election_Rule_Group_Data>
        </wd:Payment_Election_Enrollment_Data>
      </wd:Payment_Election_Enrollment>
    </wd:Payment_Election_Enrollment_Response_Data>
    """,
)


async def test_get_worker_surfaces_what_a_write_needs(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        worker = await call_tool(
            server_factory(), "workday_get_worker", {"worker_id": "21001"}
        )

    assert worker["name"] == "Jane Doe"
    assert worker["wid"] == "worker-wid-1"
    assert worker["primary_email"] == "jane.doe@example.com"
    # The position id is what Change_Organization_Assignments addresses.
    assert worker["position"]["position_id"] == "POS-100"
    assert worker["supervisory_organization"]["id"] == "SUP-ENG"
    assert worker["cost_center"]["id"] == "CC_100"
    assert worker["status"]["active"] is True


async def test_find_worker_by_employee_id(server_factory, worker_response) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        found = await call_tool(
            server_factory(), "workday_find_worker", {"query": "21001"}
        )

    assert found["match_type"] == "Employee_ID"
    assert found["candidates"][0]["worker_id"] == "21001"


async def test_find_worker_by_email_scans_and_reports_completeness(
    server_factory, worker_response
) -> None:
    """Workday cannot filter workers by email, so the scan must be honest."""
    with respx.mock:
        respx.post(HR_URL).mock(
            side_effect=[
                httpx.Response(200, text=worker_response),
                httpx.Response(200, text=EMPTY_WORKERS),
            ]
        )
        found = await call_tool(
            server_factory(),
            "workday_find_worker",
            {"query": "jane.doe@example.com"},
        )

    assert found["match_type"] == "email"
    assert found["scan_complete"] is True
    assert found["candidates"][0]["worker_id"] == "21001"


async def test_an_exhausted_scan_says_so_rather_than_reporting_nothing(
    server_factory, worker_response
) -> None:
    """Hitting the page cap must not be reported as "no such worker"."""
    # Workday says there are five pages, so a short page is not the end.
    many_pages = worker_response.replace(
        "<wd:Response_Data>",
        "<wd:Response_Results><wd:Page>1</wd:Page>"
        "<wd:Total_Pages>5</wd:Total_Pages></wd:Response_Results><wd:Response_Data>",
        1,
    )
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=many_pages))
        found = await call_tool(
            server_factory(),
            "workday_find_worker",
            {"query": "nobody@example.com", "max_scan_pages": 1},
        )

    assert found["candidates"] == []
    assert found["scan_complete"] is False
    assert "page cap" in found["note"]


async def test_an_unknown_worker_is_reported_not_raised(server_factory) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=EMPTY_WORKERS))
        result = await call_tool(
            server_factory(), "workday_get_worker", {"worker_id": "99999"}
        )
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "99999" in result


async def test_list_organizations_filters_locally(server_factory) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=ORGANIZATIONS))
        result = await call_tool(
            server_factory(),
            "workday_list_organizations",
            {"organization_type": "Cost_Center", "query": "sales"},
        )

    assert result["count"] == 1
    assert result["entries"][0]["id"] == "CC_200"
    assert result["entries"][0]["id_type"] == "Cost_Center_Reference_ID"


async def test_list_organizations_rejects_an_unknown_type(server_factory) -> None:
    result = await call_tool(
        server_factory(),
        "workday_list_organizations",
        {"organization_type": "Nonsense"},
    )
    assert isinstance(result, str)
    assert "Supported" in result


async def test_get_event_reports_state_and_awaiting_tasks(server_factory) -> None:
    with respx.mock:
        respx.post(service_url(SERVICE_INTEGRATIONS)).mock(
            return_value=httpx.Response(200, text=EVENT)
        )
        event = await call_tool(
            server_factory(), "workday_get_event", {"event_id": "event-1"}
        )

    assert event["state"] == "In Progress"
    assert event["awaiting_tasks"][0]["task"]["descriptor"] == "Approval by Manager"


async def test_payment_elections_are_returned_masked(
    server_factory, worker_response
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        respx.post(service_url(SERVICE_CASH_MANAGEMENT)).mock(
            return_value=httpx.Response(200, text=ENROLLMENTS)
        )
        result = await call_tool(
            server_factory(), "workday_get_payment_elections", {"worker_id": "21001"}
        )

    import json

    election = result["enrollments"][0]["rule_groups"][0]["elections"][0]
    assert election["bank_account"]["account_number_masked"] == "****5678"
    assert election["bank_account"]["bank_name"] == "Example Bank"
    assert election["distribution_balance"] is True
    # The full number must not survive anywhere in the payload.
    assert "12345678" not in json.dumps(result)
