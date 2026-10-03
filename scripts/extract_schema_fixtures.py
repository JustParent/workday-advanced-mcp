"""Extract unmodified schema declarations reachable from supported requests.

Usage: python scripts/extract_schema_fixtures.py /path/to/downloaded/v44.2/xsds
Downloads are deliberately separate from CI. Sources are the official service
XSDs at https://community.workday.com/sites/default/files/file-hosting/productionapi/.
Annotations are removed; types, order, attributes and constraints are retained.
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


def extract(source_dir: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for service, roots in ROOTS.items():
        source = (source_dir / f"{service}.xsd").read_bytes()
        tree = etree.fromstring(source)
        definitions = {e.get("name"): e for e in tree if e.get("name")}
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
            if element.get("name") in needed:
                child = copy.deepcopy(element)
                for annotation in child.findall(
                    ".//{http://www.w3.org/2001/XMLSchema}annotation"
                ):
                    annotation.getparent().remove(annotation)
                output.append(child)
        etree.XMLSchema(output)  # Validate the extraction itself.
        (destination / f"{service}.xsd").write_bytes(
            etree.tostring(
                output, pretty_print=True, xml_declaration=True, encoding="utf-8"
            )
        )
        manifest[service] = {
            "source": f"https://community.workday.com/sites/default/files/file-hosting/productionapi/{service}/v44.2/{service}.xsd",
            "sha256": hashlib.sha256(source).hexdigest(),
            "request_elements": roots,
        }
    (destination / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    extract(
        Path(sys.argv[1]),
        Path(__file__).resolve().parents[1] / "tests/fixtures/schemas/v44.2",
    )
