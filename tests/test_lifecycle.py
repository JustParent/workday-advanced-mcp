"""Organization hierarchy, role, joiner, address and event integration tests."""

from __future__ import annotations

import copy

import httpx
import pytest
import respx

from conftest import call_tool, service_url
from test_regressions import xml_response
from test_schema_contracts import assert_schema
from test_tools_read import EVENT
from test_tools_write import staffing_response
from workday_advanced_mcp.operations import lifecycle, organizations
from workday_advanced_mcp.references import reference
from workday_advanced_mcp.soap import parse_envelope

HR = service_url("Human_Resources")
STAFFING = service_url("Staffing")
INTEGRATIONS = service_url("Integrations")
DATE = "2026-10-01"
CREATE = {
    "organization_id": "NEW",
    "name": "New manager team",
    "superior_id": "TOP",
    "effective_date": DATE,
    "subtype_name": "Department",
    "staffing_model": "position",
    "availability_date": DATE,
}


def org(
    identifier="TOP",
    parent=None,
    manager="21001",
    staffing_model="Position Management",
    inactive=False,
):
    data = {
        "Reference_ID": identifier,
        "Name": identifier + " team",
        "Inactive": inactive,
        "Organization_Type_Reference": reference("Organization_Type_ID", "Supervisory"),
        "Manager_Reference": reference("Employee_ID", manager),
        "Leadership_Reference": reference("Employee_ID", manager),
        "Supervisory_Data": {"Staffing_Model": staffing_model},
        "Roles_Data": {
            "Organization_Role_Data": {
                "Role_Reference": reference("Organization_Role_ID", "Manager"),
                "Worker_Reference": reference("Employee_ID", manager),
            }
        },
    }
    if parent:
        data["Hierarchy_Data"] = {
            "Superior_Organization_Reference": reference(
                "Organization_Reference_ID", parent
            )
        }
    return {
        "Organization_Reference": reference("Organization_Reference_ID", identifier),
        "Organization_Data": data,
    }


def org_response(*entries, pages=1):
    return xml_response(
        "Get_Organizations",
        {
            "Response_Results": {"Page": 1, "Total_Pages": pages},
            "Response_Data": {"Organization": list(entries)},
        },
    )


def org_router(existing=None):
    orgs = copy.deepcopy(existing or {"TOP": org()})
    writes = []

    def handle(request):
        body = parse_envelope(request.content.decode()).body
        if "Organization_Add_Update" in body:
            data = body["Organization_Add_Update"]["Organization_Data"]
            identifier = data["Organization_Reference_ID"]
            superior = data["Superior_Organization_Reference"]["ID"]["#text"]
            orgs[identifier] = org(identifier, superior)
            orgs[identifier]["Organization_Data"]["Name"] = data["Organization_Name"]
            writes.append(data)
            return httpx.Response(
                200,
                text=xml_response(
                    "Response",
                    {
                        "Integration_ID_Reference": {
                            "ID": {"@bsvc:System_ID": "WD-WID", "#text": "new-org-wid"}
                        }
                    },
                    root="Organization_Reference",
                ),
            )
        data = body["Get_Organizations_Request"]
        refs = data.get("Request_References", {})
        identifier = refs.get("Organization_Reference", {}).get("ID", {}).get("#text")
        entries = (
            [orgs[identifier]]
            if identifier in orgs
            else ([] if identifier else list(orgs.values()))
        )
        return httpx.Response(200, text=org_response(*entries))

    return handle, writes


async def test_create_manager_org_inserts_under_correct_superior_and_reads_back(
    server_factory,
):
    handler, writes = org_router()
    with respx.mock:
        respx.post(HR).mock(side_effect=handler)
        server = server_factory()
        preview = await call_tool(
            server,
            "workday_preview_write",
            {
                "operation": "workday_create_supervisory_organization",
                "arguments": CREATE,
            },
        )
        assert not writes
        result = await call_tool(
            server,
            "workday_create_supervisory_organization",
            {**CREATE, "expected_state_token": preview["state_token"]},
        )
    assert result["submitted"] is True
    assert result["verification"]["verified"] is True
    assert writes[0]["Superior_Organization_Reference"]["ID"]["#text"] == "TOP"
    assert writes[0]["Position_Management_Enabled"] == "true"
    assert result["response"]["Integration_ID_Reference"]


