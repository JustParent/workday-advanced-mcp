"""Bank details must not reach a prompt, a log or a tool result."""

from __future__ import annotations

from workday_advanced_mcp.redact import (
    account_fingerprint,
    mask_account,
    redact_data,
    redact_xml,
)


def test_an_account_number_keeps_only_its_last_four_digits() -> None:
    assert mask_account("12345678") == "****5678"


def test_a_short_value_is_masked_entirely() -> None:
    assert mask_account("123") == "****"
    assert mask_account("") == ""
    assert mask_account(None) == ""


def test_a_fingerprint_identifies_an_account_without_revealing_it() -> None:
    assert account_fingerprint("12345678") == account_fingerprint(" 1234 5678 ")
    assert account_fingerprint("12345678") != account_fingerprint("87654321")
    assert "1234" not in account_fingerprint("12345678")


def test_sensitive_keys_are_masked_at_any_depth() -> None:
    data = {
        "elections": [
            {"bank": {"Account_Number": "12345678", "Bank_Name": "Example Bank"}},
            {"bank": {"IBAN": "GB33BUKB20201555555555"}},
        ]
    }
    masked = redact_data(data)
    assert masked["elections"][0]["bank"]["Account_Number"] == "****5678"
    assert masked["elections"][0]["bank"]["Bank_Name"] == "Example Bank"
    assert masked["elections"][1]["bank"]["IBAN"] == "****5555"


def test_envelope_xml_is_masked_including_namespace_prefixes() -> None:
    xml = (
        "<bsvc:Worker_Bank_Account_Data>"
        "<bsvc:Account_Number>12345678</bsvc:Account_Number>"
        "<bsvc:IBAN>GB33BUKB20201555555555</bsvc:IBAN>"
        "<bsvc:Bank_ID_Number>40-47-84</bsvc:Bank_ID_Number>"
        "</bsvc:Worker_Bank_Account_Data>"
    )
    masked = redact_xml(xml)
    assert "12345678" not in masked
    assert "GB33BUKB20201555555555" not in masked
    # The sort code identifies the bank, not the account, and stays readable.
    assert "40-47-84" in masked
