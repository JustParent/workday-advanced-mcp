"""Reference-data reads: organizations, job profiles, locations, reference IDs.

These all resolve the human name a caller has ("Engineering", "Redundancy") into
the ID a write needs. They share one response shape: a list of entries each
carrying a reference plus a descriptor.

Element order, all four operations:
    Request_References | Request_Criteria -> Response_Filter -> Response_Group
"""

from __future__ import annotations

from typing import Any

from ..references import (
    JOB_PROFILE_ID,
    LOCATION_ID,
    ORGANIZATION_TYPE_ID,
    WID,
    as_list,
    extract_descriptor,
    extract_ids,
    organization_id_type,
    reference,
    summarize_reference,
)

GET_ORGANIZATIONS = "Get_Organizations"
GET_JOB_PROFILES = "Get_Job_Profiles"
GET_LOCATIONS = "Get_Locations"
GET_REFERENCES = "Get_References"

# Workday caps a page at 100 for these operations.
PAGE_SIZE = 100


def build_get_organizations_request(
    organization_type: str,
    page: int,
    count: int = PAGE_SIZE,
    *,
    include_inactive: bool = False,
    as_of_effective_date: str | None = None,
) -> dict[str, Any]:
    """Organizations of one type.

    Organization_Response_Group does not accept Include_Reference, so no
    Response_Group is sent: the default response already carries the reference
    and the organization data.
    """
    return {
        "Request_Criteria": {
            "Organization_Type_Reference": reference(
                ORGANIZATION_TYPE_ID, organization_type
            ),
            "Include_Inactive": include_inactive,
        },
        "Response_Filter": {
            **(
                {"As_Of_Effective_Date": as_of_effective_date}
                if as_of_effective_date
                else {}
            ),
            "Page": page,
            "Count": count,
        },
        "Response_Group": {
            "Include_Roles_Data": True,
            "Include_Hierarchy_Data": True,
            "Include_Supervisory_Data": True,
        },
    }