async def test_create_never_overwrites_existing_org(server_factory):
    handler, writes = org_router({"TOP": org(), "NEW": org("NEW", "TOP")})
    with respx.mock:
        respx.post(HR).mock(side_effect=handler)
        result = await call_tool(
            server_factory(), "workday_create_supervisory_organization", CREATE
        )
    assert result["duplicate"] is True and not writes


@pytest.mark.parametrize(
    "parent,expected", [(org(inactive=True), "active"), (org("TOP", "NEW"), "cycle")]
)
async def test_invalid_parent_prevents_hierarchy_write(
    server_factory, parent, expected
):
    handler, writes = org_router({"TOP": parent, "NEW": org("NEW")})
    with respx.mock:
        respx.post(HR).mock(side_effect=handler)
        result = await call_tool(
            server_factory(),
            "workday_update_supervisory_organization",
            {"organization_id": "NEW", "superior_id": "TOP", "effective_date": DATE},
        )
    assert expected in result and not writes


@pytest.mark.parametrize("parent", ["TOP", "OTHER"])
async def test_reparent_keeps_org_identity_and_leaves_worker_moves_explicit(
    server_factory, parent
):
    handler, writes = org_router(
        {"TOP": org(), "OTHER": org("OTHER"), "NEW": org("NEW", "TOP")}
    )
    with respx.mock:
        respx.post(HR).mock(side_effect=handler)
        result = await call_tool(
            server_factory(),
            "workday_update_supervisory_organization",
            {"organization_id": "NEW", "superior_id": parent, "effective_date": DATE},
        )
    if parent == "TOP":
        assert result["no_change"] is True and not writes
    else:
        assert result["submitted"] is True
        assert writes[0]["Organization_Reference_ID"] == "NEW"
        assert "Worker_Reference" not in writes[0]


async def test_manager_lookup_distinguishes_managed_org_from_membership(
    server_factory, worker_response
):
    def handle(request):
        if "Get_Workers_Request" in request.content.decode():
            return httpx.Response(200, text=worker_response)
        return httpx.Response(
            200, text=org_response(org("TEAM", "SUP-ENG"), org("OTHER", manager="999"))
        )

    with respx.mock:
        respx.post(HR).mock(side_effect=handle)
        result = await call_tool(
            server_factory(),
            "workday_get_manager_organizations",
            {"worker_id": "21001", "as_of_effective_date": DATE},
        )
    assert result["scan_complete"] is True
    assert result["membership_organization"]["id"] == "SUP-ENG"
    assert [o["id"] for o in result["managed_organizations"]] == ["TEAM"]


async def test_manager_lookup_reports_scan_cap(server_factory, worker_response):
    def handle(request):
        text = (
            worker_response
            if "Get_Workers_Request" in request.content.decode()
            else org_response(org(), pages=2)
        )
        return httpx.Response(200, text=text)

    with respx.mock:
        respx.post(HR).mock(side_effect=handle)
        result = await call_tool(
            server_factory(),
            "workday_get_manager_organizations",
            {"worker_id": "21001", "max_pages": 1},
        )
    assert result["scan_complete"] is False


@pytest.mark.parametrize("add", [True, False])
async def test_roles_change_only_named_assignees(server_factory, add):
    with respx.mock:
        respx.post(HR).mock(return_value=httpx.Response(200, text=org_response(org())))
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Assign_Roles"))
        )
        result = await call_tool(
            server_factory(),
            "workday_assign_organization_role",
            {
                "organization_id": "TOP",
                "role_id": "Expense_Approver",
                "assignee_id": "P1",
                "effective_date": DATE,
                "add_assignee": add,
                "remove_assignee_ids": ["OLD"],
            },
        )
    assert result["submitted"] is True
    payload = route.calls[0].request.content.decode()
    assert ("Assignees_to_Add_Reference" in payload) is add
    assert "OLD" in payload and "Expense_Approver" in payload
    assert "Remove_All_Role_Assignments_for_Event_Target_Assignee" not in payload
    assert "<bsvc:Auto_Complete>false" in payload


