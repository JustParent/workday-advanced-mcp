"""Get_Workers: worker lookup and the detail every write needs first.

Element order for Get_Workers_Request (Human_Resources / Staffing v44.2):
    Request_References | Request_Criteria -> Response_Filter -> Response_Group
"""

from __future__ import annotations

from typing import Any

from ..references import (
    CONTINGENT_WORKER_ID,
    EMPLOYEE_ID,
    POSITION_ID,
    WID,
    as_list,
    extract_descriptor,
    extract_id,
    reference,
    summarize_reference,
)

OPERATION = "Get_Workers"

# Response groups. Ask for only what a tool reads: Get_Workers grows very large
# quickly and the whole response must fit inside one MCP round trip.
GROUP_IDENTITY = {
    "Include_Reference": True,
    "Include_Personal_Information": True,
}
GROUP_FULL = {
    "Include_Reference": True,
    "Include_Personal_Information": True,
    "Include_Employment_Information": True,
    "Include_Organizations": True,
    "Include_Roles": True,
    "Include_Management_Chain_Data": True,
    "Include_Multiple_Managers_in_Management_Chain_Data": True,
}


def build_get_worker_request(
    worker_id: str,
    id_type: str,
    *,
    response_group: dict[str, Any] | None = None,
    as_of_effective_date: str | None = None,
) -> dict[str, Any]:
    """Look one worker up by reference.

    ``Skip_Non_Existing_Instances`` turns an unknown ID into an empty response
    rather than a SOAP fault, so a miss reads as "not found" instead of an error.
    """
    request: dict[str, Any] = {
        "Request_References": {
            "@bsvc:Skip_Non_Existing_Instances": "true",
            "Worker_Reference": reference(id_type, worker_id),
        }
    }
    response_filter: dict[str, Any] = {}
    if as_of_effective_date:
        response_filter["As_Of_Effective_Date"] = as_of_effective_date
    if response_filter:
        request["Response_Filter"] = response_filter
    request["Response_Group"] = dict(response_group or GROUP_FULL)
    return request


def build_scan_request(
    page: int,
    count: int,
    *,
    response_group: dict[str, Any] | None = None,
    exclude_inactive: bool = True,
) -> dict[str, Any]:
    """Page through workers, for the email fallback in workday_find_worker."""
    return {
        "Request_Criteria": {"Exclude_Inactive_Workers": exclude_inactive},
        "Response_Filter": {"Page": page, "Count": count},
        "Response_Group": dict(response_group or GROUP_IDENTITY),
    }


def workers_in(response: dict[str, Any]) -> list[dict[str, Any]]:
    """The Worker entries in a Get_Workers_Response."""
    response_data = response.get("Response_Data") or {}
    if not isinstance(response_data, dict):
        return []
    return [w for w in as_list(response_data.get("Worker")) if isinstance(w, dict)]


def page_info(response: dict[str, Any]) -> tuple[int, int]:
    """``(current_page, total_pages)`` from Response_Results, 0 when absent."""
    results = response.get("Response_Results")
    if not isinstance(results, dict):
        return 0, 0
    try:
        return int(str(results.get("Page", 0))), int(str(results.get("Total_Pages", 0)))
    except ValueError:
        return 0, 0


def _primary_job(worker_data: dict[str, Any]) -> dict[str, Any]:
    """The worker's primary job, or their first one."""
    employment = worker_data.get("Employment_Data")
    if not isinstance(employment, dict):
        return {}
    jobs = [
        j for j in as_list(employment.get("Worker_Job_Data")) if isinstance(j, dict)
    ]
    if not jobs:
        return {}
    for job in jobs:
        if (
            str(job.get("@Primary_Job", "")).lower() == "true"
            or job.get("@Primary_Job") == "1"
        ):
            return job
    return jobs[0] if len(jobs) == 1 else {}


