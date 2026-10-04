"""Real-world response variations: optional/secured fields and collapsed lists."""

from __future__ import annotations

import copy
import sys

import httpx
import pytest
import respx

from conftest import call_tool, service_url, soap_response
from test_regressions import JOB, xml_response
from workday_advanced_mcp.client import WorkdayClient
from workday_advanced_mcp.operations import (
    events,
    organizations,
    payment_elections,
    recruiting,
    workers,
)
from workday_advanced_mcp.references import reference, summarize_reference
from workday_advanced_mcp.resolve import (
    assignments_by_type,
    fetch_worker,
    scan_for_email,
)
from workday_advanced_mcp.soap import parse_envelope
from workday_advanced_mcp.tools_write import _require_batch


def parsed_worker(worker_response):
    return workers.workers_in(
        parse_envelope(worker_response).body["Get_Workers_Response"]
    )[0]


@pytest.mark.parametrize("section", ["Personal_Data", "Employment_Data", "Worker_Data"])
def test_secured_worker_sections_may_be_omitted(worker_response, section):
    worker = parsed_worker(worker_response)
    if section == "Worker_Data":
        worker.pop(section)
    else:
        worker["Worker_Data"].pop(section)
    result = workers.summarize_worker(worker)
    assert result["worker_id"] == "21001"
    if section in {"Employment_Data", "Worker_Data"}:
        assert "status" not in result


@pytest.mark.parametrize(
    "ids,expected,type_name",
    [
        ({"Contingent_Worker_ID": "C1", "WID": "W"}, "C1", "Contingent_Worker_ID"),
        ({"WID": "W"}, "W", "WID"),
        ({}, "", "WID"),
    ],
)
def test_worker_identity_fallbacks_are_explicit(ids, expected, type_name):
    result = workers.summarize_worker(
        {"Worker_Reference": {"ID": [{"@type": k, "#text": v} for k, v in ids.items()]}}
    )
    assert result["worker_id"] == expected and result["worker_id_type"] == type_name


@pytest.mark.parametrize(
    "value,expected", [("1", True), ("0", False), (None, None), ("unexpected", None)]
)
def test_employment_boolean_never_confuses_missing_with_false(
    worker_response, value, expected
):
    worker = parsed_worker(worker_response)
    worker["Worker_Data"]["Employment_Data"]["Worker_Status_Data"]["Active"] = value
    assert workers.summarize_worker(worker)["status"]["active"] is expected


def test_multiple_jobs_without_primary_marker_do_not_pick_an_arbitrary_position(
    worker_response,
):
    worker = parsed_worker(worker_response)
    job = worker["Worker_Data"]["Employment_Data"]["Worker_Job_Data"]
    job.pop("@Primary_Job", None)
    worker["Worker_Data"]["Employment_Data"]["Worker_Job_Data"] = [
        job,
        copy.deepcopy(job),
    ]
    assert "position" not in workers.summarize_worker(worker)


def test_management_chain_and_secondary_address_purposes_are_retained(worker_response):
    worker = parsed_worker(worker_response)
    worker["Worker_Data"]["Management_Chain_Data"] = {
        "Worker_Supervisory_Management_Chain_Data": {
            "Management_Chain_Data": {
                "Organization_Reference": {
                    "ID": {"@type": "Organization_Reference_ID", "#text": "SUP"}
                },
                "Manager_Reference": [
                    {"ID": {"@type": "Employee_ID", "#text": "M1"}},
                    {"ID": {"@type": "Employee_ID", "#text": "M2"}},
                ],
            }
        }
    }
    managers = workers.summarize_worker(worker)["management_chain"][0]["managers"]
    assert [m["id"] for m in managers] == ["M1", "M2"]


@pytest.mark.parametrize(
    "personal",
    [
        {},
        {"Name_Data": {}},
        {"Name_Data": {"Legal_Name_Data": {}}},
        {"Name_Data": {"Legal_Name_Data": {"Name_Detail_Data": []}}},
        {
            "Name_Data": {
                "Legal_Name_Data": {
                    "Name_Detail_Data": {"First_Name": "Test", "Last_Name": "Person"}
                }
            }
        },
    ],
)
def test_optional_name_shapes_are_handled(worker_response, personal):
    worker = parsed_worker(worker_response)
    worker["Worker_Data"]["Personal_Data"] = personal
    assert workers.summarize_worker(worker)["name"]


