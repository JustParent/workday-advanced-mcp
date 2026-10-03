"""Schema-checked v44.2 organization roles, positions and home addresses."""

from __future__ import annotations

from typing import Any

from ..references import as_list, reference, summarize_reference
from ..validation import iso_date, required
from .common import business_process_parameters


def build_assign_roles(
    *,
    organization_id: str,
    role_id: str,
    assignee_id: str,
    effective_date: str,
    assignee_id_type: str = "Position_ID",
    role_id_type: str = "Organization_Role_ID",
    add_assignee: bool = True,
    remove_assignee_ids: list[str] | None = None,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    assignee = reference(assignee_id_type, required(assignee_id, "assignee_id"))
    assignment: dict[str, Any] = {
        "Role_Assigner_Reference": reference(
            "Organization_Reference_ID", required(organization_id, "organization_id")
        ),
        "Assignable_Role_Reference": reference(
            role_id_type, required(role_id, "role_id")
        ),
        "Remove_Existing_Assignees_for_Assignable_Role_on_Role_Assigner": False,
        "Update_Later_Dated_Assignments": False,
    }
    if not add_assignee and not remove_assignee_ids:
        raise ValueError("Supply an assignee to add or named assignees to remove.")
    if add_assignee:
        assignment["Assignees_to_Add_Reference"] = assignee
    if remove_assignee_ids:
        assignment["Assignees_to_Remove_Reference"] = [
            reference(assignee_id_type, required(v, "remove_assignee_id"))
            for v in remove_assignee_ids
        ]
    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Assign_Roles_Event_Data": {
            "Effective_Date": iso_date(effective_date),
            "Event_Target_Assignee_Reference": assignee,
            "Assign_Roles_Role_Assignment_Data": assignment,
        },
    }


def build_get_positions(
    *,
    organization_id: str | None = None,
    position_id: str | None = None,
    as_of_effective_date: str | None = None,
    page: int = 1,
    count: int = 100,
    exclude_filled: bool = True,
) -> dict[str, Any]:
    request: dict[str, Any] = {}
    if position_id:
        request["Request_References"] = {
            "@bsvc:Skip_Non_Existing_Instances": "true",
            "Positions_Reference": reference("Position_ID", position_id),
        }
    else:
        criteria: dict[str, Any] = {}
        if organization_id:
            criteria["Supervisory_Organization_Reference"] = reference(
                "Organization_Reference_ID", organization_id
            )
        criteria["Exclude_Filled_Positions"] = exclude_filled
        request["Request_Criteria"] = criteria
    request["Response_Filter"] = {
        **(
            {"As_Of_Effective_Date": iso_date(as_of_effective_date)}
            if as_of_effective_date
            else {}
        ),
        "Page": page,
        "Count": count,
    }
    request["Response_Group"] = {
        "Include_Reference": True,
        "Include_Position_Definition_Data": True,
        "Include_Position_Restrictions_Data": True,
        "Include_Worker_For_Filled_Positions_Data": True,
    }
    return request


def parse_positions(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            **summarize_reference(
                p.get("Position_Reference"), preferred=("Position_ID", "WID")
            ),
            "data": p.get("Position_Data", {}),
        }
        for p in as_list((response.get("Response_Data") or {}).get("Position"))
        if isinstance(p, dict)
    ]


def build_create_position(
    *,
    organization_id: str,
    position_id: str,
    title: str,
    availability_date: str,
    earliest_hire_date: str,
    job_profile_id: str | None = None,
    location_id: str | None = None,
    reason_id: str | None = None,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    iso_date(availability_date, "availability_date")
    iso_date(earliest_hire_date, "earliest_hire_date")
    if earliest_hire_date < availability_date:
        raise ValueError("earliest_hire_date cannot precede availability_date.")
    data: dict[str, Any] = {
        "Supervisory_Organization_Reference": reference(
            "Organization_Reference_ID", required(organization_id, "organization_id")
        )
    }
    if reason_id:
        data["Position_Request_Reason_Reference"] = reference(
            "General_Event_Subcategory_ID", reason_id
        )
    data["Position_Data"] = {
        "Position_ID": required(position_id, "position_id"),
        "Job_Posting_Title": required(title, "title"),
    }
    restrictions: dict[str, Any] = {
        "Availability_Date": availability_date,
        "Earliest_Hire_Date": earliest_hire_date,
    }
    if job_profile_id:
        restrictions["Job_Profile_Reference"] = reference(
            "Job_Profile_ID", job_profile_id
        )
    if location_id:
        restrictions["Location_Reference"] = reference("Location_ID", location_id)
    data["Position_Group_Restrictions_Data"] = restrictions
    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Create_Position_Data": data,
    }