def _emails(personal: dict[str, Any]) -> list[str]:
    contact = personal.get("Contact_Data")
    if not isinstance(contact, dict):
        return []
    found: list[str] = []
    for entry in as_list(contact.get("Email_Address_Data")):
        if isinstance(entry, dict):
            address = str(entry.get("Email_Address") or "").strip()
            if address and address not in found:
                found.append(address)
    return found


def _name(personal: dict[str, Any]) -> str:
    name_data = personal.get("Name_Data")
    if not isinstance(name_data, dict):
        return ""
    legal = name_data.get("Legal_Name_Data")
    if not isinstance(legal, dict):
        return ""
    detail = legal.get("Name_Detail_Data")
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    if not isinstance(detail, dict):
        return ""
    formatted = str(detail.get("@Formatted_Name") or "").strip()
    if formatted:
        return formatted
    first = str(detail.get("First_Name") or "").strip()
    last = str(detail.get("Last_Name") or "").strip()
    return f"{first} {last}".strip()


def organization_assignments(worker: dict[str, Any]) -> list[dict[str, Any]]:
    """Every organization the worker's primary position is assigned to.

    The cost centre a caller wants to change lives here, on the position rather
    than on the worker, which is why Change_Organization_Assignments addresses a
    position reference.
    """
    worker_data = worker.get("Worker_Data")
    if not isinstance(worker_data, dict):
        return []

    assignments: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    containers = []
    position_orgs = _primary_job(worker_data).get("Position_Organizations_Data")
    if isinstance(position_orgs, dict):
        containers.extend(as_list(position_orgs.get("Position_Organization_Data")))
    worker_orgs = worker_data.get("Organization_Data")
    if isinstance(worker_orgs, dict):
        containers.extend(as_list(worker_orgs.get("Worker_Organization_Data")))

    for container in containers:
        if not isinstance(container, dict):
            continue
        org_data = container.get("Organization_Data")
        if not isinstance(org_data, dict):
            continue
        org_type_ref = org_data.get("Organization_Type_Reference")
        # Prefer the typed ID; a tenant that omits it still has the descriptor.
        org_type = extract_id(
            org_type_ref, "Organization_Type_ID"
        ) or extract_descriptor(org_type_ref)
        summary = summarize_reference(
            org_data.get("Organization_Reference"),
            preferred=("Organization_Reference_ID", WID),
        )
        key = (org_type, summary.get("id", ""))
        if not summary or key in seen:
            continue
        seen.add(key)
        assignments.append(
            {
                "organization_type": org_type,
                "name": org_data.get("Organization_Name")
                or summary.get("descriptor", ""),
                "code": org_data.get("Organization_Code") or "",
                "reference_id": org_data.get("Organization_Reference_ID")
                or summary.get("id", ""),
                **summary,
            }
        )
    return assignments