def test_list_and_empty_response_shapes_are_safe():
    assert workers.workers_in({"Response_Data": ""}) == []
    assert workers.page_info({"Response_Results": {"Page": "bad"}}) == (0, 0)
    assert events.parse_event({}) == {}
    assert events.parse_event({"Response_Data": {}}) == {}
    assert (
        organizations.parse_organizations(
            {"Response_Data": {"Organization": [{}, "invalid"]}}, "Supervisory"
        )
        == []
    )
    assert organizations.parse_job_profiles({}) == []
    assert organizations.parse_locations({}) == []
    assert payment_elections.parse_enrollment({}) == {}
    assert (
        payment_elections.parse_enrollment(
            {
                "Payment_Election_Enrollment_Data": {
                    "Payment_Election_Rule_Group_Data": [
                        "bad",
                        {"Payment_Election_Data": ["bad", {}]},
                    ]
                }
            }
        )["rule_groups"][0]["elections"][0]["bank_account"]
        == {}
    )
    assert recruiting.parse_applicants({}) == []
    assert (
        recruiting.parse_applicants({"Response_Data": {"Applicant": ["bad", {}]}})[0][
            "email"
        ]
        == ""
    )
    assert workers.position_reference_for_write({}) is None
    assert workers.position_reference_for_write(
        {"position": {"position_id": "P1"}}
    ) == ("P1", "Position_ID")
    assert summarize_reference({"ID": "bad"}) == {}
    assert assignments_by_type(
        {
            "organization_assignments": [
                {},
                {"organization_type": "Cost Center", "id": "C1"},
                {"organization_type": "Cost Center", "id": "C1"},
            ]
        }
    ) == {"Cost_Center": ["C1"]}


def test_reference_vocabulary_keeps_the_lookup_type_and_descriptor():
    response = {
        "Response_Data": {
            "Reference_ID": [
                None,
                {},
                {
                    "Reference_ID_Reference": {
                        "@Descriptor": "Manager",
                        "ID": {"@type": "Organization_Role_ID", "#text": "Manager"},
                    }
                },
            ]
        }
    }
    result = organizations.parse_references(response, "Organization_Role_ID")
    assert result[0]["id"] == "Manager" and result[0]["descriptor"] == "Manager"
    assert organizations.parse_references({}, "X") == []
    assert organizations.matches_query(result[0], "manage")


@pytest.mark.parametrize(
    "tool,operation,data,args",
    [
        (
            "workday_list_reference_ids",
            "Get_References",
            {
                "Reference_ID": {
                    "Reference_ID_Reference": reference(
                        "Organization_Role_ID", "Manager"
                    )
                }
            },
            {"reference_id_type": "Organization_Role_ID"},
        ),
        (
            "workday_list_job_profiles",
            "Get_Job_Profiles",
            {
                "Job_Profile": {
                    "Job_Profile_Reference": reference("Job_Profile_ID", "JP")
                }
            },
            {},
        ),
        (
            "workday_list_locations",
            "Get_Locations",
            {"Location": {"Location_Reference": reference("Location_ID", "LON")}},
            {},
        ),
        (
            "workday_list_pre_hires",
            "Get_Applicants",
            {
                "Applicant": {
                    "Applicant_Reference": reference("WID", "APP"),
                    "Applicant_Data": {
                        "Personal_Data": {
                            "Name_Data": {
                                "Legal_Name_Data": {
                                    "Name_Detail_Data": {
                                        "First_Name": "Test",
                                        "Last_Name": "Person",
                                    }
                                }
                            },
                            "Contact_Data": {
                                "Email_Address_Data": {
                                    "Email_Address": "test@example.com"
                                }
                            },
                        }
                    },
                }
            },
            {},
        ),
    ],
)
async def test_reference_read_tools_return_usable_ids(
    server_factory, tool, operation, data, args
):
    service = (
        "Integrations"
        if operation == "Get_References"
        else "Recruiting"
        if operation == "Get_Applicants"
        else "Human_Resources"
    )
    with respx.mock:
        respx.post(service_url(service)).mock(
            return_value=httpx.Response(
                200, text=xml_response(operation, {"Response_Data": data})
            )
        )
        result = await call_tool(server_factory(), tool, args)
    assert result["count"] == 1