@pytest.mark.parametrize("model", ["Position Management", "Job Management"])
async def test_position_creation_requires_position_management(server_factory, model):
    with respx.mock:
        respx.post(HR).mock(
            return_value=httpx.Response(
                200, text=org_response(org(staffing_model=model))
            )
        )
        route = respx.post(STAFFING).mock(
            return_value=httpx.Response(200, text=staffing_response("Create_Position"))
        )
        result = await call_tool(
            server_factory(),
            "workday_create_position",
            {
                "organization_id": "TOP",
                "position_id": "NEW-POS",
                "title": "Analyst",
                "availability_date": DATE,
                "earliest_hire_date": DATE,
            },
        )
    if model.startswith("Position"):
        assert (
            result["submitted"] is True
            and "NEW-POS" in route.calls[0].request.content.decode()
        )
    else:
        assert "position-management" in result and route.call_count == 0


HOME = """<wd:Address_Data>
<wd:Country_Reference><wd:ID wd:type="ISO_3166-1_Alpha-2_Code">GB</wd:ID></wd:Country_Reference>
<wd:Address_Line_Data wd:Type="ADDRESS_LINE_1">10 Home Street</wd:Address_Line_Data>
<wd:Address_Line_Data wd:Type="ADDRESS_LINE_2">Flat 2</wd:Address_Line_Data>
<wd:Municipality>London</wd:Municipality><wd:Postal_Code>N1 1AA</wd:Postal_Code>
<wd:Usage_Data wd:Public="false"><wd:Type_Data wd:Primary="true"><wd:Type_Reference><wd:ID wd:type="Communication_Usage_Type_ID">HOME</wd:ID></wd:Type_Reference></wd:Type_Data></wd:Usage_Data>
<wd:Municipality_Local>London local</wd:Municipality_Local>
<wd:Address_Reference><wd:ID wd:type="Address_ID">HOME-1</wd:ID></wd:Address_Reference>
</wd:Address_Data>"""


def with_addresses(worker_response, addresses=HOME):
    return worker_response.replace(
        "</wd:Contact_Data>", addresses + "</wd:Contact_Data>"
    )


async def test_home_address_patch_preserves_other_fields_and_home_privacy(
    server_factory, worker_response
):
    with respx.mock:
        read = respx.post(HR).mock(
            side_effect=[
                httpx.Response(200, text=with_addresses(worker_response)),
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
                "address": {
                    "postal_code": "N2 2BB",
                    "lines": {"ADDRESS_LINE_1": "20 New Street"},
                },
            },
        )
    assert result["submitted"] is True
    xml = read.calls[1].request.content.decode()
    assert (
        "20 New Street" in xml
        and "Flat 2" in xml
        and "London local" in xml
        and "HOME-1" in xml
    )
    assert 'Public="false"' in xml and 'Do_Not_Replace_All="true"' in xml
    assert "WORK" not in xml and "Email_Address_Data" not in xml


@pytest.mark.parametrize(
    "addresses,arguments,error",
    [
        ("", {}, "No visible"),
        (HOME + HOME, {}, "Multiple"),
        (HOME, {"address_id": "MISSING"}, "No visible"),
        (HOME, {"address": {"country": "US"}}, "region_id"),
        (HOME, {"address": {"unexpected": "value"}}, "Unsupported"),
    ],
)
async def test_address_ambiguity_or_unknown_fields_block_writes(
    server_factory, worker_response, addresses, arguments, error
):
    with respx.mock:
        route = respx.post(HR).mock(
            return_value=httpx.Response(
                200, text=with_addresses(worker_response, addresses)
            )
        )
        result = await call_tool(
            server_factory(),
            "workday_update_home_address",
            {
                "worker_id": "21001",
                "effective_date": DATE,
                "address": {"postal_code": "N2 2BB"},
                **arguments,
            },
        )
    assert error in result and route.call_count == 1


async def test_new_home_address_requires_explicit_creation(
    server_factory, worker_response
):
    with respx.mock:
        respx.post(HR).mock(
            side_effect=[
                httpx.Response(200, text=worker_response),
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
                "address": {
                    "country": "GB",
                    "lines": {"ADDRESS_LINE_1": "10 Home Street"},
                },
                "create_if_missing": True,
            },
        )
    assert result["submitted"] is True


