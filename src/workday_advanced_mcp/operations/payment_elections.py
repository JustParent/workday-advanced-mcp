"""Payment elections: where a worker's pay is sent.

Submit_Payment_Election_Enrollment **replaces** a worker's entire set of payment
elections, so sending only the account that changed silently deletes the rest.
Every write therefore starts from the current set and merges into it; the tool
layer refuses to skip that read.

Account numbers are masked on the way out (see ``redact``); they are never
returned to a caller or written to a log.

Schema reference: Workday Web Services v44.2, Cash_Management service.
"""

from __future__ import annotations

from typing import Any

from ..redact import account_fingerprint, mask_account
from ..references import (
    COUNTRY_ISO_ALPHA2,
    CURRENCY_ID,
    PAYMENT_TYPE_ID,
    WID,
    as_list,
    reference,
    summarize_reference,
)
from .common import business_process_parameters

GET_ENROLLMENTS = "Get_Payment_Election_Enrollments"
SUBMIT_ENROLLMENT = "Submit_Payment_Election_Enrollment"

PAGE_SIZE = 100

# Worker_Bank_Account_Data children, in schema order.
BANK_ACCOUNT_ELEMENTS = (
    "Bank_Account_Nickname",
    "Account_Type_Reference",
    "Bank_Name",
    "Bank_ID_Number",
    "Account_Number",
    "IBAN",
    "BIC",
    "Country_Reference",
)


def build_get_enrollments_by_reference(worker_wid: str) -> dict[str, Any]:
    """Fetch one worker's enrollment directly.

    Request_References takes a Payment_Election_Enrollment_Reference, which is
    keyed by the enrollable role rather than a separate enrollment ID, so the
    worker's WID is what identifies it.
    """
    return {
        "Request_References": {
            "Payment_Election_Enrollment_Reference": reference(WID, worker_wid)
        },
        "Response_Group": {"Include_Reference": True},
    }