async def test_read_pagination_respects_total_pages_and_filter(server_factory):
    first = xml_response(
        "Get_Locations",
        {
            "Response_Results": {"Page": 1, "Total_Pages": 2},
            "Response_Data": {
                "Location": {"Location_Reference": reference("Location_ID", "PARIS")}
            },
        },
    )
    second = xml_response(
        "Get_Locations",
        {
            "Response_Results": {"Page": 2, "Total_Pages": 2},
            "Response_Data": {
                "Location": {"Location_Reference": reference("Location_ID", "LONDON")}
            },
        },
    )
    with respx.mock:
        route = respx.post(service_url("Human_Resources")).mock(
            side_effect=[
                httpx.Response(200, text=first),
                httpx.Response(200, text=second),
            ]
        )
        result = await call_tool(
            server_factory(), "workday_list_locations", {"query": "LONDON", "limit": 1}
        )
    assert result["entries"][0]["id"] == "LONDON" and route.call_count == 2


async def test_empty_worker_search_and_invalid_ids_are_not_errors_of_identity(
    server_factory,
):
    with respx.mock:
        respx.post(service_url("Human_Resources")).mock(
            return_value=httpx.Response(200, text=soap_response("Get_Workers", ""))
        )
        result = await call_tool(
            server_factory(), "workday_find_worker", {"query": "missing"}
        )
        assert result["candidates"] == [] and result["match_type"] == "none"
    client = WorkdayClient()
    for worker_id, id_type in [("", "Employee_ID"), ("1", "Wrong")]:
        with pytest.raises(ValueError):
            await fetch_worker(client, worker_id, id_type)
    assert await scan_for_email(client, "") == ([], True)
    await client.aclose()


async def test_malformed_or_wrong_operation_response_cannot_report_write_success(
    server_factory, worker_response
):
    with respx.mock:
        respx.post(service_url("Human_Resources")).mock(
            return_value=httpx.Response(200, text=worker_response)
        )
        route = respx.post(service_url("Staffing")).mock(
            return_value=httpx.Response(
                200, text="<Envelope><Body><Wrong_Response/></Body></Envelope>"
            )
        )
        result = await call_tool(server_factory(), "workday_change_job", JOB)
    assert result["results"][0]["outcome_unknown"] is True and route.call_count == 1


async def test_processing_fault_is_not_safe_to_retry(server_factory, worker_response):
    fault = "<Envelope><Body><Fault><faultcode>ProcessingFault</faultcode><faultstring>Response processing failed</faultstring></Fault></Body></Envelope>"
    with respx.mock:
        respx.post(service_url("Human_Resources")).mock(
            return_value=httpx.Response(200, text=worker_response)
        )
        route = respx.post(service_url("Staffing")).mock(
            return_value=httpx.Response(500, text=fault)
        )
        server = server_factory()
        first = await call_tool(server, "workday_change_job", JOB)
        second = await call_tool(server, "workday_change_job", JOB)
    assert first["results"][0]["outcome_unknown"] is True
    assert second["results"][0]["write_status"] == "unknown" and route.call_count == 1


def test_invalid_batch_entries_are_rejected_before_planning():
    with pytest.raises(ValueError, match="object"):
        _require_batch([None], "changes")


@pytest.mark.parametrize("read_only", [False, True])
def test_diagnostics_cover_installation_without_leaking_secrets(
    monkeypatch, capsys, read_only
):
    from workday_advanced_mcp.server import main

    monkeypatch.setattr(
        sys,
        "argv",
        ["workday-advanced-mcp", "--test"] + (["--read-only"] if read_only else []),
    )
    main()
    output = capsys.readouterr().out
    assert "isu-password" not in output
    assert f"Registered tools ({16 if read_only else 31})" in output
    assert "Credentials: configured" in output
