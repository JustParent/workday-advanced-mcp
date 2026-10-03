"""Optional Workday journal endpoints, independent of source-file ingestion."""

from __future__ import annotations

from typing import Any

from .client import SERVICE_FINANCIAL_MANAGEMENT, get_client
from .operations.journals import build_submit_journal
from .references import reference
from .registry import ToolRegistrar
from .tools_write import _check_auto_complete, _submit


def register_journal_tools(registrar: ToolRegistrar, *, read_only: bool) -> None:
    async def workday_get_journal(
        journal_id: str, id_type: str = "Accounting_Journal_ID"
    ) -> Any:
        result = await get_client().call(
            SERVICE_FINANCIAL_MANAGEMENT,
            "Get_Journals",
            {
                "Request_References": {
                    "JournalEntryReference": reference(id_type, journal_id)
                }
            },
            is_read=True,
        )
        return result.response("Get_Journals")

    registrar.add(
        workday_get_journal,
        name="workday_get_journal",
        title="Get an accounting journal",
        description=(
            "Read a journal by stable ID or WID to reconcile an import or check "
            "approval/posting status."
        ),
        read_only=True,
        idempotent=True,
    )
    if read_only:
        return

    async def workday_submit_accounting_journal(
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
    ) -> Any:
        _check_auto_complete(auto_complete)
        request = build_submit_journal(
            journal_id=journal_id,
            company_id=company_id,
            currency=currency,
            ledger_type_id=ledger_type_id,
            accounting_date=accounting_date,
            journal_source_id=journal_source_id,
            lines=lines,
            memo=memo,
            auto_complete=auto_complete,
            comment=comment,
        )
        result = await _submit(
            get_client(),
            SERVICE_FINANCIAL_MANAGEMENT,
            "Submit_Accounting_Journal",
            request,
            auto_complete=auto_complete,
        )
        result["journal_id"] = journal_id
        return result

    registrar.add(
        workday_submit_accounting_journal,
        name="workday_submit_accounting_journal",
        title="Submit an accounting journal",
        description=(
            "Submit a balanced single-company, single-currency journal with a "
            "stable journal_id. Each line requires ledger_account_wid and exactly "
            "one positive debit or credit. Optional worktags are explicit "
            "{id_type,id} references. Add-only: never replaces an existing journal. "
            "Preview first; approvals run by default. Source files and recurring "
            "schedules are handled by the caller."
        ),
        read_only=False,
        destructive=True,
    )