def build_get_enrollments_page(page: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    """Page through every enrollment, for tenants where the direct lookup misses."""
    return {
        "Response_Filter": {"Page": page, "Count": count},
        "Response_Group": {"Include_Reference": True},
    }


def enrollments_in(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("Payment_Election_Enrollment_Response_Data") or response.get(
        "Response_Data"
    )
    if not isinstance(data, dict):
        return []
    return [
        e
        for e in as_list(data.get("Payment_Election_Enrollment"))
        if isinstance(e, dict)
    ]


def _bank_account(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {
        "nickname": data.get("Bank_Account_Nickname") or "",
        "account_type": summarize_reference(data.get("Account_Type_Reference")).get(
            "descriptor", ""
        ),
        "bank_name": data.get("Bank_Name") or "",
        "bank_id_number": data.get("Bank_ID_Number") or "",
        "account_number_masked": mask_account(data.get("Account_Number")),
        "account_fingerprint": account_fingerprint(
            data.get("Account_Number") or data.get("IBAN")
        ),
        "iban_masked": mask_account(data.get("IBAN")),
        "bic": data.get("BIC") or "",
        "country": summarize_reference(data.get("Country_Reference")).get("id", ""),
    }


def parse_enrollment(enrollment: dict[str, Any]) -> dict[str, Any]:
    """Flatten one enrollment, with account numbers masked."""
    data = enrollment.get("Payment_Election_Enrollment_Data")
    if not isinstance(data, dict):
        return {}

    groups: list[dict[str, Any]] = []
    for group in as_list(data.get("Payment_Election_Rule_Group_Data")):
        if not isinstance(group, dict):
            continue
        elections: list[dict[str, Any]] = []
        for election in as_list(group.get("Payment_Election_Data")):
            if not isinstance(election, dict):
                continue
            elections.append(
                {
                    "order": election.get("Order") or "",
                    "payment_type": summarize_reference(
                        election.get("Payment_Type_Reference"),
                        preferred=(PAYMENT_TYPE_ID, WID),
                    ),
                    "distribution_amount": election.get("Distribution_Amount") or "",
                    "distribution_percentage": election.get("Distribution_Percentage")
                    or "",
                    "distribution_balance": str(
                        election.get("Distribution_Balance") or ""
                    ).lower()
                    == "true",
                    "bank_account": _bank_account(
                        election.get("Worker_Bank_Account_Data")
                    ),
                }
            )
        groups.append(
            {
                "payment_election_rule": summarize_reference(
                    group.get("Payment_Election_Rule_Reference")
                ),
                "elections": elections,
            }
        )

    return {
        "worker": summarize_reference(data.get("Role_ID_Reference")),
        "country": summarize_reference(data.get("Country_Reference")).get("id", ""),
        "currency": summarize_reference(data.get("Currency_Reference")).get("id", ""),
        "rule_groups": groups,
    }


def build_submit_enrollment_request(
    *,
    worker_wid: str,
    country: str,
    currency: str,
    rule_groups: list[dict[str, Any]],
    retain_unused_bank_accounts: bool = True,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    """Replace a worker's payment elections with the full set given.

    Document order:
        @Retain_Unused_Worker_Bank_Accounts
        Business_Process_Parameters
        Payment_Election_Enrollment_Data
            Role_ID_Reference       (the worker, as a payment-election enrollable)
            Country_Reference
            Currency_Reference
            Payment_Election_Rule_Group_Data
                Payment_Election_Rule_Reference
                Payment_Election_Data
                    Order
                    Payment_Election_Rule_Reference
                    Country_Reference
                    Currency_Reference
                    Payment_Type_Reference
                    Worker_Bank_Account_Data
                    Distribution_Amount | Distribution_Percentage | Distribution_Balance

    ``rule_groups`` is the complete intended set: whatever is omitted is removed.
    """
    if not rule_groups:
        raise ValueError(
            "Refusing to submit an empty payment election set: that would delete every "
            "election the worker has. Include the elections to keep."
        )

    groups: list[dict[str, Any]] = []
    for group in rule_groups:
        rule_id = str(group.get("payment_election_rule_id") or "").strip()
        if not rule_id:
            raise ValueError(
                "Each rule group needs payment_election_rule_id, taken from the "
                "worker's current elections (workday_get_payment_elections)."
            )
        rule_id_type = str(group.get("payment_election_rule_id_type") or WID)
        rule_reference = reference(rule_id_type, rule_id)

        elections: list[dict[str, Any]] = []
        for election in group.get("elections") or []:
            elections.append(
                _build_election(
                    election,
                    rule_reference=rule_reference,
                    country=country,
                    currency=currency,
                )
            )
        if not elections:
            raise ValueError(
                f"Rule group {rule_id} has no elections. Workday requires at least one."
            )
        groups.append(
            {
                "Payment_Election_Rule_Reference": rule_reference,
                "Payment_Election_Data": elections,
            }
        )

    return {
        "@bsvc:Retain_Unused_Worker_Bank_Accounts": (
            "true" if retain_unused_bank_accounts else "false"
        ),
        "Business_Process_Parameters": business_process_parameters(
            auto_complete=auto_complete, comment=comment
        ),
        "Payment_Election_Enrollment_Data": {
            "Role_ID_Reference": reference(WID, worker_wid),
            "Country_Reference": reference(COUNTRY_ISO_ALPHA2, country),
            "Currency_Reference": reference(CURRENCY_ID, currency),
            "Payment_Election_Rule_Group_Data": groups,
        },
    }


def _build_election(
    election: dict[str, Any],
    *,
    rule_reference: dict[str, Any],
    country: str,
    currency: str,
) -> dict[str, Any]:
    payment_type = str(election.get("payment_type_id") or "").strip()
    if not payment_type:
        raise ValueError(
            "Each election needs payment_type_id (for example 'Direct_Deposit')."
        )

    data: dict[str, Any] = {
        "Order": election.get("order") or 1,
        "Payment_Election_Rule_Reference": rule_reference,
        "Country_Reference": reference(
            COUNTRY_ISO_ALPHA2, election.get("country") or country
        ),
        "Currency_Reference": reference(
            CURRENCY_ID, election.get("currency") or currency
        ),
        "Payment_Type_Reference": reference(PAYMENT_TYPE_ID, payment_type),
    }

    account = election.get("bank_account")
    if isinstance(account, dict) and account:
        data["Worker_Bank_Account_Data"] = _build_bank_account(account)

    distribution = _distribution(election)
    data.update(distribution)
    return data


def _build_bank_account(account: dict[str, Any]) -> dict[str, Any]:
    """Bank account details, emitted in the schema's element order."""
    values: dict[str, Any] = {}
    if account.get("nickname"):
        values["Bank_Account_Nickname"] = account["nickname"]
    if account.get("account_type_id"):
        values["Account_Type_Reference"] = reference(
            "Bank_Account_Type_ID", account["account_type_id"]
        )
    if account.get("bank_name"):
        values["Bank_Name"] = account["bank_name"]
    if account.get("bank_id_number"):
        values["Bank_ID_Number"] = account["bank_id_number"]
    if account.get("account_number"):
        values["Account_Number"] = account["account_number"]
    if account.get("iban"):
        values["IBAN"] = account["iban"]
    if account.get("bic"):
        values["BIC"] = account["bic"]
    if account.get("country"):
        values["Country_Reference"] = reference(COUNTRY_ISO_ALPHA2, account["country"])

    if not (values.get("Account_Number") or values.get("IBAN")):
        raise ValueError(
            "A bank account needs either account_number (with bank_id_number) or iban."
        )
    return values


def _distribution(election: dict[str, Any]) -> dict[str, Any]:
    """Exactly one of amount, percentage or balance, as Workday requires."""
    amount = election.get("distribution_amount")
    percentage = election.get("distribution_percentage")
    balance = election.get("distribution_balance")

    chosen = [v for v in (amount, percentage, balance) if v not in (None, "", False)]
    if len(chosen) > 1:
        raise ValueError(
            "An election takes exactly one of distribution_amount, "
            "distribution_percentage or distribution_balance."
        )
    if amount not in (None, ""):
        return {"Distribution_Amount": amount}
    if percentage not in (None, ""):
        return {"Distribution_Percentage": percentage}
    if balance:
        return {"Distribution_Balance": True}
    raise ValueError(
        "An election needs distribution_balance: true, or a distribution_amount or "
        "distribution_percentage."
    )
