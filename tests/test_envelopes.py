"""Golden tests for the request envelopes.

Workday rejects a request whose elements are out of document order, and the
resulting fault does not say which element is misplaced. Because this package
builds SOAP by hand with no schema to validate against, these tests are what
stands in for one: they pin the order and shape of every request the server
sends.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from workday_advanced_mcp.operations import (
    events,
    payment_elections,
    recruiting,
    staffing,
)
from workday_advanced_mcp.operations import workers as workers_op
from workday_advanced_mcp.soap import (
    PASSWORD_TEXT_TYPE,
    build_envelope,
    build_username_token,
    local_name,
)

BSVC = "urn:com.workday/bsvc"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


def attr(element: ET.Element, name: str) -> str | None:
    """Read an attribute by local name.

    ElementTree resolves ``bsvc:type`` to ``{urn:com.workday/bsvc}type`` when it
    parses the envelope back, which is itself evidence the prefixes are declared
    correctly; look under every spelling so the assertions stay readable.
    """
    for key in (f"{{{BSVC}}}{name}", f"{{{SOAP}}}{name}", name):
        if key in element.attrib:
            return element.attrib[key]
    return None


def _request_element(operation: str, request_data: dict) -> ET.Element:
    root = ET.fromstring(build_envelope(operation, request_data))
    body = next(e for e in root.iter() if local_name(e.tag) == "Body")
    return next(iter(body))


def child_order(element: ET.Element) -> list[str]:
    return [local_name(child.tag) for child in element]


def find(element: ET.Element, path: str) -> ET.Element:
    """Find a descendant by a slash-separated path of local names."""
    current = element
    for step in path.split("/"):
        found = next(
            (child for child in current if local_name(child.tag) == step), None
        )
        assert found is not None, f"{step!r} missing from {path!r}"
        current = found
    return current


def test_envelope_namespaces_and_operation_wrapper() -> None:
    request = _request_element(
        "Get_Workers", {"Response_Group": {"Include_Reference": True}}
    )
    assert local_name(request.tag) == "Get_Workers_Request"
    assert request.tag == f"{{{BSVC}}}Get_Workers_Request"


def test_username_token_carries_a_plaintext_password_type() -> None:
    envelope = build_envelope(
        "Get_Workers", {}, security=build_username_token("isu@acme_preview", "secret")
    )
    root = ET.fromstring(envelope)
    header = next(e for e in root.iter() if local_name(e.tag) == "Header")
    username = find(header, "Security/UsernameToken/Username")
    password = find(header, "Security/UsernameToken/Password")
    assert username.text == "isu@acme_preview"
    assert password.text == "secret"
    assert attr(password, "Type") == PASSWORD_TEXT_TYPE
    security = find(header, "Security")
    assert attr(security, "mustUnderstand") == "1"


def test_no_credentials_appear_without_a_security_header() -> None:
    envelope = build_envelope("Get_Workers", {})
    assert "UsernameToken" not in envelope
    assert "Workday_Common_Header" in envelope


def test_reference_ids_become_typed_id_elements() -> None:
    request = _request_element(
        "Get_Workers", workers_op.build_get_worker_request("21001", "Employee_ID")
    )
    identifier = find(request, "Request_References/Worker_Reference/ID")
    assert identifier.text == "21001"
    assert attr(identifier, "type") == "Employee_ID"


def test_unknown_worker_ids_do_not_fault_the_request() -> None:
    request = _request_element(
        "Get_Workers", workers_op.build_get_worker_request("21001", "Employee_ID")
    )
    references = find(request, "Request_References")
    assert attr(references, "Skip_Non_Existing_Instances") == "true"


# --------------------------------------------------------------------------
# Write operations. Each expected order is taken from the Workday Web Services
# v44.2 schema for that operation.
# --------------------------------------------------------------------------


def test_change_organization_assignments_order_and_effective_date_attribute() -> None:
    request = _request_element(
        staffing.CHANGE_ORGANIZATION_ASSIGNMENTS,
        staffing.build_change_organization_assignments_request(
            position_id="POS-100",
            effective_date="2026-04-01",
            assignments={"Cost_Center": ["CC_200"], "Company": ["CO_1"]},
            worker_id="21001",
        ),
    )
    assert child_order(request) == [
        "Business_Process_Parameters",
        "Change_Organization_Assignments_Data",
    ]

    data = find(request, "Change_Organization_Assignments_Data")
    # Effective date is an attribute on this operation, not a child element.
    assert attr(data, "Effective_Date") == "2026-04-01"
    assert "Effective_Date" not in child_order(data)
    assert child_order(data) == [
        "Position_Reference",
        "Worker_Reference",
        "Position_Organization_Assignments_Data",
    ]

    # Company precedes Cost_Center in the schema regardless of argument order.
    assignments = find(data, "Position_Organization_Assignments_Data")
    assert child_order(assignments) == [
        "Company_Assignments_Reference",
        "Cost_Center_Assignments_Reference",
    ]
    cost_centre = find(assignments, "Cost_Center_Assignments_Reference/ID")
    assert cost_centre.text == "CC_200"
    assert attr(cost_centre, "type") == "Cost_Center_Reference_ID"


def test_change_organization_assignments_rejects_an_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Supervisory"):
        staffing.build_change_organization_assignments_request(
            position_id="POS-100",
            effective_date="2026-04-01",
            assignments={"Supervisory": ["SUP-1"]},
        )


def test_change_organization_assignments_rejects_an_empty_set() -> None:
    with pytest.raises(ValueError, match="No organization assignments"):
        staffing.build_change_organization_assignments_request(
            position_id="POS-100", effective_date="2026-04-01", assignments={}
        )


def test_change_job_order_and_supervisory_organization_placement() -> None:
    request = _request_element(
        staffing.CHANGE_JOB,
        staffing.build_change_job_request(
            worker_id="21001",
            effective_date="2026-04-01",
            reason_id="Transfer",
            position_id="POS-100",
            supervisory_organization_id="SUP-NEW",
            job_profile_id="JP-ENG",
            location_id="LOC-LON",
            business_title="Staff Engineer",
        ),
    )
    assert child_order(request) == ["Business_Process_Parameters", "Change_Job_Data"]

    data = find(request, "Change_Job_Data")
    assert child_order(data) == [
        "Worker_Reference",
        "Position_Reference",
        "Effective_Date",
        "Change_Job_Detail_Data",
    ]

    detail = find(data, "Change_Job_Detail_Data")
    # Reason is required and comes first; the supervisory org lives here, not in
    # the organization assignment operation.
    assert child_order(detail) == [
        "Reason_Reference",
        "Supervisory_Organization_Reference",
        "Job_Details_Data",
    ]
    assert child_order(find(detail, "Job_Details_Data")) == [
        "Job_Profile_Reference",
        "Business_Title",
        "Location_Reference",
    ]


def test_terminate_employee_order() -> None:
    request = _request_element(
        staffing.TERMINATE_EMPLOYEE,
        staffing.build_terminate_employee_request(
            worker_id="21001",
            termination_date="2026-05-31",
            primary_reason_id="Voluntary_Resignation",
            last_day_of_work="2026-05-31",
            regrettable=True,
        ),
    )
    assert child_order(request) == [
        "Business_Process_Parameters",
        "Terminate_Employee_Data",
    ]

    data = find(request, "Terminate_Employee_Data")
    assert child_order(data) == [
        "Employee_Reference",
        "Termination_Date",
        "Terminate_Event_Data",
    ]
    event = find(data, "Terminate_Event_Data")
    assert child_order(event) == [
        "Last_Day_of_Work",
        "Primary_Reason_Reference",
        "Regrettable_Reference",
    ]
    # Workday models this as a Yes/No business object rather than a boolean.
    assert find(event, "Regrettable_Reference/ID").text == "Yes"


def test_hire_employee_order_matches_the_schema() -> None:
    request = _request_element(
        staffing.HIRE_EMPLOYEE,
        staffing.build_hire_employee_request(
            organization_id="SUP-ENG",
            hire_date="2026-06-01",
            pre_hire_wid="prehire-wid",
            job_profile_id="JP-ENG",
            location_id="LOC-LON",
            employee_id="21055",
        ),
    )
    data = find(request, "Hire_Employee_Data")
    assert child_order(data) == [
        "Applicant_Reference",
        "Organization_Reference",
        "Hire_Date",
        "Hire_Employee_Event_Data",
        "Employee_ID",
    ]
    assert child_order(find(data, "Hire_Employee_Event_Data/Position_Details")) == [
        "Job_Profile_Reference",
        "Location_Reference",
    ]


def test_hire_without_a_pre_hire_builds_the_applicant_inline() -> None:
    request = _request_element(
        staffing.HIRE_EMPLOYEE,
        staffing.build_hire_employee_request(
            organization_id="SUP-ENG",
            hire_date="2026-06-01",
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            country_iso="GB",
        ),
    )
    data = find(request, "Hire_Employee_Data")
    assert child_order(data)[0] == "Applicant_Data"
    detail = find(
        data, "Applicant_Data/Personal_Data/Name_Data/Legal_Name_Data/Name_Detail_Data"
    )
    assert find(detail, "First_Name").text == "Ada"
    assert (
        attr(find(detail, "Country_Reference/ID"), "type") == "ISO_3166-1_Alpha-2_Code"
    )


def test_hire_without_a_name_or_pre_hire_is_refused() -> None:
    with pytest.raises(ValueError, match="pre_hire_wid"):
        staffing.build_hire_employee_request(
            organization_id="SUP-ENG", hire_date="2026-06-01"
        )


def test_business_process_parameters_default_to_tenant_approval_routing() -> None:
    request = _request_element(
        staffing.CHANGE_JOB,
        staffing.build_change_job_request(
            worker_id="21001",
            effective_date="2026-04-01",
            reason_id="Transfer",
            supervisory_organization_id="SUP-NEW",
            comment="Bob sync",
        ),
    )
    parameters = find(request, "Business_Process_Parameters")
    assert find(parameters, "Auto_Complete").text == "false"
    assert find(parameters, "Run_Now").text == "true"
    assert "Bob sync" in (find(parameters, "Comment_Data/Comment").text or "")


def test_put_applicant_requires_contact_data_shape() -> None:
    request = _request_element(
        recruiting.PUT_APPLICANT,
        recruiting.build_put_applicant_request(
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            country_iso="GB",
        ),
    )
    contact = find(request, "Applicant_Data/Personal_Data/Contact_Data")
    assert find(contact, "Email_Address_Data/Email_Address").text == "ada@example.com"
    usage = find(contact, "Email_Address_Data/Usage_Data/Type_Data")
    assert attr(usage, "Primary") == "true"


def test_payment_election_submission_order_and_retain_attribute() -> None:
    request = _request_element(
        payment_elections.SUBMIT_ENROLLMENT,
        payment_elections.build_submit_enrollment_request(
            worker_wid="worker-wid-1",
            country="GB",
            currency="GBP",
            rule_groups=[
                {
                    "payment_election_rule_id": "rule-wid",
                    "elections": [
                        {
                            "order": 1,
                            "payment_type_id": "Direct_Deposit",
                            "distribution_balance": True,
                            "bank_account": {
                                "account_number": "12345678",
                                "bank_id_number": "40-47-84",
                                "bank_name": "Example Bank",
                                "country": "GB",
                            },
                        }
                    ],
                }
            ],
        ),
    )
    assert attr(request, "Retain_Unused_Worker_Bank_Accounts") == "true"
    assert child_order(request) == [
        "Business_Process_Parameters",
        "Payment_Election_Enrollment_Data",
    ]

    data = find(request, "Payment_Election_Enrollment_Data")
    assert child_order(data) == [
        "Role_ID_Reference",
        "Country_Reference",
        "Currency_Reference",
        "Payment_Election_Rule_Group_Data",
    ]
    group = find(data, "Payment_Election_Rule_Group_Data")
    assert child_order(group) == [
        "Payment_Election_Rule_Reference",
        "Payment_Election_Data",
    ]
    election = find(group, "Payment_Election_Data")
    assert child_order(election) == [
        "Order",
        "Payment_Election_Rule_Reference",
        "Country_Reference",
        "Currency_Reference",
        "Payment_Type_Reference",
        "Worker_Bank_Account_Data",
        "Distribution_Balance",
    ]
    assert child_order(find(election, "Worker_Bank_Account_Data")) == [
        "Bank_Name",
        "Bank_ID_Number",
        "Account_Number",
        "Country_Reference",
    ]


def test_payment_election_refuses_an_empty_set() -> None:
    with pytest.raises(ValueError, match="delete every election"):
        payment_elections.build_submit_enrollment_request(
            worker_wid="w", country="GB", currency="GBP", rule_groups=[]
        )


def test_payment_election_requires_exactly_one_distribution() -> None:
    def build(**election):
        return payment_elections.build_submit_enrollment_request(
            worker_wid="w",
            country="GB",
            currency="GBP",
            rule_groups=[
                {
                    "payment_election_rule_id": "rule",
                    "elections": [
                        {
                            "payment_type_id": "Direct_Deposit",
                            "bank_account": {"iban": "GB33BUKB20201555555555"},
                            **election,
                        }
                    ],
                }
            ],
        )

    with pytest.raises(ValueError, match="exactly one"):
        build(distribution_balance=True, distribution_percentage=50)
    with pytest.raises(ValueError, match="distribution_balance"):
        build()


def test_payment_election_requires_an_account_identifier() -> None:
    with pytest.raises(ValueError, match="account_number"):
        payment_elections.build_submit_enrollment_request(
            worker_wid="w",
            country="GB",
            currency="GBP",
            rule_groups=[
                {
                    "payment_election_rule_id": "rule",
                    "elections": [
                        {
                            "payment_type_id": "Direct_Deposit",
                            "distribution_balance": True,
                            "bank_account": {"bank_name": "Example Bank"},
                        }
                    ],
                }
            ],
        )


def test_get_event_request_shape() -> None:
    request = _request_element(
        events.OPERATION, events.build_get_event_request("event-wid")
    )
    identifier = find(request, "Request_References/Event_Reference/ID")
    assert identifier.text == "event-wid"
    assert attr(identifier, "type") == "WID"
