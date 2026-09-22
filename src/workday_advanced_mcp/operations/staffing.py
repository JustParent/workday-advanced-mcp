"""Staffing business processes: hire, change job, org assignments, terminate.

Every builder here states the document order Workday's schema requires. Workday
rejects a request whose children are out of order with a validation fault that
does not say which element is misplaced, so the order is asserted by
``tests/test_envelopes.py`` rather than trusted.

Schema reference: Workday Web Services v44.2, Staffing service.
"""

from __future__ import annotations

from typing import Any

from ..references import (
    EMPLOYEE_ID,
    EMPLOYEE_TYPE_ID,
    EVENT_CLASSIFICATION_SUBCATEGORY_ID,
    JOB_PROFILE_ID,
    JOB_REQUISITION_ID,
    LOCATION_ID,
    ORGANIZATION_REFERENCE_ID,
    POSITION_ID,
    POSITION_TIME_TYPE_ID,
    TERMINATION_SUBCATEGORY_ID,
    WID,
    organization_id_type,
    reference,
)
from .common import business_process_parameters

CHANGE_ORGANIZATION_ASSIGNMENTS = "Change_Organization_Assignments"
CHANGE_JOB = "Change_Job"
TERMINATE_EMPLOYEE = "Terminate_Employee"
HIRE_EMPLOYEE = "Hire_Employee"

# Position_Organization_Assignments_Data children, in the order the schema lists
# them. An organization type absent from this map cannot be assigned here.
ASSIGNMENT_ELEMENTS: dict[str, str] = {
    "Company": "Company_Assignments_Reference",
    "Cost_Center": "Cost_Center_Assignments_Reference",
    "Region": "Region_Assignments_Reference",
    "Fund": "Fund_Assignments_Reference",
    "Grant": "Grant_Assignments_Reference",
    "Program": "Program_Assignments_Reference",
    "Business_Unit": "Business_Unit_Assignments_Reference",
    "Gift": "Gift_Assignments_Reference",
}
ASSIGNMENT_ORDER = (
    "Company",
    "Cost_Center",
    "Region",
    "Fund",
    "Grant",
    "Program",
    "Business_Unit",
    "Gift",
)

ASSIGNABLE_TYPES = tuple(ASSIGNMENT_ORDER)


def build_change_organization_assignments_request(
    *,
    position_id: str,
    position_id_type: str = POSITION_ID,
    effective_date: str,
    assignments: dict[str, list[str]],
    worker_id: str | None = None,
    worker_id_type: str = EMPLOYEE_ID,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    """Assign a position's cost centre, company, region and similar.

    Document order:
        Business_Process_Parameters
        Change_Organization_Assignments_Data
            @Effective_Date            (an attribute, not an element)
            Position_Reference         (required - the position, not the worker)
            Worker_Reference           (optional)
            Position_Organization_Assignments_Data

    ``assignments`` maps an organization type to the IDs to assign. Workday
    replaces the assignments for the types present, so the tool layer merges the
    worker's current assignments in before calling this.
    """
    assignment_data: dict[str, Any] = {}
    for organization_type in ASSIGNMENT_ORDER:
        values = [v for v in assignments.get(organization_type, []) if v]
        if not values:
            continue
        id_type = organization_id_type(organization_type)
        element = ASSIGNMENT_ELEMENTS[organization_type]
        assignment_data[element] = [reference(id_type, value) for value in values]

    unknown = set(assignments) - set(ASSIGNMENT_ELEMENTS)
    if unknown:
        raise ValueError(
            "Cannot assign organization type(s) "
            + ", ".join(sorted(unknown))
            + f". Supported types: {', '.join(ASSIGNABLE_TYPES)}."
        )
    if not assignment_data:
        raise ValueError("No organization assignments were supplied.")

    data: dict[str, Any] = {"@bsvc:Effective_Date": effective_date}
    data["Position_Reference"] = reference(position_id_type, position_id)
    if worker_id:
        data["Worker_Reference"] = reference(worker_id_type, worker_id)
    data["Position_Organization_Assignments_Data"] = assignment_data

    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Change_Organization_Assignments_Data": data,
    }


