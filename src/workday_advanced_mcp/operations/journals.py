"""Small, single-company/single-currency accounting journals.

Source-file collection and accounting policy belong to the caller. This builder
requires resolved account WIDs and balanced decimal amounts; it never infers an
account, worktag or currency conversion.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..references import reference
from ..validation import iso_date, required
from .common import business_process_parameters


def build_submit_journal(
    *,
    journal_id: str,
    company_id: str,
    currency: str,
    ledger_type_id: str,
    accounting_date: str,
    journal_source_id: str,
    lines: list[dict[str, Any]],
    memo: str | None = None,
    auto_complete: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    if not 2 <= len(lines) <= 1000:
        raise ValueError("A journal must contain between 2 and 1000 lines.")
    debit_total = Decimal(0)
    credit_total = Decimal(0)
    entries = []
    for index, line in enumerate(lines, 1):
        if set(line) - {"ledger_account_wid", "debit", "credit", "memo", "worktags"}:
            raise ValueError(f"Unsupported journal line fields on line {index}.")
        try:
            debit = Decimal(str(line.get("debit", "0")))
            credit = Decimal(str(line.get("credit", "0")))
        except InvalidOperation as exc:
            raise ValueError(f"Line {index} amounts must be decimal numbers.") from exc
        if (
            not debit.is_finite()
            or not credit.is_finite()
            or debit < 0
            or credit < 0
            or bool(debit) == bool(credit)
        ):
            raise ValueError(
                f"Line {index} must have exactly one positive debit or credit."
            )
        debit_total += debit
        credit_total += credit
        entry: dict[str, Any] = {
            "Line_Order": str(index),
            "Ledger_Account_Reference": reference(
                "WID",
                required(line.get("ledger_account_wid", ""), "ledger_account_wid"),
            ),
            "Debit_Amount": str(debit),
            "Credit_Amount": str(credit),
        }
        if line.get("memo"):
            entry["Memo"] = line["memo"]
        if line.get("worktags"):
            entry["Worktags_Reference"] = [
                reference(
                    required(tag.get("id_type", ""), "worktag id_type"),
                    required(tag.get("id", ""), "worktag id"),
                )
                for tag in line["worktags"]
            ]
        entries.append(entry)
    if debit_total != credit_total:
        raise ValueError(
            f"Journal does not balance: debits={debit_total}, credits={credit_total}."
        )
    bp = business_process_parameters(auto_complete=auto_complete, comment=comment)
    bp.pop("Run_Now")  # Financials has a different BP parameters type.
    data: dict[str, Any] = {
        "Accounting_Journal_ID": required(journal_id, "journal_id"),
        "Submit": True,
        "Company_Reference": reference(
            "Company_Reference_ID", required(company_id, "company_id")
        ),
        "Currency_Reference": reference("Currency_ID", required(currency, "currency")),
        "Ledger_Type_Reference": reference(
            "Ledger_Type_ID", required(ledger_type_id, "ledger_type_id")
        ),
        "Accounting_Date": iso_date(accounting_date, "accounting_date"),
        "Journal_Source_Reference": reference(
            "Journal_Source_ID", required(journal_source_id, "journal_source_id")
        ),
    }
    if memo:
        data["Journal_Entry_Memo"] = memo
    data["External_Reference_ID"] = journal_id
    data["Journal_Entry_Line_Replacement_Data"] = entries
    return {
        "@bsvc:Add_Only": "true",
        "Business_Process_Parameters": bp,
        "Accounting_Journal_Data": data,
    }
