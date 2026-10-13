# Workday schema research and tenant validation

## Official sources

The implementation was reviewed against the official [production API directory](https://community.workday.com/sites/default/files/file-hosting/productionapi/index.html?lang=en-us)
and these **v44.2** schemas/WSDLs. The default stays v44.2 for compatibility.

| Service | Official schema | Operations |
| --- | --- | --- |
| Human Resources | [XSD](https://community.workday.com/sites/default/files/file-hosting/productionapi/Human_Resources/v44.2/Human_Resources.xsd) | Workers, organizations, profiles, locations, org maintenance/inactivation, home contact change, worker history. |
| Staffing | [XSD](https://community.workday.com/sites/default/files/file-hosting/productionapi/Staffing/v44.2/Staffing.xsd) | Hire, termination, job/org assignments, positions, roles. |
| Recruiting | [XSD](https://community.workday.com/sites/default/files/file-hosting/productionapi/Recruiting/v44.2/Recruiting.xsd) | Get/put applicants. |
| Cash Management | [XSD](https://community.workday.com/sites/default/files/file-hosting/productionapi/Cash_Management/v44.2/Cash_Management.xsd) | Get/submit payment elections. |
| Integrations | [XSD](https://community.workday.com/sites/default/files/file-hosting/productionapi/Integrations/v44.2/Integrations.xsd) | References, event detail, cancel/rescind BP. |
| Financial Management | [XSD](https://community.workday.com/sites/default/files/file-hosting/productionapi/Financial_Management/v44.2/Financial_Management.xsd) | Submit/get accounting journals. |

Operation pages use the same directory, e.g.
[Add_Update_Organization](https://community.workday.com/sites/default/files/file-hosting/productionapi/Human_Resources/v44.2/Add_Update_Organization.html),
[Assign_Roles](https://community.workday.com/sites/default/files/file-hosting/productionapi/Staffing/v44.2/Assign_Roles.html)
and [Submit_Payment_Election_Enrollment](https://community.workday.com/sites/default/files/file-hosting/productionapi/Cash_Management/v44.2/Submit_Payment_Election_Enrollment.html).
Use the official docs together with tenant configuration: XSDs cannot establish
ISU visibility or expense approval routing.

## Schema corrections

- `Add_Update_Organization` uses `Organization_Add_Update` and returns
  `Organization_Reference`. Type/subtype/visibility use nested names. It bypasses
  normal organization BP approval policies.
- `Inactivate_Organization` uses `Organization_Inactivate`; its WSDL defines an
  empty output, so a successful empty SOAP body is valid here.
- The operation is `Get_Event_Detail`; request/response roots are plural
  `Get_Event_Details_Request` / `Get_Event_Details_Response`. Awaiting tasks use
  `Task_Reference` and carry awaiting-person references.
- Worker history uses legacy `Worker_Event_History_Get` / `Worker_Event_History`
  roots and legacy employee integration IDs.
- Hire `Employee_ID` belongs inside `Hire_Employee_Event_Data`. Position details
  have a defined child order. Former worker/pre-hire/new applicant and
  position/job requisition are exclusive choices.
- Bank fields follow schema order. Financial BP parameters do not permit
  `Run_Now`. Election inputs cannot reuse a bank account solely by reference.
- Get Positions uses `Positions_Reference`; Get Journals uses
  `JournalEntryReference`. Pre-hire phone precedes email in contact data.
- Omitted entry moments default to the current timestamp. Paging must pin that
  moment; approval checks must ignore its changing response echo.

`Get_Applicants` belongs to Recruiting in this version and remains there.

## Offline fixtures

`tests/fixtures/schemas/v44.2/` contains the transitive declaration closure for
tested request roots. Only annotations and unused declarations are removed;
sequence, choice, cardinality, attributes, restrictions and referenced types
remain. `sources.json` records official URLs, full-source SHA-256 hashes and roots.

To regenerate, download each full `<Service>.xsd` above into one directory:

```bash
python scripts/extract_schema_fixtures.py /path/to/downloaded/schemas
pytest tests/test_schema_contracts.py tests/test_pagination.py -q
```

Review hash/provenance changes on refresh. CI never downloads docs; runtime
startup does not load the fixtures. Synthetic response fixtures exercise parser
variants and are not claimed as live tenant captures.

## Tenant checks

The automated suite uses mocked tenant responses. Validate operations in an
implementation tenant before enabling production writes.

| Area | Verify with the tenant administrator |
| --- | --- |
| Identity/access | Dedicated ISU/OAuth identity, intended population visibility, enabled operations' read/write/initiate/view permissions and activated policies. Restricted access must fail clearly. |
| Org maintenance | Manage: Organization Update Integration, subtype/visibility names, naming/ID convention, location defaults, effective/availability dates, staffing model, approval before immediate writes. |
| Hierarchy/roles | Membership vs managed orgs, multi-org managers, role IDs/types, position assignees, future assignments and actual expense routing. |
| Staffing | Change/termination reasons, position restrictions, employee types, rehire identity and required BP sub-processes. |
| Home contact | Read/maintain permissions, country/region/line mappings, private HOME usage and preservation of other contact data. |
| Payments | Expenses/payroll rules, currencies/types, account formats, full-field visibility for merging and BP routing. |
| Recovery | Visible tasks/awaiting people, cancel/rescind eligibility/dependencies and compensating roles. |
| Finance, if enabled | Journal source, ledger, account WIDs, worktags, period rules, stable IDs, approval/posting. |
| Deployment | Host-side duplicate suppression and approval records, allowlist/read-only mode, secrets, outbound IP and host timeout. |

## Acceptance scenarios

1. Promote a worker to manager: create the org under the correct parent, assign
   manager/expense roles, move reports and verify real expense approval routing.
2. Repeat the source event while the BP awaits approval, including after the
   sandbox is recycled; confirm the host sends no second submission. Preview in
   one sandbox and submit the approval in another.
3. Reparent a managed org, reject cycles and preserve the intended membership;
   include future-effective changes.
4. Create/approve a position before hire; rehire without a duplicate person.
5. Transfer a departing manager's reports/roles, terminate and inactivate the
   empty org; review future assignments before removing roles.
6. Patch HOME while preserving extra/local fields and other contact data;
   verify private usage.
7. Change accounts sharing last-four digits, routing-only changes and
   rule/distribution/currency-only changes. Preserve unrelated rules; refuse
   masked/incomplete unsafe merges and confirm payroll elections survive.
8. Change business data after approval and confirm stale-token rejection. Repeat
   with only a later query timestamp and confirm the unchanged plan proceeds.
9. Lose a write response, recycle the sandbox and reconcile through event
   history. Exercise eligible and ineligible cancel/rescind cases without blind
   retries.
10. Fail batch item two; retain item one's event and mark later items unattempted.
    Resume after reconciliation and fresh approval.
11. If finance is enabled, submit a balanced journal once and reconcile its
    stable ID and eventual approval/posting status.

Record sanitized request/response samples and configuration assumptions. Keep
real names, bank details, credentials and production records out of tests.