def parse_addresses(contact: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for data in as_list(contact.get("Address_Data")):
        usages = []
        for usage in as_list(data.get("Usage_Data")):
            for typ in as_list(usage.get("Type_Data")):
                usages.append(
                    {
                        "type": summarize_reference(
                            typ.get("Type_Reference"),
                            preferred=("Communication_Usage_Type_ID", "WID"),
                        ).get("id"),
                        "primary": str(typ.get("@Primary", "false")).lower()
                        in ("1", "true"),
                        "public": str(usage.get("@Public", "false")).lower()
                        in ("1", "true"),
                    }
                )
        results.append(
            {
                "reference": summarize_reference(
                    data.get("Address_Reference"), preferred=("Address_ID", "WID")
                ),
                "country": summarize_reference(
                    data.get("Country_Reference"),
                    preferred=("ISO_3166-1_Alpha-2_Code", "WID"),
                ).get("id", ""),
                "lines": {
                    line.get("@Type", ""): line.get("#text", "")
                    for line in as_list(data.get("Address_Line_Data"))
                    if isinstance(line, dict)
                },
                "city": data.get("Municipality", ""),
                "postal_code": data.get("Postal_Code", ""),
                "region_id": summarize_reference(
                    data.get("Country_Region_Reference"),
                    preferred=("Country_Region_ID", "WID"),
                ).get("id", ""),
                "additional_data": {
                    k: v
                    for k, v in data.items()
                    if k
                    in {
                        "Country_City_Reference",
                        "Submunicipality_Data",
                        "Subregion_Data",
                        "Number_of_Days",
                        "Municipality_Local",
                    }
                },
                "usage_data": data.get("Usage_Data"),
                "usages": usages,
            }
        )
    return results


def build_home_address(
    *,
    worker_id: str,
    effective_date: str,
    address: dict[str, Any],
    worker_id_type: str = "Employee_ID",
    address_id: str | None = None,
    address_id_type: str = "Address_ID",
    primary: bool = True,
    auto_complete: bool = False,
    comment: str | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed = {"country", "lines", "city", "region_id", "postal_code"}
    if set(address) - allowed:
        raise ValueError(
            f"Unsupported address fields: {sorted(set(address) - allowed)}"
        )
    data: dict[str, Any] = {
        "@bsvc:Do_Not_Replace_All": "true",
        "@bsvc:Effective_Date": iso_date(effective_date),
        "Country_Reference": reference(
            "ISO_3166-1_Alpha-2_Code", required(address.get("country", ""), "country")
        ),
    }
    lines = address.get("lines") or {}
    if not isinstance(lines, dict) or any(
        not str(key).startswith("ADDRESS_LINE_") for key in lines
    ):
        raise ValueError("lines must map ADDRESS_LINE_1 etc. to address text.")
    if lines:
        data["Address_Line_Data"] = [
            {"@bsvc:Type": k, "#text": v} for k, v in sorted(lines.items())
        ]
    if "city" in address:
        data["Municipality"] = address["city"]
    if address.get("region_id"):
        data["Country_Region_Reference"] = reference(
            "Country_Region_ID", address["region_id"]
        )
    if "postal_code" in address:
        data["Postal_Code"] = address["postal_code"]
    data["Usage_Data"] = {
        "@bsvc:Public": "false",
        "Type_Data": {
            "@bsvc:Primary": str(primary).lower(),
            "Type_Reference": reference("Communication_Usage_Type_ID", "HOME"),
        },
    }
    if address_id:
        data["Address_Reference"] = reference(address_id_type, address_id)
    if baseline:
        data.update(baseline.get("additional_data") or {})
        # Keep secondary HOME purposes and comments, never a WORK/public usage.
        if baseline.get("usage_data"):
            from copy import deepcopy

            usage = deepcopy(baseline["usage_data"])
            for item in as_list(usage):
                item.pop("@Public", None)
                item["@bsvc:Public"] = "false"
            data["Usage_Data"] = usage
    order = [
        "@bsvc:Do_Not_Replace_All",
        "@bsvc:Effective_Date",
        "Country_Reference",
        "Address_Line_Data",
        "Municipality",
        "Country_City_Reference",
        "Submunicipality_Data",
        "Country_Region_Reference",
        "Subregion_Data",
        "Postal_Code",
        "Usage_Data",
        "Number_of_Days",
        "Municipality_Local",
        "Address_Reference",
    ]

    # Parsed attributes have no namespace prefix; qualify them for outbound XML.
    def qualify(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                (
                    f"@bsvc:{key[1:]}"
                    if key.startswith("@") and ":" not in key
                    else key
                ): qualify(v)
                for key, v in value.items()
            }
        if isinstance(value, list):
            return [qualify(v) for v in value]
        return value

    data = qualify({key: data[key] for key in order if key in data})
    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Maintain_Contact_Information_Data": {
            "Worker_Reference": reference(worker_id_type, worker_id),
            "Effective_Date": effective_date,
            "Worker_Contact_Information_Data": {"Address_Data": data},
        },
    }
