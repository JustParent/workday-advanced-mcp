"""Validate real request envelopes against published Workday v44.2 XSDs.

Unlike handwritten child-order expectations, these independent fixtures detect
incorrect element names, nesting, choices, attributes, values and cardinality.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest
from lxml import etree  # type: ignore[import-untyped]

from workday_advanced_mcp.operations import (
    events,
    journals,
    lifecycle,
    organizations,
    payment_elections,
    recruiting,
    staffing,
    workers,
)
from workday_advanced_mcp.references import reference
from workday_advanced_mcp.soap import build_envelope

SCHEMAS = Path(__file__).parent / "fixtures/schemas/v44.2"
DATE = "2026-10-01"


@lru_cache
def schema(service):
    return etree.XMLSchema(etree.parse(str(SCHEMAS / f"{service}.xsd")))


def assert_schema(service, operation, request):
    root = etree.fromstring(build_envelope(operation, request).encode())
    body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    schema(service).assertValid(body[0])


def samples():
    yield (
        "Human_Resources",
        workers.OPERATION,
        workers.build_get_worker_request(
            "100", "Employee_ID", as_of_effective_date=DATE
        ),
    )
    yield "Human_Resources", workers.OPERATION, workers.build_scan_request(2, 100)
    yield (
        "Human_Resources",
        organizations.GET_ORGANIZATIONS,
        organizations.build_get_organizations_request(
            "Supervisory", 1, include_inactive=True, as_of_effective_date=DATE
        ),
    )
    yield (
        "Human_Resources",
        organizations.GET_ORGANIZATIONS,
        organizations.build_get_organization("SUP", as_of_effective_date=DATE),
    )
    yield (
        "Human_Resources",
        organizations.GET_JOB_PROFILES,
        organizations.build_get_job_profiles_request(1),
    )
    yield (
        "Human_Resources",
        organizations.GET_LOCATIONS,
        organizations.build_get_locations_request(1),
    )
    yield (
        "Human_Resources",
        "Add_Update_Organization",
        organizations.build_put_supervisory_organization(
            organization_id="NEW",
            name="New manager",
            superior_id="TOP",
            effective_date=DATE,
            subtype_name="Department",
            staffing_model="position",
            availability_date=DATE,
            location_id="LON",
            visibility_name="Everyone",
        ),
    )
    yield (
        "Human_Resources",
        "Add_Update_Organization",
        organizations.build_put_supervisory_organization(
            organization_id="NEW",
            name="New manager",
            superior_id="TOP",
            effective_date=DATE,
        ),
    )
    yield (
        "Human_Resources",
        "Get_Worker_Event_History",
        events.build_worker_history(
            "100", "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z"
        ),
    )
    yield (
        "Human_Resources",
        "Maintain_Contact_Information_for_Person_Event",
        lifecycle.build_home_address(
            worker_id="100",
            effective_date=DATE,
            address={
                "country": "GB",
                "lines": {
                    "ADDRESS_LINE_1": "10 High Street",
                    "ADDRESS_LINE_2": "Flat 2",
                },
                "city": "London",
                "region_id": "GB-LND",
                "postal_code": "N1 1AA",
            },
            address_id="ADDR-1",
        ),
    )
    yield (
        "Staffing",
        "Assign_Roles",
        lifecycle.build_assign_roles(
            organization_id="NEW",
            role_id="Manager",
            assignee_id="P1",
            effective_date=DATE,
            remove_assignee_ids=["OLD"],
        ),
    )
    yield (
        "Staffing",
        "Get_Positions",
        lifecycle.build_get_positions(organization_id="NEW", as_of_effective_date=DATE),
    )
    yield "Staffing", "Get_Positions", lifecycle.build_get_positions(position_id="P1")
    yield (
        "Staffing",
        "Create_Position",
        lifecycle.build_create_position(
            organization_id="NEW",
            position_id="P1",
            title="Analyst",
            availability_date=DATE,
            earliest_hire_date=DATE,
            job_profile_id="JP1",
            location_id="LON",
            reason_id="Growth",
        ),
    )
    yield (
        "Staffing",
        staffing.CHANGE_JOB,
        staffing.build_change_job_request(
            worker_id="100",
            position_id="P1",
            effective_date=DATE,
            reason_id="Transfer",
            supervisory_organization_id="NEW",
            job_profile_id="JP1",
            location_id="LON",
            business_title="Analyst",
            position_title="Analyst",
            position_time_type_id="Full_time",
            employee_type_id="Regular",
        ),
    )
    yield (
        "Staffing",
        staffing.CHANGE_ORGANIZATION_ASSIGNMENTS,
        staffing.build_change_organization_assignments_request(
            position_id="P1",
            worker_id="100",
            effective_date=DATE,
            assignments={"Cost_Center": ["CC1"], "Company": ["CO1"]},
        ),
    )
    yield (
        "Staffing",
        staffing.TERMINATE_EMPLOYEE,
        staffing.build_terminate_employee_request(
            worker_id="100",
            termination_date=DATE,
            primary_reason_id="Voluntary",
            last_day_of_work=DATE,
            pay_through_date=DATE,
            resignation_date=DATE,
            notification_date=DATE,
            regrettable=True,
            eligible_for_hire=True,
        ),
    )
    for person in (
        {"pre_hire_wid": "APP"},
        {"former_worker_id": "100"},
        {"first_name": "Test", "last_name": "Person", "email": "test@example.com"},
    ):
        yield (
            "Staffing",
            staffing.HIRE_EMPLOYEE,
            staffing.build_hire_employee_request(
                organization_id="NEW",
                hire_date=DATE,
                employee_id="200",
                position_id="P1",
                job_profile_id="JP1",
                location_id="LON",
                position_time_type_id="Full_time",
                employee_type_id="Regular",
                business_title="Analyst",
                position_title="Analyst",
                **person,
            ),
        )
    yield (
        "Recruiting",
        recruiting.GET_APPLICANTS,
        recruiting.build_get_applicants_request(1, 100, email="test@example.com"),
    )
    yield (
        "Recruiting",
        recruiting.PUT_APPLICANT,
        recruiting.build_put_applicant_request(
            first_name="Test",
            last_name="Person",
            country_iso="GB",
            email="test@example.com",
        ),
    )
    yield (
        "Cash_Management",
        payment_elections.GET_ENROLLMENTS,
        payment_elections.build_get_enrollments_by_reference("W1"),
    )
    yield (
        "Cash_Management",
        payment_elections.GET_ENROLLMENTS,
        payment_elections.build_get_enrollments_page(1),
    )
    for distribution in [
        {"distribution_balance": True},
        {"distribution_amount": "12.50"},
        {"distribution_percentage": "25"},
    ]:
        yield (
            "Cash_Management",
            payment_elections.SUBMIT_ENROLLMENT,
            payment_elections.build_submit_enrollment_request(
                worker_wid="W1",
                country="GB",
                currency="GBP",
                rule_groups=[
                    {
                        "payment_election_rule_id": "RULE",
                        "elections": [
                            {
                                "payment_type_id": "Direct_Deposit",
                                "bank_account": {
                                    "country": "GB",
                                    "currency": "GBP",
                                    "nickname": "Main",
                                    "account_name": "Test Person",
                                    "account_number": "12345678",
                                    "roll_number": "123456789",
                                    "account_type_id": "DDA",
                                    "bank_name": "Bank",
                                    "iban": "GB00TEST12345678123456",
                                    "bank_id_number": "123456",
                                    "bic": "TESTGB00",
                                    "branch_name": "Main",
                                    "branch_id_number": "123",
                                    "check_digit": "1",
                                },
                                **distribution,
                            }
                        ],
                    }
                ],
            ),
        )
    yield "Integrations", events.OPERATION, events.build_get_event_request("E1")
    yield (
        "Integrations",
        organizations.GET_REFERENCES,
        organizations.build_get_references_request("Organization_Role_ID", 1),
    )
    for action in ("cancel", "rescind"):
        yield (
            "Integrations",
            f"{action.title()}_Business_Process",
            events.build_recovery("E1", action, "Approved correction"),
        )
    yield (
        "Financial_Management",
        "Get_Journals",
        {"Request_References": {"JournalEntryReference": reference("WID", "J1")}},
    )
    yield (
        "Financial_Management",
        "Submit_Accounting_Journal",
        journals.build_submit_journal(
            journal_id="J1",
            company_id="CO1",
            currency="GBP",
            ledger_type_id="ACTUALS",
            accounting_date=DATE,
            journal_source_id="IMPORT",
            memo="Daily journal",
            lines=[
                {
                    "ledger_account_wid": "A1",
                    "debit": "10.25",
                    "memo": "Debit",
                    "worktags": [{"id_type": "Cost_Center_Reference_ID", "id": "CC1"}],
                },
                {"ledger_account_wid": "A2", "credit": "10.25"},
            ],
        ),
    )


@pytest.mark.parametrize(
    "service,operation,payload",
    list(samples()),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_request_matches_published_schema(service, operation, payload):
    assert_schema(service, operation, payload)


def test_schema_catches_original_misplaced_employee_id():
    request = staffing.build_hire_employee_request(
        organization_id="SUP", hire_date=DATE, pre_hire_wid="APP"
    )
    request["Hire_Employee_Data"]["Employee_ID"] = "100"
    with pytest.raises(etree.DocumentInvalid):
        assert_schema("Staffing", "Hire_Employee", request)


def test_schema_catches_original_bank_element_order():
    request = next(
        r for s, op, r in samples() if op == payment_elections.SUBMIT_ENROLLMENT
    )
    account = request["Payment_Election_Enrollment_Data"][
        "Payment_Election_Rule_Group_Data"
    ][0]["Payment_Election_Data"][0]["Worker_Bank_Account_Data"]
    account["Country_Reference"] = account.pop("Country_Reference")
    with pytest.raises(etree.DocumentInvalid):
        assert_schema("Cash_Management", payment_elections.SUBMIT_ENROLLMENT, request)


@pytest.mark.parametrize("email", [None, "test@example.com"])
def test_applicant_phone_and_email_follow_contact_schema_order(email):
    payload = recruiting.build_put_applicant_request(
        first_name="Test",
        last_name="Person",
        phone="02079460000",
        email=email,
        country_iso="GB",
    )
    assert_schema("Recruiting", "Put_Applicant", payload)


def test_hire_into_job_requisition_with_minimal_position_details():
    payload = staffing.build_hire_employee_request(
        organization_id="SUP",
        hire_date=DATE,
        pre_hire_wid="APP",
        job_requisition_id="REQ",
    )
    assert_schema("Staffing", "Hire_Employee", payload)
