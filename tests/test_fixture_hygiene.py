"""Keep real people, credentials and tenant details out of the repository.

docs/tenant-validation.md asks for sanitized samples only. These checks catch the
usual ways a live capture leaks: a real email address, an IP address, a Workday id,
a request id, a tenant name or the data centre a customer is hosted in.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
# .env holds real credentials locally and is never committed; this file holds the
# patterns below.
SKIP_FILES = {".env", "test_fixture_hygiene.py"}
TEXT_SUFFIXES = {".py", ".md", ".xsd", ".json", ".toml", ".yml", ".yaml", ".txt"}

SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}
# Loopback and the documentation ranges (RFC 5737) are the only addresses allowed.
SAFE_IP = re.compile(
    r"127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+"
)
# Any other data centre would name the one a customer is hosted in.
EXAMPLE_DATA_CENTRES = {"wd3", "wd5"}
EXAMPLE_TENANTS = {"acme_preview"}

# name -> (pattern, whether a match is an allowed placeholder)
CHECKS: dict[str, tuple[re.Pattern[str], Callable[[re.Match[str]], bool]]] = {
    "email address outside the example domains": (
        re.compile(
            r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
        ),
        lambda m: m.group(1).lower() in SAFE_EMAIL_DOMAINS,
    ),
    "IPv4 address outside loopback and the documentation ranges": (
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        lambda m: bool(SAFE_IP.fullmatch(m.group(0))),
    ),
    "32-character hex id (a Workday WID)": (
        re.compile(r"\b[0-9a-f]{32}\b"),
        lambda m: False,
    ),
    "Workday instance id such as 12$3456 (single digits are placeholders)": (
        re.compile(r"\b\d+\$\d+\b"),
        lambda m: bool(re.fullmatch(r"\d\$\d", m.group(0))),
    ),
    "Workday or Cloudflare request id": (
        re.compile(r"GID\||x-wd-gid|cf-ray", re.IGNORECASE),
        lambda m: False,
    ),
    "data centre other than the example ones": (
        re.compile(r"\bwd(\d+)(?=[.-])"),
        lambda m: f"wd{m.group(1)}" in EXAMPLE_DATA_CENTRES,
    ),
    "tenant other than the example one in a service address": (
        re.compile(r"/ccx/(?:service|oauth2|api/v1)/([A-Za-z0-9_]+)"),
        lambda m: m.group(1) in EXAMPLE_TENANTS,
    ),
}


def repo_files() -> Iterator[Path]:
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(directory) / name
            if path.suffix in TEXT_SUFFIXES and name not in SKIP_FILES:
                yield path


@pytest.mark.parametrize("check", CHECKS)
def test_no_real_data_in_the_repository(check: str) -> None:
    pattern, allowed = CHECKS[check]
    leaks: list[str] = []
    for path in repo_files():
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            leaks.extend(
                f"{path.relative_to(ROOT)}:{number}: {match.group(0)}"
                for match in pattern.finditer(line)
                if not allowed(match)
            )
    assert not leaks, f"{check}:\n" + "\n".join(leaks[:20])
