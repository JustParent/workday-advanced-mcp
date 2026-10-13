"""Environment-driven configuration for the Workday Advanced MCP server.

Settings are read at call time rather than import time so the process can start
(and list its tools) before credentials are present, and so tests can change the
environment without reloading modules.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

ENV_HOST = "WORKDAY_HOST"
ENV_TENANT = "WORKDAY_TENANT"
ENV_AUTH_MODE = "WORKDAY_AUTH_MODE"
ENV_ISU_USERNAME = "WORKDAY_ISU_USERNAME"
ENV_ISU_PASSWORD = "WORKDAY_ISU_PASSWORD"
ENV_CLIENT_ID = "WORKDAY_CLIENT_ID"
ENV_CLIENT_SECRET = "WORKDAY_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "WORKDAY_REFRESH_TOKEN"
ENV_API_VERSION = "WORKDAY_API_VERSION"
ENV_READ_ONLY = "WORKDAY_READ_ONLY"
ENV_ENABLED_TOOLS = "WORKDAY_ENABLED_TOOLS"
ENV_ALLOW_AUTO_COMPLETE = "WORKDAY_ALLOW_AUTO_COMPLETE"
ENV_PROXY_URL = "WORKDAY_PROXY_URL"
ENV_HTTP_TIMEOUT = "WORKDAY_HTTP_TIMEOUT"

AUTH_MODE_ISU = "isu"
AUTH_MODE_OAUTH = "oauth"

# Harriet's first-party Workday integration pins v44.2; keep the same default so a
# tenant that works there works here.
DEFAULT_API_VERSION = "v44.2"
# Must stay well below the host's per-RPC sandbox timeout (the README's Harriet
# config uses 240) so a slow call returns a structured error instead of the RPC
# dying mid-write.
DEFAULT_HTTP_TIMEOUT = 90.0

_TRUTHY = {"1", "true", "yes", "on"}

# Hosts already warned about, so repeated settings reads stay quiet.
_warned_hosts: set[str] = set()


class ConfigError(Exception):
    """Raised when the server is not configured well enough to reach Workday."""


def parse_workday_host(raw: str) -> str:
    """Extract a Workday hostname from a bare host or a pasted tenant URL.

    Accepts ``wd3-impl-services1.workday.com``,
    ``https://wd3-impl.workday.com/acme_preview/d/home.htmld`` or a
    protocol-relative URL. Paths and ports are dropped.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" in s or s.startswith("//"):
        if s.startswith("//"):
            s = f"https:{s}"
        return urlparse(s).hostname or ""
    return s.split("/")[0].split(":")[0]


def tenant_from_url(raw: str) -> str:
    """The tenant in a pasted Workday URL: the first path segment, if any.

    ``https://wd3-impl.workday.com/acme_preview/d/home.htmld`` -> ``acme_preview``.
    Mirrors how the first-party integration parses ``Integration.domain``.
    """
    s = (raw or "").strip()
    if "://" not in s and not s.startswith("//"):
        s = f"https://{s}"
    path = urlparse(s).path.strip("/")
    if not path:
        return ""
    return path.split("/")[0]


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _enabled_tools() -> frozenset[str] | None:
    """The explicit tool allowlist, or None when every eligible tool registers."""
    raw = _env(ENV_ENABLED_TOOLS)
    if not raw:
        return None
    names = {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}
    return frozenset(names) or None


def _http_timeout() -> float:
    raw = _env(ENV_HTTP_TIMEOUT)
    if not raw:
        return DEFAULT_HTTP_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HTTP_TIMEOUT
    return value if value > 0 else DEFAULT_HTTP_TIMEOUT