def build_change_job_request(
    *,
    worker_id: str,
    worker_id_type: str = EMPLOYEE_ID,
    effective_date: str,
    reason_id: str,
    reason_id_type: str = EVENT_CLASSIFICATION_SUBCATEGORY_ID,
    position_id: str | None = None,
    position_id_type: str = POSITION_ID,
    supervisory_organization_id: str | None = None,
    supervisory_organization_id_type: str = ORGANIZATION_REFERENCE_ID,
    job_profile_id: str | None = None,
    location_id: str | None = None,
    business_title: str | None = None,
    position_title: str | None = None,
    position_time_type_id: str | None = None,
    employee_type_id: str | None = None,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    """Move a worker to a new supervisory organization, job profile or location.

    A supervisory-organization move is a Change Job (a transfer), not an
    organization assignment: Change_Organization_Assignments covers cost centre
    and company, never the reporting structure.

    Document order:
        Business_Process_Parameters
        Change_Job_Data
            Worker_Reference
            Position_Reference
            Effective_Date
            Change_Job_Detail_Data
                Reason_Reference                    (required)
                Supervisory_Organization_Reference
                Employee_Type_Reference
                Job_Details_Data
                    Job_Profile_Reference
                    Position_Title
                    Business_Title
                    Location_Reference
                    Position_Time_Type_Reference
    """
    detail: dict[str, Any] = {"Reason_Reference": reference(reason_id_type, reason_id)}
    if supervisory_organization_id:
        detail["Supervisory_Organization_Reference"] = reference(
            supervisory_organization_id_type, supervisory_organization_id
        )
    if employee_type_id:
        detail["Employee_Type_Reference"] = reference(
            EMPLOYEE_TYPE_ID, employee_type_id
        )

    job_details: dict[str, Any] = {}
    if job_profile_id:
        job_details["Job_Profile_Reference"] = reference(JOB_PROFILE_ID, job_profile_id)
    if position_title:
        job_details["Position_Title"] = position_title
    if business_title:
        job_details["Business_Title"] = business_title
    if location_id:
        job_details["Location_Reference"] = reference(LOCATION_ID, location_id)
    if position_time_type_id:
        job_details["Position_Time_Type_Reference"] = reference(
            POSITION_TIME_TYPE_ID, position_time_type_id
        )
    if job_details:
        detail["Job_Details_Data"] = job_details

    data: dict[str, Any] = {"Worker_Reference": reference(worker_id_type, worker_id)}
    if position_id:
        data["Position_Reference"] = reference(position_id_type, position_id)
    data["Effective_Date"] = effective_date
    data["Change_Job_Detail_Data"] = detail

    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Change_Job_Data": data,
    }


def build_terminate_employee_request(
    *,
    worker_id: str,
    worker_id_type: str = EMPLOYEE_ID,
    termination_date: str,
    primary_reason_id: str,
    primary_reason_id_type: str = TERMINATION_SUBCATEGORY_ID,
    last_day_of_work: str | None = None,
    pay_through_date: str | None = None,
    resignation_date: str | None = None,
    notification_date: str | None = None,
    regrettable: bool | None = None,
    eligible_for_hire: bool | None = None,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    """End a worker's employment.

    Document order:
        Business_Process_Parameters
        Terminate_Employee_Data
            Employee_Reference
            Termination_Date
            Terminate_Event_Data
                Last_Day_of_Work
                Primary_Reason_Reference       (required)
                Pay_Through_Date
                Resignation_Date
                Notification_Date
                Regrettable_Reference          (Yes/No reference, not a boolean)
                Eligible_for_Hire_Reference

    ``primary_reason_id`` is tenant-defined: resolve it with
    workday_list_reference_ids before calling, never guess it.
    """
    event: dict[str, Any] = {}
    if last_day_of_work:
        event["Last_Day_of_Work"] = last_day_of_work
    event["Primary_Reason_Reference"] = reference(
        primary_reason_id_type, primary_reason_id
    )
    if pay_through_date:
        event["Pay_Through_Date"] = pay_through_date
    if resignation_date:
        event["Resignation_Date"] = resignation_date
    if notification_date:
        event["Notification_Date"] = notification_date
    if regrettable is not None:
        event["Regrettable_Reference"] = _yes_no(regrettable)
    if eligible_for_hire is not None:
        event["Eligible_for_Hire_Reference"] = _yes_no(eligible_for_hire)

    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Terminate_Employee_Data": {
            "Employee_Reference": reference(worker_id_type, worker_id),
            "Termination_Date": termination_date,
            "Terminate_Event_Data": event,
        },
    }


