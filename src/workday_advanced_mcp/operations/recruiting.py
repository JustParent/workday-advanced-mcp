"""Recruiting: pre-hire lookup and creation.

A Workday hire needs somebody to hire. ``Put_Applicant`` creates that pre-hire
record; ``Get_Applicants`` finds one that already exists, so a re-run does not
create a duplicate person.

Element order for Put_Applicant_Request:
    Applicant_Reference -> Applicant_Data(Personal_Data -> ...)
"""

from __future__ import annotations

from typing import Any

from ..references import (
    COMMUNICATION_USAGE_TYPE_ID,
    COUNTRY_ISO_ALPHA2,
    WID,
    as_list,
    extract_descriptor,
    extract_id,
    reference,
)

GET_APPLICANTS = "Get_Applicants"
PUT_APPLICANT = "Put_Applicant"

PAGE_SIZE = 100


def build_get_applicants_request(
    page: int, count: int = PAGE_SIZE, *, email: str | None = None
) -> dict[str, Any]:
    """Page through pre-hires, narrowing by email when Workday can."""
    request: dict[str, Any] = {}
    address = (email or "").strip()
    if address:
        request["Request_Criteria"] = {"Email_Address": address}
    request["Response_Filter"] = {"Page": page, "Count": count}
    request["Response_Group"] = {
        "Include_Reference": True,
        "Include_Personal_Information": True,
    }
    return request


def _usage_data() -> dict[str, Any]:
    return {
        "Type_Data": {
            "@bsvc:Primary": "true",
            "Type_Reference": reference(COMMUNICATION_USAGE_TYPE_ID, "WORK"),
        }
    }


def build_put_applicant_request(
    *,
    first_name: str,
    last_name: str,
    country_iso: str = "US",
    email: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """Create a pre-hire from a name plus at least one contact method.

    Workday requires an email, phone or address to create an applicant; the tool
    layer enforces that before this is called.
    """
    personal_data: dict[str, Any] = {
        "Name_Data": {
            "Legal_Name_Data": {
                "Name_Detail_Data": {
                    "Country_Reference": reference(
                        COUNTRY_ISO_ALPHA2, country_iso or "US"
                    ),
                    "First_Name": first_name or "",
                    "Last_Name": last_name or "",
                }
            }
        }
    }

    contact_data: dict[str, Any] = {}
    if email:
        contact_data["Email_Address_Data"] = [
            {"Email_Address": email, "Usage_Data": _usage_data()}
        ]
    if phone:
        contact_data["Phone_Data"] = [
            {
                "Phone_Number": phone,
                "Phone_Device_Type_Reference": reference(
                    "Phone_Device_Type_ID", "Mobile"
                ),
                "Usage_Data": _usage_data(),
            }
        ]
    if contact_data:
        personal_data["Contact_Data"] = contact_data

    return {"Applicant_Data": {"Personal_Data": personal_data}}


def parse_applicants(response: dict[str, Any]) -> list[dict[str, Any]]:
    response_data = response.get("Response_Data")
    if not isinstance(response_data, dict):
        return []

    results: list[dict[str, Any]] = []
    for entry in as_list(response_data.get("Applicant")):
        if not isinstance(entry, dict):
            continue
        ref = entry.get("Applicant_Reference")
        applicant_data = entry.get("Applicant_Data") or {}
        personal = applicant_data.get("Personal_Data") or {}
        results.append(
            {
                "wid": extract_id(ref, WID),
                "descriptor": extract_descriptor(ref),
                "name": _formatted_name(personal) or extract_descriptor(ref),
                "email": _first_email(personal),
            }
        )
    return results


def _formatted_name(personal: dict[str, Any]) -> str:
    name_data = personal.get("Name_Data") if isinstance(personal, dict) else None
    legal = (
        (name_data or {}).get("Legal_Name_Data")
        if isinstance(name_data, dict)
        else None
    )
    detail = (legal or {}).get("Name_Detail_Data") if isinstance(legal, dict) else None
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    if not isinstance(detail, dict):
        return ""
    formatted = str(detail.get("@Formatted_Name") or "").strip()
    if formatted:
        return formatted
    return f"{detail.get('First_Name', '')} {detail.get('Last_Name', '')}".strip()


def _first_email(personal: dict[str, Any]) -> str:
    contact = personal.get("Contact_Data") if isinstance(personal, dict) else None
    if not isinstance(contact, dict):
        return ""
    for entry in as_list(contact.get("Email_Address_Data")):
        if isinstance(entry, dict):
            address = str(entry.get("Email_Address") or "").strip()
            if address:
                return address
    return ""


def applicant_wid_from_response(response: dict[str, Any]) -> str:
    return extract_id(response.get("Applicant_Reference"), WID)