@dataclass(frozen=True)
class Settings:
    """Resolved server configuration."""

    host: str
    tenant: str
    auth_mode: str
    isu_username: str
    isu_password: str
    client_id: str
    client_secret: str
    refresh_token: str
    api_version: str
    read_only: bool
    enabled_tools: frozenset[str] | None
    allow_auto_complete: bool
    proxy_url: str
    http_timeout: float

    @property
    def credentials_configured(self) -> bool:
        if self.auth_mode == AUTH_MODE_OAUTH:
            return bool(self.client_id and self.client_secret and self.refresh_token)
        return bool(self.isu_username and self.isu_password)

    @property
    def qualified_isu_username(self) -> str:
        """The ISU name as Workday wants it in WS-Security: ``user@tenant``."""
        user = self.isu_username
        if not user or "@" in user:
            return user
        return f"{user}@{self.tenant}" if self.tenant else user

    @property
    def endpoint_base(self) -> str:
        return f"https://{self.host}/ccx/service/{self.tenant}"

    @property
    def token_url(self) -> str:
        return f"https://{self.host}/ccx/oauth2/{self.tenant}/token"

    def service_url(self, service: str) -> str:
        """The SOAP endpoint for one Workday web service."""
        return f"{self.endpoint_base}/{service}/{self.api_version}"

    def require_ready(self) -> None:
        """Raise ConfigError unless this server can actually call Workday."""
        missing = []
        if not self.host:
            missing.append(ENV_HOST)
        if not self.tenant:
            missing.append(ENV_TENANT)
        if missing:
            raise ConfigError(
                "Workday connection is not configured. Set "
                + " and ".join(missing)
                + "."
            )
        if self.credentials_configured:
            return
        if self.auth_mode == AUTH_MODE_OAUTH:
            raise ConfigError(
                f"Workday OAuth credentials are not configured. Set {ENV_CLIENT_ID}, "
                f"{ENV_CLIENT_SECRET} and {ENV_REFRESH_TOKEN}, or switch "
                f"{ENV_AUTH_MODE} to '{AUTH_MODE_ISU}'."
            )
        raise ConfigError(
            f"Workday credentials are not configured. Set {ENV_ISU_USERNAME} and "
            f"{ENV_ISU_PASSWORD} to an Integration System User and its password."
        )


def resolve_auth_mode(
    explicit: str,
    isu_password: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> str:
    """Pick the auth mode, defaulting to ISU.

    An explicit mode always wins. Otherwise a complete OAuth trio with no ISU
    password means OAuth, so the catalog can ship both credential sets and let the
    customer fill in whichever they have.
    """
    mode = (explicit or "").strip().lower()
    if mode in (AUTH_MODE_ISU, AUTH_MODE_OAUTH):
        return mode
    if not isu_password and client_id and client_secret and refresh_token:
        return AUTH_MODE_OAUTH
    return AUTH_MODE_ISU


def load_settings() -> Settings:
    """Read configuration from the environment."""
    raw_host = _env(ENV_HOST)
    host = parse_workday_host(raw_host)
    tenant = _env(ENV_TENANT) or tenant_from_url(raw_host)

    if host and not host.endswith(".workday.com") and host not in _warned_hosts:
        _warned_hosts.add(host)
        print(
            f"warning: {ENV_HOST} resolves to {host!r}, which is not a "
            "*.workday.com host. Workday credentials will be sent there.",
            file=sys.stderr,
        )

    isu_password = _env(ENV_ISU_PASSWORD)
    client_id = _env(ENV_CLIENT_ID)
    client_secret = _env(ENV_CLIENT_SECRET)
    refresh_token = _env(ENV_REFRESH_TOKEN)

    return Settings(
        host=host,
        tenant=tenant,
        auth_mode=resolve_auth_mode(
            _env(ENV_AUTH_MODE), isu_password, client_id, client_secret, refresh_token
        ),
        isu_username=_env(ENV_ISU_USERNAME),
        isu_password=isu_password,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        api_version=_env(ENV_API_VERSION) or DEFAULT_API_VERSION,
        read_only=_truthy(ENV_READ_ONLY),
        enabled_tools=_enabled_tools(),
        allow_auto_complete=_truthy(ENV_ALLOW_AUTO_COMPLETE),
        proxy_url=_env(ENV_PROXY_URL),
        http_timeout=_http_timeout(),
    )