def build_get_job_profiles_request(page: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    return {
        "Response_Filter": {"Page": page, "Count": count},
        "Response_Group": {"Include_Reference": True},
    }


def build_get_locations_request(page: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    return {
        "Response_Filter": {"Page": page, "Count": count},
        "Response_Group": {"Include_Reference": True},
    }


def build_get_references_request(
    reference_id_type: str, page: int, count: int = PAGE_SIZE
) -> dict[str, Any]:
    """Values of one Reference ID Type (Integrations service).

    Get_References is how tenant-defined vocabularies - termination reasons, job
    change reasons - are discovered, since their IDs differ per tenant.
    """
    return {
        "Request_Criteria": {"Reference_ID_Type": reference_id_type},
        "Response_Filter": {"Page": page, "Count": count},
    }


def _entries(response: dict[str, Any], element: str) -> list[dict[str, Any]]:
    response_data = response.get("Response_Data")
    if not isinstance(response_data, dict):
        return []
    return [e for e in as_list(response_data.get(element)) if isinstance(e, dict)]


def parse_organizations(
    response: dict[str, Any], organization_type: str
) -> list[dict[str, Any]]:
    id_type = organization_id_type(organization_type)
    results = []
    for entry in _entries(response, "Organization"):
        summary = summarize_reference(
            entry.get("Organization_Reference"),
            preferred=(id_type, "Organization_Reference_ID", WID),
        )
        if not summary:
            continue
        org_data = entry.get("Organization_Data")
        if isinstance(org_data, dict):
            summary["name"] = (
                org_data.get("Name")
                or org_data.get("Organization_Name")
                or summary.get("descriptor", "")
            )
            summary["code"] = org_data.get("Organization_Code") or ""
            summary["reference_id"] = org_data.get("Reference_ID") or summary.get(
                "id", ""
            )
            summary["inactive"] = str(org_data.get("Inactive", "false")).lower() in (
                "true",
                "1",
            )
            summary["organization_type"] = summarize_reference(
                org_data.get("Organization_Type_Reference"),
                preferred=(ORGANIZATION_TYPE_ID, WID),
            ).get("id", "")
            summary["availability_date"] = org_data.get("Availibility_Date", "")
            summary["manager"] = summarize_reference(
                org_data.get("Manager_Reference"), preferred=("Employee_ID", WID)
            )
            summary["leaders"] = [
                summarize_reference(r, preferred=("Employee_ID", WID))
                for r in as_list(org_data.get("Leadership_Reference"))
            ]
            hierarchy = org_data.get("Hierarchy_Data") or {}
            summary["superior"] = summarize_reference(
                hierarchy.get("Superior_Organization_Reference"),
                preferred=("Organization_Reference_ID", WID),
            )
            summary["subordinates"] = [
                summarize_reference(r, preferred=("Organization_Reference_ID", WID))
                for r in as_list(hierarchy.get("Subordinate_Organization_Reference"))
            ]
            supervisory = org_data.get("Supervisory_Data") or {}
            summary["staffing_model"] = supervisory.get("Staffing_Model", "")
            summary["location"] = summarize_reference(
                supervisory.get("Location_Reference"), preferred=(LOCATION_ID, WID)
            )
            summary["roles"] = [
                {
                    "role": summarize_reference(r.get("Role_Reference")),
                    "workers": [
                        summarize_reference(w, preferred=("Employee_ID", WID))
                        for w in as_list(r.get("Worker_Reference"))
                    ],
                }
                for r in as_list(
                    (org_data.get("Roles_Data") or {}).get("Organization_Role_Data")
                )
            ]
        results.append(summary)
    return results


def build_get_organization(
    organization_id: str,
    id_type: str = "Organization_Reference_ID",
    as_of_effective_date: str | None = None,
) -> dict[str, Any]:
    request = build_get_organizations_request(
        "Supervisory", 1, 100, as_of_effective_date=as_of_effective_date
    )
    request.pop("Request_Criteria")
    return {
        "Request_References": {
            "@bsvc:Skip_Non_Existing_Instances": "true",
            "Organization_Reference": reference(id_type, organization_id),
        },
        **request,
    }


def build_put_supervisory_organization(
    *,
    organization_id: str,
    name: str,
    superior_id: str,
    effective_date: str,
    subtype_name: str | None = None,
    staffing_model: str | None = None,
    availability_date: str | None = None,
    location_id: str | None = None,
    visibility_name: str | None = None,
) -> dict[str, Any]:
    from ..validation import iso_date, required

    if organization_id == superior_id:
        raise ValueError("An organization cannot be its own superior.")
    data: dict[str, Any] = {
        "@bsvc:Effective_Date": iso_date(effective_date),
        "Organization_Reference_ID": required(organization_id, "organization_id"),
        "Organization_Name": required(name, "name"),
    }
    if availability_date:
        data["Availability_Date"] = iso_date(availability_date, "availability_date")
    if staffing_model:
        if staffing_model not in {"job", "position"}:
            raise ValueError("staffing_model must be job or position.")
        data["Job_Management_Enabled"] = staffing_model == "job"
        data["Position_Management_Enabled"] = staffing_model == "position"
    data["Superior_Organization_Reference"] = reference(
        "Organization_Reference_ID", required(superior_id, "superior_id")
    )
    data["Organization_Type_Reference"] = {"Organization_Type_Name": "Supervisory"}
    if subtype_name:
        data["Organization_Subtype_Reference"] = {
            "Organization_Subtype_Name": subtype_name
        }
    if visibility_name:
        data["Organization_Visibility_Reference"] = {
            "Organization_Visibility_Name": visibility_name
        }
    if location_id:
        data["Primary_Business_Site_Reference"] = reference(LOCATION_ID, location_id)
    return {"Organization_Data": data}


def build_inactivate_organization(
    organization_wid: str,
    effective_date: str,
    *,
    keep_in_hierarchy: bool = True,
    remove_role_assignments: bool = False,
) -> dict[str, Any]:
    from ..validation import iso_date, required

    return {
        "Organization_Reference_Data": {
            "Integration_ID_Reference": {
                "ID": {
                    "@bsvc:System_ID": "WD-WID",
                    "#text": required(organization_wid, "organization_wid"),
                }
            }
        },
        "Organization_Inactivate_Data": {
            "@bsvc:Effective_Date": iso_date(effective_date)
        },
        "Keep_in_Hierarchy": keep_in_hierarchy,
        "Remove_All_Role_Assignments": remove_role_assignments,
    }


def parse_job_profiles(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        summary
        for entry in _entries(response, "Job_Profile")
        if (
            summary := summarize_reference(
                entry.get("Job_Profile_Reference"), preferred=(JOB_PROFILE_ID, WID)
            )
        )
    ]


def parse_locations(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        summary
        for entry in _entries(response, "Location")
        if (
            summary := summarize_reference(
                entry.get("Location_Reference"), preferred=(LOCATION_ID, WID)
            )
        )
    ]


def parse_references(
    response: dict[str, Any], reference_id_type: str
) -> list[dict[str, Any]]:
    """Get_References returns bare reference elements; keep the requested type."""
    results: list[dict[str, Any]] = []
    response_data = response.get("Response_Data")
    if not isinstance(response_data, dict):
        return results
    # The element name varies by tenant build; take whatever the Response_Data holds.
    for _element, value in response_data.items():
        for entry in as_list(value):
            if not isinstance(entry, dict):
                continue
            ids = extract_ids(entry.get("Reference_ID_Reference") or entry)
            if not ids:
                continue
            results.append(
                {
                    "id": ids.get(reference_id_type, "")
                    or next(iter(ids.values()), ""),
                    "id_type": reference_id_type
                    if reference_id_type in ids
                    else next(iter(ids), ""),
                    "descriptor": extract_descriptor(
                        entry.get("Reference_ID_Reference") or entry
                    ),
                    "ids": ids,
                }
            )
    return results


def matches_query(entry: dict[str, Any], query: str) -> bool:
    """Case-insensitive substring match across an entry's id, name and descriptor."""
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(entry.get(key, "")) for key in ("id", "descriptor", "name", "code")
    ).lower()
    return needle in haystack
