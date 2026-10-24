"""Shared fixtures.

Every test runs against an explicitly configured tenant so nothing depends on the
developer's environment, and the process-wide client is reset between tests so a
cached one never leaks configuration across them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

HOST = "wd3-impl-services1.workday.com"
TENANT = "acme_preview"
API_VERSION = "v44.2"

BASE_ENV = {
    "WORKDAY_HOST": HOST,
    "WORKDAY_TENANT": TENANT,
    "WORKDAY_AUTH_MODE": "isu",
    "WORKDAY_ISU_USERNAME": "isu_harriet",
    "WORKDAY_ISU_PASSWORD": "isu-password",
    "WORKDAY_API_VERSION": API_VERSION,
}

ALL_WORKDAY_ENV = (
    "WORKDAY_HOST",
    "WORKDAY_TENANT",
    "WORKDAY_AUTH_MODE",
    "WORKDAY_ISU_USERNAME",
    "WORKDAY_ISU_PASSWORD",
    "WORKDAY_CLIENT_ID",
    "WORKDAY_CLIENT_SECRET",
    "WORKDAY_REFRESH_TOKEN",
    "WORKDAY_API_VERSION",
    "WORKDAY_READ_ONLY",
    "WORKDAY_ENABLED_TOOLS",
    "WORKDAY_ALLOW_AUTO_COMPLETE",
    "WORKDAY_PROXY_URL",
    "WORKDAY_HTTP_TIMEOUT",
)


def service_url(service: str) -> str:
    return f"https://{HOST}/ccx/service/{TENANT}/{service}/{API_VERSION}"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start each test from the base tenant configuration and a fresh client."""
    from workday_advanced_mcp.client import reset_client

    for name in ALL_WORKDAY_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    reset_client()
    yield
    reset_client()


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Override environment variables and drop the cached client."""
    from workday_advanced_mcp.client import reset_client

    def _set(**values: str | None) -> None:
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        reset_client()

    return _set


@pytest.fixture
def server_factory() -> Callable[..., FastMCP]:
    """Build a server the way the entry point does."""
    from workday_advanced_mcp.server import build_server

    def _factory(**kwargs: Any) -> FastMCP:
        return build_server(**kwargs)

    return _factory


def soap_response(operation: str, inner_xml: str) -> str:
    """Wrap response content in the envelope Workday would return."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wd="urn:com.workday/bsvc">'
        f"<env:Body><wd:{operation}_Response>{inner_xml}</wd:{operation}_Response></env:Body>"
        "</env:Envelope>"
    )


def soap_fault(faultstring: str, *validation_messages: str) -> str:
    """A Workday validation fault, which arrives as HTTP 500."""
    errors = "".join(
        f"<wd:Validation_Error><wd:Message>{message}</wd:Message></wd:Validation_Error>"
        for message in validation_messages
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wd="urn:com.workday/bsvc">'
        "<env:Body><env:Fault>"
        "<faultcode>SOAP-ENV:Client.validationError</faultcode>"
        f"<faultstring>{faultstring}</faultstring>"
        f"<detail><wd:Validation_Fault>{errors}</wd:Validation_Fault></detail>"
        "</env:Fault></env:Body></env:Envelope>"
    )


