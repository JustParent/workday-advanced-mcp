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
    organization_type: str, page: int, count: int = PAGE_SIZE
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
            )
        },
        "Response_Filter": {"Page": page, "Count": count},
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
            summary["name"] = org_data.get("Organization_Name") or summary.get(
                "descriptor", ""
            )
            summary["code"] = org_data.get("Organization_Code") or ""
        results.append(summary)
    return results


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
