"""Hand-crafted Workday SOAP envelopes.

Deliberately no SOAP library. Workday publishes a multi-megabyte WSDL per web
service; zeep or suds would download and parse one on startup, which both blows
the host's 120-second MCP ``initialize`` deadline and repeats on every cold
sandbox. Instead each operation owns a small builder that emits an ordered
mapping, and this module turns that mapping into XML with the standard library.

Element order is therefore the builder's responsibility, not a schema's: Workday
rejects a request whose children are out of document order, so every builder
states its order explicitly and ``tests/test_envelopes.py`` locks it in.

The mapping dialect matches the one Harriet already uses in
``justparent/bots/utils/soap.py`` so envelopes are interchangeable between the
two codebases:

* ``{'Key': value}``          -> ``<bsvc:Key>value</bsvc:Key>``
* ``{'@Attr': value}``        -> attribute ``Attr`` on the parent element
* ``{'#text': value}``        -> text content of the parent element
* ``{'Key': [a, b]}``         -> ``<bsvc:Key>`` repeated per item
* ``{'ID': [{'type': t, 'value': v}]}`` -> ``<bsvc:ID bsvc:type="t">v</bsvc:ID>``
  (``_value_1`` is accepted as a synonym for ``value``)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
BSVC_NS = "urn:com.workday/bsvc"
WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
PASSWORD_TEXT_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordText"
)

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def build_username_token(username: str, password: str) -> dict[str, Any]:
    """The WS-Security header an Integration System User authenticates with.

    Workday accepts a plain ``PasswordText`` UsernameToken over TLS; no nonce or
    timestamp is required. The username must be qualified as ``user@tenant``.
    """
    return {
        "wsse:Security": {
            "@soap:mustUnderstand": "1",
            "wsse:UsernameToken": {
                "wsse:Username": username,
                "wsse:Password": {
                    "@Type": PASSWORD_TEXT_TYPE,
                    "#text": password,
                },
            },
        }
    }


def workday_common_header() -> dict[str, Any]:
    """Ask Workday to include descriptors, so references carry readable names."""
    return {
        "bsvc:Workday_Common_Header": {
            "bsvc:Include_Reference_Descriptors_In_Response": "true"
        }
    }


def add_bsvc_prefixes(data: Any) -> Any:
    """Prefix unprefixed element names with ``bsvc:`` and normalise ID lists."""
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            if key.startswith(("@", "#")) or ":" in key:
                result[key] = add_bsvc_prefixes(value)
            elif key == "ID" and isinstance(value, list):
                result[f"bsvc:{key}"] = _convert_id_list(value)
            else:
                result[f"bsvc:{key}"] = add_bsvc_prefixes(value)
        return result
    if isinstance(data, list):
        return [add_bsvc_prefixes(item) for item in data]
    if isinstance(data, bool):
        return "true" if data else "false"
    return data


def _convert_id_list(id_list: list[dict[str, Any]]) -> Any:
    """``[{'type': 'WID', 'value': 'x'}]`` -> ``<bsvc:ID "
    "bsvc:type="WID">x</bsvc:ID>``."""
    converted = [
        {
            "@bsvc:type": item.get("type", ""),
            "#text": item.get("value", item.get("_value_1", "")),
        }
        for item in id_list
    ]
    return converted[0] if len(converted) == 1 else converted