def _yes_no(value: bool) -> dict[str, Any]:
    """Workday models these flags as a Yes/No business object, not a boolean."""
    return reference("Yes_No_Type_ID", "Yes" if value else "No")


def build_hire_employee_request(
    *,
    organization_id: str,
    hire_date: str,
    pre_hire_wid: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    country_iso: str = "US",
    email: str | None = None,
    organization_id_type_value: str = ORGANIZATION_REFERENCE_ID,
    job_requisition_id: str | None = None,
    employee_type_id: str | None = None,
    job_profile_id: str | None = None,
    location_id: str | None = None,
    position_time_type_id: str | None = None,
    position_title: str | None = None,
    business_title: str | None = None,
    employee_id: str | None = None,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    """Hire a pre-hire into a supervisory organization.

    Document order (matching the shape Harriet's first-party integration already
    submits successfully in production):
        Business_Process_Parameters
        Hire_Employee_Data
            Applicant_Reference | Applicant_Data
            Organization_Reference
            Job_Requisition_Reference
            Hire_Date
            Hire_Employee_Event_Data
                Employee_Type_Reference
                Position_Details
                    Job_Profile_Reference
                    Location_Reference
                    Position_Time_Type_Reference
                    Position_Title
                    Business_Title
            Employee_ID

    A tenant whose Hire business process requires a compensation sub-process will
    leave an auto-completed hire incomplete; submit with auto_complete off so the
    tenant's own routing collects what it needs.
    """
    from .recruiting import build_put_applicant_request

    hire_data: dict[str, Any] = {}
    if pre_hire_wid:
        hire_data["Applicant_Reference"] = reference(WID, pre_hire_wid)
    else:
        if not (first_name and last_name):
            raise ValueError(
                "Hiring without a pre-hire needs first_name and last_name, or pass "
                "pre_hire_wid from workday_list_pre_hires / workday_create_pre_hire."
            )
        applicant = build_put_applicant_request(
            first_name=first_name,
            last_name=last_name,
            country_iso=country_iso,
            email=email,
        )
        hire_data["Applicant_Data"] = applicant["Applicant_Data"]

    hire_data["Organization_Reference"] = reference(
        organization_id_type_value, organization_id
    )
    if job_requisition_id:
        hire_data["Job_Requisition_Reference"] = reference(
            JOB_REQUISITION_ID, job_requisition_id
        )
    hire_data["Hire_Date"] = hire_date

    position_details: dict[str, Any] = {}
    if job_profile_id:
        position_details["Job_Profile_Reference"] = reference(
            JOB_PROFILE_ID, job_profile_id
        )
    if location_id:
        position_details["Location_Reference"] = reference(LOCATION_ID, location_id)
    if position_time_type_id:
        position_details["Position_Time_Type_Reference"] = reference(
            POSITION_TIME_TYPE_ID, position_time_type_id
        )
    if position_title:
        position_details["Position_Title"] = position_title
    if business_title:
        position_details["Business_Title"] = business_title

    event_data: dict[str, Any] = {}
    if employee_type_id:
        event_data["Employee_Type_Reference"] = reference(
            EMPLOYEE_TYPE_ID, employee_type_id
        )
    if position_details:
        event_data["Position_Details"] = position_details
    # A pre-hire hire must carry the event element even when it is empty.
    if pre_hire_wid or event_data:
        hire_data["Hire_Employee_Event_Data"] = event_data
    if employee_id:
        hire_data["Employee_ID"] = employee_id

    return {
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Hire_Employee_Data": hire_data,
    }