async def test_home_address_noop(server_factory, worker_response):
    with respx.mock:
        route = respx.post(HR).mock(
            return_value=httpx.Response(200, text=with_addresses(worker_response))
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
    assert result["no_change"] is True and route.call_count == 1


@pytest.mark.parametrize("action", ["cancel", "rescind"])
async def test_event_recovery_requires_confirmation_and_preserves_notifications(
    server_factory, action
):
    with respx.mock:
        route = respx.post(INTEGRATIONS).mock(
            side_effect=[
                httpx.Response(200, text=EVENT),
                httpx.Response(200, text=EVENT),
                httpx.Response(
                    200, text=staffing_response(f"{action.title()}_Business_Process")
                ),
            ]
        )
        args = {
            "event_id": "event-1",
            "action": action,
            "comment": "Approved correction",
        }
        server = server_factory()
        first = await call_tool(server, "workday_recover_event", args)
        assert first["requires_confirmation"] is True and route.call_count == 1
        result = await call_tool(
            server, "workday_recover_event", {**args, "confirm": True}
        )
    assert result["submitted"] is True
    assert (
        "<bsvc:Suppress_Notifications>false" in route.calls[-1].request.content.decode()
    )


async def test_event_history_uses_legacy_envelope_and_returns_event_ids(server_factory):
    response = xml_response(
        "History",
        {
            "Worker_Event_History_Data": {
                "Event_Data": {
                    "Event_ID": "event-1",
                    "Effective_Date": DATE,
                    "Description": "Hire",
                }
            }
        },
        root="Worker_Event_History",
    )
    with respx.mock:
        route = respx.post(HR).mock(return_value=httpx.Response(200, text=response))
        result = await call_tool(
            server_factory(),
            "workday_get_worker_event_history",
            {"worker_id": "21001", "updated_from": "2026-10-01T00:00:00Z"},
        )
    assert result["events"][0]["event_id"] == "event-1"
    assert "Worker_Event_History_Get" in route.calls[0].request.content.decode()


async def test_event_approver_ids_are_retained(server_factory):
    xml = EVENT.replace(
        "</wd:Awaiting_Task_Data>",
        '<wd:Awaiting_Person_Reference><wd:ID wd:type="Employee_ID">APPROVER</wd:ID></wd:Awaiting_Person_Reference><wd:Task_Status_Reference wd:Descriptor="Awaiting Action"><wd:ID wd:type="WID">STATUS</wd:ID></wd:Task_Status_Reference></wd:Awaiting_Task_Data>',
    )
    with respx.mock:
        respx.post(INTEGRATIONS).mock(return_value=httpx.Response(200, text=xml))
        result = await call_tool(
            server_factory(), "workday_get_event", {"event_id": "event-1"}
        )
    task = result["awaiting_tasks"][0]
    assert task["awaiting_people"][0]["id"] == "APPROVER"
    assert task["status"]["descriptor"] == "Awaiting Action"


@pytest.mark.parametrize(
    "fn,args",
    [
        (
            lifecycle.build_assign_roles,
            {
                "organization_id": "O",
                "role_id": "R",
                "assignee_id": "P",
                "effective_date": DATE,
                "add_assignee": False,
            },
        ),
        (
            lifecycle.build_create_position,
            {
                "organization_id": "O",
                "position_id": "P",
                "title": "T",
                "availability_date": DATE,
                "earliest_hire_date": "2026-09-01",
            },
        ),
        (
            organizations.build_put_supervisory_organization,
            {
                "organization_id": "O",
                "superior_id": "O",
                "name": "Team",
                "effective_date": DATE,
            },
        ),
        (
            organizations.build_put_supervisory_organization,
            {
                "organization_id": "O",
                "superior_id": "P",
                "name": "Team",
                "effective_date": DATE,
                "staffing_model": "invalid",
            },
        ),
    ],
)
def test_invalid_lifecycle_parameters_are_rejected(fn, args):
    with pytest.raises(ValueError):
        fn(**args)


def test_home_address_with_preserved_local_data_validates_against_xsd():
    payload = lifecycle.build_home_address(
        worker_id="21001",
        effective_date=DATE,
        address={"country": "GB", "lines": {"ADDRESS_LINE_1": "10 Street"}},
        baseline={
            "additional_data": {"Municipality_Local": "London", "Number_of_Days": "2"},
            "usage_data": {
                "@Public": "false",
                "Type_Data": {
                    "@Primary": "true",
                    "Type_Reference": {
                        "ID": {"@type": "Communication_Usage_Type_ID", "#text": "HOME"}
                    },
                },
            },
        },
    )
    assert_schema(
        "Human_Resources", "Maintain_Contact_Information_for_Person_Event", payload
    )