def soap_auth_fault() -> str:
    """Workday's answer to credentials it rejects: HTTP 500 and an authentication fault.

    Captured from a live implementation tenant on 2026-10-22, identical on v44.2
    and v48.0. It is not an HTTP 401, and "invalid username or password" is the
    only reason given.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        "<SOAP-ENV:Body>"
        '<SOAP-ENV:Fault xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wd="urn:com.workday/bsvc">'
        "<faultcode>SOAP-ENV:Client.authenticationError</faultcode>"
        "<faultstring>invalid username or password</faultstring>"
        "</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def soap_unknown_id_fault(value: str, id_type: str, xpath: str) -> str:
    """Workday's answer to a lookup of an id that matches nothing: a validation fault.

    Captured from a live implementation tenant on 2026-10-24 for workers,
    organizations, positions and events. Skip_Non_Existing_Instances does not turn
    it into an empty response.
    """
    message = (
        f"Invalid ID value.  '{value}' is not a valid ID value for type = '{id_type}'"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        "<SOAP-ENV:Body>"
        '<SOAP-ENV:Fault xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wd="urn:com.workday/bsvc">'
        "<faultcode>SOAP-ENV:Client.validationError</faultcode>"
        f"<faultstring>Validation error occurred. {message}</faultstring>"
        "<detail><wd:Validation_Fault><wd:Validation_Error>"
        f"<wd:Message>{message}</wd:Message>"
        f"<wd:Detail_Message>{message}</wd:Detail_Message>"
        f"<wd:Xpath>{xpath}</wd:Xpath>"
        "</wd:Validation_Error></wd:Validation_Fault></detail>"
        "</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


WORKER_XML = """
<wd:Response_Data>
  <wd:Worker>
    <wd:Worker_Reference wd:Descriptor="Jane Doe">
      <wd:ID wd:type="WID">worker-wid-1</wd:ID>
      <wd:ID wd:type="Employee_ID">21001</wd:ID>
    </wd:Worker_Reference>
    <wd:Worker_Data>
      <wd:Worker_ID>21001</wd:Worker_ID>
      <wd:Personal_Data>
        <wd:Name_Data>
          <wd:Legal_Name_Data>
            <wd:Name_Detail_Data wd:Formatted_Name="Jane Doe">
              <wd:First_Name>Jane</wd:First_Name>
              <wd:Last_Name>Doe</wd:Last_Name>
            </wd:Name_Detail_Data>
          </wd:Legal_Name_Data>
        </wd:Name_Data>
        <wd:Contact_Data>
          <wd:Email_Address_Data>
            <wd:Email_Address>jane.doe@example.com</wd:Email_Address>
          </wd:Email_Address_Data>
        </wd:Contact_Data>
      </wd:Personal_Data>
      <wd:Employment_Data>
        <wd:Worker_Job_Data wd:Primary_Job="true">
          <wd:Position_Data>
            <wd:Position_ID>POS-100</wd:Position_ID>
            <wd:Position_Title>Engineer</wd:Position_Title>
            <wd:Business_Title>Senior Engineer</wd:Business_Title>
            <wd:Worker_Type_Reference wd:Descriptor="Regular">
              <wd:ID wd:type="WID">worker-type-wid</wd:ID>
              <wd:ID wd:type="Employee_Type_ID">Regular</wd:ID>
            </wd:Worker_Type_Reference>
            <wd:Job_Profile_Summary_Data>
              <wd:Job_Profile_Reference wd:Descriptor="Engineer">
                <wd:ID wd:type="WID">jp-wid</wd:ID>
                <wd:ID wd:type="Job_Profile_ID">JP-ENG</wd:ID>
              </wd:Job_Profile_Reference>
            </wd:Job_Profile_Summary_Data>
            <wd:Business_Site_Summary_Data>
              <wd:Location_Reference wd:Descriptor="London">
                <wd:ID wd:type="Location_ID">LOC-LON</wd:ID>
              </wd:Location_Reference>
            </wd:Business_Site_Summary_Data>
          </wd:Position_Data>
          <wd:Position_Organizations_Data>
            <wd:Position_Organization_Data>
              <wd:Organization_Reference wd:Descriptor="Engineering">
                <wd:ID wd:type="Organization_Reference_ID">SUP-ENG</wd:ID>
              </wd:Organization_Reference>
              <wd:Organization_Data>
                <wd:Organization_Type_Reference wd:Descriptor="Supervisory">
                  <wd:ID wd:type="Organization_Type_ID">Supervisory</wd:ID>
                </wd:Organization_Type_Reference>
                <wd:Organization_Name>Engineering</wd:Organization_Name>
                <wd:Organization_Reference_ID>SUP-ENG</wd:Organization_Reference_ID>
              </wd:Organization_Data>
            </wd:Position_Organization_Data>
            <wd:Position_Organization_Data>
              <wd:Organization_Reference wd:Descriptor="R&amp;D">
                <wd:ID wd:type="Cost_Center_Reference_ID">CC_100</wd:ID>
              </wd:Organization_Reference>
              <wd:Organization_Data>
                <wd:Organization_Type_Reference wd:Descriptor="Cost Center">
                  <wd:ID wd:type="Organization_Type_ID">Cost_Center</wd:ID>
                </wd:Organization_Type_Reference>
                <wd:Organization_Name>R&amp;D</wd:Organization_Name>
                <wd:Organization_Reference_ID>CC_100</wd:Organization_Reference_ID>
              </wd:Organization_Data>
            </wd:Position_Organization_Data>
            <wd:Position_Organization_Data>
              <wd:Organization_Reference wd:Descriptor="Acme Ltd">
                <wd:ID wd:type="Company_Reference_ID">CO_1</wd:ID>
              </wd:Organization_Reference>
              <wd:Organization_Data>
                <wd:Organization_Type_Reference wd:Descriptor="Company">
                  <wd:ID wd:type="Organization_Type_ID">Company</wd:ID>
                </wd:Organization_Type_Reference>
                <wd:Organization_Name>Acme Ltd</wd:Organization_Name>
                <wd:Organization_Reference_ID>CO_1</wd:Organization_Reference_ID>
              </wd:Organization_Data>
            </wd:Position_Organization_Data>
          </wd:Position_Organizations_Data>
        </wd:Worker_Job_Data>
        <wd:Worker_Status_Data>
          <wd:Active>true</wd:Active>
          <wd:Hire_Date>2020-01-06</wd:Hire_Date>
          <wd:Terminated>false</wd:Terminated>
        </wd:Worker_Status_Data>
      </wd:Employment_Data>
    </wd:Worker_Data>
  </wd:Worker>
</wd:Response_Data>
"""


@pytest.fixture
def worker_response() -> str:
    return soap_response("Get_Workers", WORKER_XML)


async def call_tool(mcp: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool the way a client does and decode what comes back.

    Tools return JSON for success and a plain ``Error: ...`` string for failure,
    so a decode failure is itself the error message.
    """
    import json

    from mcp.types import TextContent

    result = await mcp.call_tool(name, arguments)
    if isinstance(result, dict):
        return result
    text = "".join(block.text for block in result if isinstance(block, TextContent))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
