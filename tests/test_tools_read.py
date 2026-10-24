"""Read tools, end to end over a mocked tenant."""

from __future__ import annotations

import httpx
import respx

from conftest import (
    call_tool,
    service_url,
    soap_fault,
    soap_response,
    soap_unknown_id_fault,
)
from workday_advanced_mcp.client import (
    SERVICE_CASH_MANAGEMENT,
    SERVICE_HUMAN_RESOURCES,
    SERVICE_INTEGRATIONS,
    SERVICE_STAFFING,
)
from workday_advanced_mcp.soap import parse_envelope

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
    assert worker["position"]["employee_type"]["id"] == "Regular"
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


# A live tenant faults a lookup of an id it does not hold, even with
# Skip_Non_Existing_Instances set, instead of returning an empty page.
WORKER_XPATH = (
    "/bsvc:Get_Workers_Request[1]/bsvc:Request_References[1]/bsvc:Worker_Reference[1]"
)


def unknown_worker(id_type: str, value: str = "99999") -> httpx.Response:
    return httpx.Response(500, text=soap_unknown_id_fault(value, id_type, WORKER_XPATH))


def worker_lookup(**by_id_type: httpx.Response):
    """Answer each Get_Workers lookup according to the id type it asks for."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = parse_envelope(request.content.decode()).body["Get_Workers_Request"]
        reference = body["Request_References"]["Worker_Reference"]
        return by_id_type[reference["ID"]["@type"]]

    return handle


async def test_find_worker_tries_the_next_id_type_when_workday_faults_an_unknown_id(
    server_factory, worker_response
) -> None:
    with respx.mock:
        route = respx.post(HR_URL).mock(
            side_effect=worker_lookup(
                Employee_ID=unknown_worker("Employee_ID", "worker-wid-1"),
                WID=httpx.Response(200, text=worker_response),
            )
        )
        found = await call_tool(
            server_factory(), "workday_find_worker", {"query": "worker-wid-1"}
        )

    assert found["match_type"] == "WID"
    assert found["candidates"][0]["worker_id"] == "21001"
    assert route.call_count == 2


async def test_find_worker_reports_no_match_when_every_id_type_is_unknown(
    server_factory,
) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(
            side_effect=worker_lookup(
                Employee_ID=unknown_worker("Employee_ID"),
                WID=unknown_worker("WID"),
                Contingent_Worker_ID=unknown_worker("Contingent_Worker_ID"),
            )
        )
        found = await call_tool(
            server_factory(), "workday_find_worker", {"query": "99999"}
        )

    assert found["match_type"] == "none"
    assert found["candidates"] == []


async def test_an_unknown_worker_id_is_reported_as_not_found(server_factory) -> None:
    with respx.mock:
        respx.post(HR_URL).mock(
            side_effect=worker_lookup(Employee_ID=unknown_worker("Employee_ID"))
        )
        result = await call_tool(
            server_factory(), "workday_get_worker", {"worker_id": "99999"}
        )

    assert result.startswith("Error: No Workday worker has Employee_ID '99999'")


async def test_an_unknown_organization_is_reported_as_not_found(server_factory) -> None:
    fault = soap_unknown_id_fault(
        "NOPE",
        "Organization_Reference_ID",
        "/bsvc:Get_Organizations_Request[1]/bsvc:Request_References[1]"
        "/bsvc:Organization_Reference[1]",
    )
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(500, text=fault))
        result = await call_tool(
            server_factory(), "workday_get_organization", {"organization_id": "NOPE"}
        )

    assert "Organization 'NOPE' was not found" in result


async def test_an_unknown_position_id_lists_no_positions(server_factory) -> None:
    fault = soap_unknown_id_fault(
        "NOPE",
        "Position_ID",
        "/bsvc:Get_Positions_Request[1]/bsvc:Request_References[1]"
        "/bsvc:Positions_Reference[1]",
    )
    with respx.mock:
        respx.post(service_url(SERVICE_STAFFING)).mock(
            return_value=httpx.Response(500, text=fault)
        )
        result = await call_tool(
            server_factory(), "workday_list_positions", {"position_id": "NOPE"}
        )

    assert result["positions"] == []


async def test_an_unknown_event_id_is_reported_as_no_event(server_factory) -> None:
    fault = soap_unknown_id_fault(
        "NOPE",
        "WID",
        "/bsvc:Get_Event_Details_Request[1]/bsvc:Request_References[1]"
        "/bsvc:Event_Reference[1]",
    )
    with respx.mock:
        respx.post(service_url(SERVICE_INTEGRATIONS)).mock(
            return_value=httpx.Response(500, text=fault)
        )
        result = await call_tool(
            server_factory(), "workday_get_event", {"event_id": "NOPE"}
        )

    assert "Workday returned no event for 'NOPE'" in result


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


async def test_payment_elections_scan_when_workday_faults_the_direct_lookup(
    server_factory, worker_response
) -> None:
    """A live tenant rejects a worker WID used as an enrollment reference."""
    message = (
        "Invalid instance 1$1 for "
        "Payment_Election_Enrollment_Request_References__Secured_--IS (2$2)"
    )
    with respx.mock:
        respx.post(HR_URL).mock(return_value=httpx.Response(200, text=worker_response))
        cash = respx.post(service_url(SERVICE_CASH_MANAGEMENT)).mock(
            side_effect=[
                httpx.Response(500, text=soap_fault(message, message)),
                httpx.Response(200, text=ENROLLMENTS),
            ]
        )
        result = await call_tool(
            server_factory(), "workday_get_payment_elections", {"worker_id": "21001"}
        )

    assert cash.call_count == 2
    assert result["scan_complete"] is True
    election = result["enrollments"][0]["rule_groups"][0]["elections"][0]
    assert election["bank_account"]["account_number_masked"] == "****5678"


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
