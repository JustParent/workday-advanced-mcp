"""Configuration parsing, especially the choices that decide how we authenticate."""

from __future__ import annotations

import pytest

from workday_advanced_mcp.config import (
    AUTH_MODE_ISU,
    AUTH_MODE_OAUTH,
    DEFAULT_API_VERSION,
    DEFAULT_HTTP_TIMEOUT,
    ConfigError,
    load_settings,
    parse_workday_host,
    resolve_auth_mode,
    tenant_from_url,
)


def test_host_accepts_a_pasted_tenant_url() -> None:
    url = "https://wd3-impl.workday.com/acme_preview/d/home.htmld"
    assert parse_workday_host(url) == "wd3-impl.workday.com"
    assert tenant_from_url(url) == "acme_preview"


def test_host_accepts_a_bare_hostname() -> None:
    assert parse_workday_host("wd3-impl-services1.workday.com:443/x") == (
        "wd3-impl-services1.workday.com"
    )


def test_tenant_is_inferred_from_a_pasted_url(set_env) -> None:
    set_env(
        WORKDAY_HOST="https://wd3-impl.workday.com/acme_preview/d/home.htmld",
        WORKDAY_TENANT=None,
    )
    settings = load_settings()
    assert settings.tenant == "acme_preview"
    assert settings.host == "wd3-impl.workday.com"


def test_isu_username_is_qualified_with_the_tenant() -> None:
    assert load_settings().qualified_isu_username == "isu_harriet@acme_preview"


def test_an_already_qualified_isu_username_is_left_alone(set_env) -> None:
    set_env(WORKDAY_ISU_USERNAME="isu_harriet@other_tenant")
    assert load_settings().qualified_isu_username == "isu_harriet@other_tenant"


def test_auth_mode_defaults_to_isu() -> None:
    assert resolve_auth_mode("", "password", "", "", "") == AUTH_MODE_ISU


def test_oauth_is_inferred_when_only_oauth_credentials_are_present() -> None:
    assert resolve_auth_mode("", "", "id", "secret", "refresh") == AUTH_MODE_OAUTH


def test_an_explicit_auth_mode_wins_over_inference() -> None:
    assert resolve_auth_mode("isu", "", "id", "secret", "refresh") == AUTH_MODE_ISU


def test_service_url_uses_the_configured_version(set_env) -> None:
    set_env(WORKDAY_API_VERSION="v45.0")
    settings = load_settings()
    assert settings.service_url("Staffing").endswith("/Staffing/v45.0")
    assert settings.token_url.endswith("/ccx/oauth2/acme_preview/token")


def test_defaults_match_the_first_party_integration(set_env) -> None:
    set_env(WORKDAY_API_VERSION=None, WORKDAY_HTTP_TIMEOUT=None)
    settings = load_settings()
    assert settings.api_version == DEFAULT_API_VERSION
    assert settings.http_timeout == DEFAULT_HTTP_TIMEOUT


def test_enabled_tools_parses_a_comma_list(set_env) -> None:
    set_env(WORKDAY_ENABLED_TOOLS=" workday_get_worker , workday_find_worker ")
    assert load_settings().enabled_tools == frozenset(
        {"workday_get_worker", "workday_find_worker"}
    )


def test_auto_complete_is_refused_unless_opted_in(set_env) -> None:
    assert load_settings().allow_auto_complete is False
    set_env(WORKDAY_ALLOW_AUTO_COMPLETE="true")
    assert load_settings().allow_auto_complete is True


def test_missing_credentials_name_what_to_set(set_env) -> None:
    set_env(WORKDAY_ISU_PASSWORD=None)
    with pytest.raises(ConfigError, match="WORKDAY_ISU_PASSWORD"):
        load_settings().require_ready()


def test_missing_host_is_reported_before_credentials(set_env) -> None:
    set_env(WORKDAY_HOST=None, WORKDAY_TENANT=None)
    with pytest.raises(ConfigError, match="WORKDAY_HOST"):
        load_settings().require_ready()
