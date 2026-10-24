"""Extract unmodified schema declarations reachable from supported requests.

Usage: python scripts/extract_schema_fixtures.py <version> /path/to/downloaded/files

Each service needs either ``<Service>.xsd`` or ``<Service>.wsdl`` in that directory.
Prefer the official service XSDs at
https://community.workday.com/sites/default/files/file-hosting/productionapi/.
A release not published there yet (a preview tenant runs it early) can be taken
from the service's WSDL under Public Web Services, which embeds the same schema.
Downloads are deliberately separate from CI. Annotations are removed; types,
order, attributes and constraints are retained.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

from lxml import etree

ROOTS = {
    "Human_Resources": [
        "Get_Workers_Request",
        "Get_Organizations_Request",
        "Get_Job_Profiles_Request",
        "Get_Locations_Request",
        "Organization_Add_Update",
        "Organization_Inactivate",
        "Worker_Event_History_Get",
        "Maintain_Contact_Information_for_Person_Event_Request",
    ],
    "Staffing": [
        "Change_Job_Request",
        "Change_Organization_Assignments_Request",
        "Hire_Employee_Request",
        "Terminate_Employee_Request",
        "Assign_Roles_Request",
        "Get_Positions_Request",
        "Create_Position_Request",
    ],
    "Recruiting": ["Get_Applicants_Request", "Put_Applicant_Request"],
    "Cash_Management": [
        "Get_Payment_Election_Enrollments_Request",
        "Submit_Payment_Election_Enrollment_Request",
    ],
    "Integrations": [
        "Get_Event_Details_Request",
        "Get_References_Request",
        "Cancel_Business_Process_Request",
        "Rescind_Business_Process_Request",
    ],
    "Financial_Management": [
        "Get_Journals_Request",
        "Submit_Accounting_Journal_Request",
    ],
}

PUBLIC_SOURCE = (
    "https://community.workday.com/sites/default/files/file-hosting/productionapi/"
    "{service}/{version}/{service}.xsd"
)
WSDL_SOURCE = (
    "Workday Web Services {version} {service} WSDL (Public Web Services), "
    "embedded schema; sha256 is of the canonical schema, not the whole WSDL"
)
WSDL_NS = "{http://schemas.xmlsoap.org/wsdl/}"

# The inputs are downloads, so never resolve entities or touch the network.
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


def load_schema(source_dir: Path, service: str) -> tuple[etree._Element, bytes, bool]:
    """A service's schema element, its source bytes and whether they were a WSDL."""
    xsd = source_dir / f"{service}.xsd"
    if xsd.exists():
        source = xsd.read_bytes()
        return etree.fromstring(source, PARSER), source, False
    source = (source_dir / f"{service}.wsdl").read_bytes()
    types = etree.fromstring(source, PARSER).find(f"{WSDL_NS}types")
    schemas = list(types) if types is not None else []
    if len(schemas) != 1:
        raise SystemExit(f"{service}.wsdl embeds {len(schemas)} schemas, expected one")
    return schemas[0], source, True


def extract(source_dir: Path, destination: Path, version: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for service, roots in ROOTS.items():
        tree, source, from_wsdl = load_schema(source_dir, service)
        definitions = {
            e.get("name"): e for e in tree if isinstance(e.tag, str) and e.get("name")
        }
        needed = set(roots)
        pending = list(roots)
        while pending:
            name = pending.pop()
            for element in definitions[name].iter():
                for attribute in ("type", "base", "ref"):
                    value = element.get(attribute, "")
                    if value.startswith("wd:") and value[3:] not in needed:
                        needed.add(value[3:])
                        pending.append(value[3:])
        output = etree.Element(tree.tag, attrib=dict(tree.attrib), nsmap=tree.nsmap)
        for element in tree:
            if isinstance(element.tag, str) and element.get("name") in needed:
                child = copy.deepcopy(element)
                for annotation in child.findall(
                    ".//{http://www.w3.org/2001/XMLSchema}annotation"
                ):
                    annotation.getparent().remove(annotation)
                output.append(child)
        # A WSDL root declares its binding namespaces too; keep only what is used.
        etree.cleanup_namespaces(output, keep_ns_prefixes=["wd", "xsd"])
        etree.XMLSchema(output)  # Validate the extraction itself.
        if from_wsdl:
            # The rest of a tenant's WSDL (its service address) is tenant-specific, so
            # fingerprint the schema alone: it is the same from any tenant.
            source = etree.tostring(tree, method="c14n")
        (destination / f"{service}.xsd").write_bytes(
            etree.tostring(
                output, pretty_print=True, xml_declaration=True, encoding="utf-8"
            )
        )
        manifest[service] = {
            "source": (WSDL_SOURCE if from_wsdl else PUBLIC_SOURCE).format(
                service=service, version=version
            ),
            "sha256": hashlib.sha256(source).hexdigest(),
            "request_elements": roots,
        }
    (destination / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    extract(
        Path(sys.argv[2]),
        Path(__file__).resolve().parents[1] / "tests/fixtures/schemas" / sys.argv[1],
        sys.argv[1],
    )
