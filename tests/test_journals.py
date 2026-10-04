from __future__ import annotations

import copy

import httpx
import pytest
import respx

from conftest import call_tool, service_url
from test_regressions import xml_response
from test_tools_write import staffing_response
from workday_advanced_mcp.operations.journals import build_submit_journal

JOURNAL = {
    "journal_id": "SOURCE-2026-10-01",
    "company_id": "CO1",
    "currency": "GBP",
    "ledger_type_id": "ACTUALS",
    "accounting_date": "2026-10-01",
    "journal_source_id": "IMPORT",
    "lines": [
        {"ledger_account_wid": "DEBIT", "debit": "0.30"},
        {"ledger_account_wid": "CREDIT", "credit": "0.10"},
        {"ledger_account_wid": "CREDIT", "credit": "0.20"},
    ],
}


async def test_journal_preview_then_submission_is_balanced_add_only_and_approval_preserving(
    server_factory,
):
    with respx.mock:
        route = respx.post(service_url("Financial_Management")).mock(
            return_value=httpx.Response(
                200, text=staffing_response("Submit_Accounting_Journal")
            )
        )
        server = server_factory()
        preview = await call_tool(
            server,
            "workday_preview_write",
            {"operation": "workday_submit_accounting_journal", "arguments": JOURNAL},
        )
        assert preview["would_submit"] is True and route.call_count == 0
        result = await call_tool(
            server,
            "workday_submit_accounting_journal",
            {**JOURNAL, "expected_state_token": preview["state_token"]},
        )
    assert result["submitted"] is True
    xml = route.calls[0].request.content.decode()
    assert 'Add_Only="true"' in xml and "Run_Now" not in xml
    assert "<bsvc:Auto_Complete>false" in xml
    assert "SOURCE-2026-10-01" in xml


async def test_journal_read_retains_status_for_reconciliation(server_factory):
    with respx.mock:
        route = respx.post(service_url("Financial_Management")).mock(
            return_value=httpx.Response(
                200,
                text=xml_response(
                    "Get_Journals",
                    {"Response_Data": {"Journal_Entry": {"Status": "Posted"}}},
                ),
            )
        )
        result = await call_tool(
            server_factory(), "workday_get_journal", {"journal_id": "J1"}
        )
    assert result["Response_Data"]["Journal_Entry"]["Status"] == "Posted"
    assert "JournalEntryReference" in route.calls[0].request.content.decode()


@pytest.mark.parametrize(
    "bad",
    [
        {"debit": "NaN"},
        {"debit": "Infinity"},
        {"debit": "-1"},
        {"debit": "0"},
        {"debit": "0.30", "credit": "1"},
        {"debit": "0.31"},
        {"debit": "not-a-number"},
        {"debit": "0.30", "unsupported": 1},
        {"debit": "0.30", "ledger_account_wid": ""},
    ],
)
def test_invalid_journal_lines_never_reach_workday(bad):
    args = copy.deepcopy(JOURNAL)
    args["lines"][0].update(bad)
    with pytest.raises(ValueError):
        build_submit_journal(**args)


@pytest.mark.parametrize("count", [0, 1, 1001])
def test_journal_size_is_bounded(count):
    args = copy.deepcopy(JOURNAL)
    args["lines"] = [{"ledger_account_wid": "A", "debit": "1"}] * count
    with pytest.raises(ValueError, match="between 2 and 1000"):
        build_submit_journal(**args)
