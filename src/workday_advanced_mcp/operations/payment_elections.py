"""Payment elections: where a worker's pay is sent.

Submit_Payment_Election_Enrollment **replaces** a worker's entire set of payment
elections, so sending only the account that changed silently deletes the rest.
Every write therefore reads the complete current set first. Optional merging
requires readable, unmasked values for every account that must be carried forward.

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
from .common import financial_business_process_parameters

GET_ENROLLMENTS = "Get_Payment_Election_Enrollments"
SUBMIT_ENROLLMENT = "Submit_Payment_Election_Enrollment"

PAGE_SIZE = 100

# Worker_Bank_Account_Data children, in schema order.
BANK_ACCOUNT_ELEMENTS = (
    "Country_Reference",
    "Currency_Reference",
    "Bank_Account_Nickname",
    "Bank_Account_Name",
    "Account_Number",
    "Roll_Number",
    "Account_Type_Code",
    "Account_Type_Reference",
    "Bank_Name",
    "IBAN",
    "Bank_ID_Number",
    "BIC",
    "Branch_Name",
    "Branch_ID_Number",
    "Check_Digit",
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
        "account_type": summarize_reference(
            data.get("Account_Type_Reference"), preferred=("Bank_Account_Type_ID", WID)
        ).get("id", ""),
        "account_name": data.get("Bank_Account_Name") or "",
        "roll_number_masked": mask_account(data.get("Roll_Number")),
        "roll_fingerprint": account_fingerprint(data.get("Roll_Number")),
        "currency": summarize_reference(
            data.get("Currency_Reference"), preferred=(CURRENCY_ID, WID)
        ).get("id", ""),
        "branch_name": data.get("Branch_Name") or "",
        "branch_id_number": data.get("Branch_ID_Number") or "",
        "check_digit": data.get("Check_Digit") or "",
        "bank_name": data.get("Bank_Name") or "",
        "bank_id_number": data.get("Bank_ID_Number") or "",
        "account_number_masked": mask_account(data.get("Account_Number")),
        "account_number_fingerprint": account_fingerprint(data.get("Account_Number")),
        "iban_fingerprint": account_fingerprint(data.get("IBAN")),
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
                    "country": summarize_reference(
                        election.get("Country_Reference"),
                        preferred=(COUNTRY_ISO_ALPHA2, WID),
                    ).get("id", ""),
                    "currency": summarize_reference(
                        election.get("Currency_Reference"), preferred=(CURRENCY_ID, WID)
                    ).get("id", ""),
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
                    in ("true", "1"),
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
        "Business_Process_Parameters": financial_business_process_parameters(
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
    if account.get("currency"):
        values["Currency_Reference"] = reference(CURRENCY_ID, account["currency"])
    for key, element in {
        "account_name": "Bank_Account_Name",
        "roll_number": "Roll_Number",
        "branch_name": "Branch_Name",
        "branch_id_number": "Branch_ID_Number",
        "check_digit": "Check_Digit",
    }.items():
        if account.get(key):
            values[element] = account[key]

    if not (values.get("Account_Number") or values.get("IBAN")):
        raise ValueError(
            "A bank account needs either account_number (with bank_id_number) or iban."
        )
    for key in ("account_number", "iban", "roll_number"):
        if account.get(key) and not account_fingerprint(str(account[key])):
            raise ValueError(f"{key} is masked; supply the full value before writing.")
    if values.get("Account_Number") and not (
        values.get("Bank_ID_Number") or values.get("IBAN")
    ):
        raise ValueError("account_number requires bank_id_number or iban.")
    return {key: values[key] for key in BANK_ACCOUNT_ELEMENTS if key in values}


def _distribution(election: dict[str, Any]) -> dict[str, Any]:
    """Exactly one of amount, percentage or balance, as Workday requires."""
    amount = election.get("distribution_amount")
    percentage = election.get("distribution_percentage")
    balance = election.get("distribution_balance")

    chosen = (
        int(amount not in (None, ""))
        + int(percentage not in (None, ""))
        + int(balance is True)
    )
    if chosen != 1:
        raise ValueError(
            "An election takes exactly one of distribution_amount, "
            "distribution_percentage or distribution_balance."
        )
    if amount not in (None, ""):
        _positive_decimal(amount, "distribution_amount")
        return {"Distribution_Amount": amount}
    if percentage not in (None, ""):
        _positive_decimal(percentage, "distribution_percentage", maximum=100)
        return {"Distribution_Percentage": percentage}
    if balance:
        return {"Distribution_Balance": True}
    raise ValueError(
        "An election needs distribution_balance: true, or a distribution_amount or "
        "distribution_percentage."
    )


def _positive_decimal(value: Any, name: str, maximum: int | None = None) -> None:
    from decimal import Decimal, InvalidOperation

    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or (maximum and number > maximum):
            raise ValueError(f"{name} is out of range.")
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric.") from exc


def same_enrollment(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Compare the entire election, only when account identity is provable."""
    from decimal import Decimal

    def normalize(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            if "id_type" in value:
                return [value.get("id_type"), value.get("id")]
            return {k: normalize(v, k) for k, v in value.items() if k != "worker"}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if (
            key in {"order", "distribution_amount", "distribution_percentage"}
            and value != ""
        ):
            return str(Decimal(str(value)).normalize())
        return value

    for enrollment in (current, desired):
        for group in enrollment.get("rule_groups", []):
            for election in group.get("elections", []):
                account = election.get("bank_account")
                if account and not account.get("account_fingerprint"):
                    return False
                if account and any(
                    account.get(f"{key}_masked")
                    and not account.get(f"{fingerprint}_fingerprint")
                    for key, fingerprint in [
                        ("account_number", "account_number"),
                        ("iban", "iban"),
                        ("roll_number", "roll"),
                    ]
                ):
                    return False
    return normalize(current) == normalize(desired)


def merge_groups(
    entries: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Carry forward untouched rule groups only when full bank values are readable.

    There is no bank-account reference field in the v44.2 election input schema.
    Never manufacture full numbers from masked read responses.
    """
    replacement_ids = {str(g.get("payment_election_rule_id", "")) for g in incoming}
    merged = list(incoming)
    from ..references import extract_ids

    for entry in entries:
        data = entry.get("Payment_Election_Enrollment_Data") or {}
        for group in as_list(data.get("Payment_Election_Rule_Group_Data")):
            ref = summarize_reference(group.get("Payment_Election_Rule_Reference"))
            if replacement_ids.intersection(
                extract_ids(group.get("Payment_Election_Rule_Reference")).values()
            ):
                continue
            elections = []
            for raw in as_list(group.get("Payment_Election_Data")):
                parsed = parse_enrollment(
                    {
                        "Payment_Election_Enrollment_Data": {
                            "Payment_Election_Rule_Group_Data": {
                                "Payment_Election_Data": raw
                            }
                        }
                    }
                )["rule_groups"][0]["elections"][0]
                bank = raw.get("Worker_Bank_Account_Data") or {}
                account = {
                    key: bank.get(element)
                    for key, element in {
                        "nickname": "Bank_Account_Nickname",
                        "account_name": "Bank_Account_Name",
                        "account_number": "Account_Number",
                        "roll_number": "Roll_Number",
                        "bank_name": "Bank_Name",
                        "iban": "IBAN",
                        "bank_id_number": "Bank_ID_Number",
                        "bic": "BIC",
                        "branch_name": "Branch_Name",
                        "branch_id_number": "Branch_ID_Number",
                        "check_digit": "Check_Digit",
                    }.items()
                    if bank.get(element)
                }
                if bank:
                    account.update(
                        account_type_id=parsed["bank_account"]["account_type"],
                        country=parsed["bank_account"]["country"],
                        currency=parsed["bank_account"]["currency"],
                    )
                    _build_bank_account(account)  # refuses masked/missing values
                election = {
                    key: parsed[key] for key in ("order", "country", "currency")
                }
                election["payment_type_id"] = parsed["payment_type"].get("id", "")
                for key in (
                    "distribution_amount",
                    "distribution_percentage",
                    "distribution_balance",
                ):
                    if parsed[key] not in ("", False):
                        election[key] = parsed[key]
                if bank:
                    election["bank_account"] = account
                elections.append(election)
            merged.append(
                {
                    "payment_election_rule_id": ref.get("id"),
                    "payment_election_rule_id_type": ref.get("id_type"),
                    "elections": elections,
                }
            )
    return merged