def _dict_to_xml(data: Any, parent: ET.Element, parent_key: str | None = None) -> None:
    """Recursively render a mapping onto an element, preserving insertion order."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key.startswith("@"):
                parent.set(key[1:], _text(value))
            elif key == "#text":
                parent.text = _text(value)
            elif isinstance(value, list):
                for item in value:
                    _dict_to_xml(item, ET.SubElement(parent, key), key)
            else:
                _dict_to_xml(value, ET.SubElement(parent, key), key)
    elif isinstance(data, list):
        if parent_key:
            for item in data:
                _dict_to_xml(item, ET.SubElement(parent, parent_key), parent_key)
    elif data is not None:
        parent.text = _text(data)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_envelope(
    operation: str,
    request_data: dict[str, Any],
    *,
    security: dict[str, Any] | None = None,
    include_common_header: bool = True,
) -> str:
    """Render ``<operation>_Request`` as a complete SOAP envelope."""
    envelope = ET.Element("soap:Envelope")
    envelope.set("xmlns:soap", SOAP_NS)
    envelope.set("xmlns:bsvc", BSVC_NS)
    if security:
        envelope.set("xmlns:wsse", WSSE_NS)

    headers: dict[str, Any] = {}
    if security:
        headers.update(security)
    if include_common_header:
        headers.update(workday_common_header())
    if headers:
        _dict_to_xml(headers, ET.SubElement(envelope, "soap:Header"))

    body = ET.SubElement(envelope, "soap:Body")
    _dict_to_xml({f"bsvc:{operation}_Request": add_bsvc_prefixes(request_data)}, body)

    return f"{XML_DECLARATION}\n{ET.tostring(envelope, encoding='unicode')}"


def local_name(tag: str) -> str:
    """``{ns}Tag`` or ``ns:Tag`` -> ``Tag``."""
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    return tag.rsplit(":", 1)[-1]


def _xml_to_dict(element: ET.Element) -> Any:
    result: dict[str, Any] = {}

    for key, value in element.attrib.items():
        result[f"@{local_name(key)}"] = value

    children: dict[str, Any] = {}
    for child in element:
        tag = local_name(child.tag)
        value = _xml_to_dict(child)
        if tag in children:
            if not isinstance(children[tag], list):
                children[tag] = [children[tag]]
            children[tag].append(value)
        else:
            children[tag] = value
    result.update(children)

    if element.text and element.text.strip():
        if result:
            result["#text"] = element.text.strip()
        else:
            return element.text.strip()

    return result or ""


@dataclass
class SoapFault:
    """A parsed SOAP fault."""

    code: str = ""
    string: str = ""
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class ParsedEnvelope:
    """The body of a SOAP response, or the fault it carried instead."""

    body: dict[str, Any] = field(default_factory=dict)
    fault: SoapFault | None = None


# Elements inside a fault detail that carry a human-readable reason.
_FAULT_MESSAGE_TAGS = frozenset(
    {"Message", "Detail_Message", "Error", "Validation_Message"}
)


def parse_envelope(xml_text: str) -> ParsedEnvelope:
    """Parse a Workday SOAP response into its body, or the fault it returned.

    Parsing failures surface as a fault rather than an exception so a truncated
    or non-XML response still produces an actionable message.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return ParsedEnvelope(
            fault=SoapFault(
                code="ParseError", string=f"Malformed XML from Workday: {exc}"
            )
        )

    body = None
    for element in root.iter():
        if local_name(element.tag) == "Body":
            body = element
            break
    if body is None:
        return ParsedEnvelope(
            fault=SoapFault(
                code="ParseError", string="No SOAP Body in the Workday response."
            )
        )

    for child in body:
        if local_name(child.tag) == "Fault":
            return ParsedEnvelope(fault=_parse_fault(child))

    return ParsedEnvelope(
        body={local_name(child.tag): _xml_to_dict(child) for child in body}
    )


def _parse_fault(fault_element: ET.Element) -> SoapFault:
    fault = SoapFault()
    for child in fault_element:
        tag = local_name(child.tag)
        if tag == "faultcode":
            fault.code = (child.text or "").strip()
        elif tag == "faultstring":
            fault.string = (child.text or "").strip()

    for element in fault_element.iter():
        if local_name(element.tag) in _FAULT_MESSAGE_TAGS:
            text = (element.text or "").strip()
            if text and text not in fault.validation_errors:
                fault.validation_errors.append(text)

    return fault