def summarize_worker(
    worker: dict[str, Any], *, detailed: bool = True
) -> dict[str, Any]:
    """Flatten one Worker entry into the fields the tools report."""
    worker_ref = worker.get("Worker_Reference")
    summary: dict[str, Any] = {
        "worker_id": extract_id(worker_ref, EMPLOYEE_ID),
        "worker_id_type": EMPLOYEE_ID,
        "wid": extract_id(worker_ref, WID),
        "descriptor": extract_descriptor(worker_ref)
        or str(worker.get("Worker_Descriptor") or ""),
    }
    if not summary["worker_id"]:
        # A contingent worker has no Employee_ID; fall back to whatever it has.
        for id_type in (EMPLOYEE_ID, CONTINGENT_WORKER_ID, WID):
            value = extract_id(worker_ref, id_type)
            if value:
                summary["worker_id"] = value
                summary["worker_id_type"] = id_type
                break
        else:
            summary["worker_id"] = summary["wid"]
            summary["worker_id_type"] = WID

    worker_data = worker.get("Worker_Data")
    if not isinstance(worker_data, dict):
        return summary

    personal = worker_data.get("Personal_Data")
    if isinstance(personal, dict):
        summary["name"] = _name(personal) or summary["descriptor"]
        emails = _emails(personal)
        if emails:
            summary["emails"] = emails
            summary["primary_email"] = emails[0]

    if not detailed:
        return summary

    from .lifecycle import parse_addresses

    summary["addresses"] = (
        parse_addresses((personal or {}).get("Contact_Data") or {})
        if isinstance(personal, dict)
        else []
    )
    chain = (worker_data.get("Management_Chain_Data") or {}).get(
        "Worker_Supervisory_Management_Chain_Data"
    ) or {}
    summary["management_chain"] = [
        {
            "organization": summarize_reference(
                item.get("Organization_Reference"),
                preferred=("Organization_Reference_ID", WID),
            ),
            "managers": [
                summarize_reference(manager, preferred=(EMPLOYEE_ID, WID))
                for manager in as_list(item.get("Manager_Reference"))
            ],
        }
        for item in as_list(chain.get("Management_Chain_Data"))
        if isinstance(item, dict)
    ]

    employment = worker_data.get("Employment_Data")
    if isinstance(employment, dict):
        status = employment.get("Worker_Status_Data")
        if isinstance(status, dict):
            summary["status"] = {
                "active": xml_bool(status.get("Active")),
                "terminated": xml_bool(status.get("Terminated")),
                "hire_date": status.get("Hire_Date") or "",
                "original_hire_date": status.get("Original_Hire_Date") or "",
                "termination_date": status.get("Termination_Date") or "",
                "end_employment_date": status.get("End_Employment_Date") or "",
            }

    job = _primary_job(worker_data)
    position_data = job.get("Position_Data") if isinstance(job, dict) else None
    if isinstance(position_data, dict):
        job_profile = position_data.get("Job_Profile_Summary_Data")
        site = position_data.get("Business_Site_Summary_Data")
        summary["position"] = {
            "position_id": position_data.get("Position_ID") or "",
            "position_id_type": POSITION_ID,
            "position_title": position_data.get("Position_Title") or "",
            "business_title": position_data.get("Business_Title") or "",
            "start_date": position_data.get("Start_Date") or "",
            "employee_type": summarize_reference(
                position_data.get("Employee_Type_Reference"),
                preferred=("Employee_Type_ID", WID),
            ),
            "position_time_type": summarize_reference(
                position_data.get("Position_Time_Type_Reference"),
                preferred=("Position_Time_Type_ID", WID),
            ),
            "job_profile": summarize_reference(
                (job_profile or {}).get("Job_Profile_Reference")
                if isinstance(job_profile, dict)
                else None,
                preferred=("Job_Profile_ID", WID),
            ),
            "location": summarize_reference(
                (site or {}).get("Location_Reference")
                if isinstance(site, dict)
                else None,
                preferred=("Location_ID", WID),
            ),
        }

    assignments = organization_assignments(worker)
    if assignments:
        summary["organization_assignments"] = assignments
        supervisory = next(
            (
                a
                for a in assignments
                if a.get("organization_type")
                in ("Supervisory", "Supervisory Organization")
            ),
            None,
        )
        if supervisory:
            summary["supervisory_organization"] = supervisory
        cost_center = next(
            (
                a
                for a in assignments
                if a.get("organization_type") in ("Cost_Center", "Cost Center")
            ),
            None,
        )
        if cost_center:
            summary["cost_center"] = cost_center

    return summary


def position_reference_for_write(
    worker_summary: dict[str, Any],
) -> tuple[str, str] | None:
    """``(position_id, id_type)`` for the worker's primary position, if known.

    Change_Organization_Assignments requires a position, not a worker, so a caller
    that only has an employee ID must read the worker first.
    """
    position = worker_summary.get("position")
    if isinstance(position, dict):
        position_id = str(position.get("position_id") or "").strip()
        if position_id:
            return position_id, POSITION_ID
    return None


def xml_bool(value: Any) -> bool | None:
    """Retain the distinction between false and unavailable data."""
    if str(value).lower() in ("true", "1"):
        return True
    if str(value).lower() in ("false", "0"):
        return False
    return None
