# workday-advanced-mcp

A stdio MCP server for Workday staffing, supervisory hierarchies, organization
roles, home addresses, payment elections and accounting journals. It complements
the standard HRIS connector with operations from Workday Web Services (SOAP).

There are 30 tools: 15 reads, 14 writes and one preview. Read-only deployments
register only the 15 reads. See [tenant validation](docs/tenant-validation.md)
for schema provenance, deployment checks and acceptance scenarios.

## Workday setup

Use a dedicated integration identity and validate it in an implementation tenant
first. Scope the security group to the intended population where the tenant
permits it.

1. **Create Integration System User**: no new password at next sign-in, session
   timeout `0`, **Do Not Allow UI Sessions** ticked.
2. **Maintain Password Rules**: add the ISU to *System Users exempt from password
   expiration*.
3. **Create Security Group**: *Integration System Security Group (Unconstrained)*,
   or *(Constrained)* with an organization scope. Add the ISU.
4. **Maintain Domain Permissions for Security Group**, under Integration
   Permissions (names vary slightly by tenant):

   | Access | Domains | Tools |
   | --- | --- | --- |
   | Get | Worker Data: Public Worker Reports, Workers, Current Staffing Information, Employment Data, Organization Information, All Positions; Person Data: Name, Work Email | worker, member and position reads |
   | Get | Manage: Organization Integration, Manage: Location, Job Information | organizations, locations, job profiles |
   | Get and Put | Manage: Organization Update Integration | create, update and inactivate supervisory organizations |
   | Get and Put | Manage: Pre-Hire Process, Pre-Hire Process Data: Name and Contact Information | pre-hire reads and creation |
   | Get and Put | Person Data: Home Address (or Home Contact Information) | home address read and update |
   | Get and Put | Worker Data: Payroll Interface (Payment Elections) or Worker Data: Payroll (Payment Elections); Manage: Payment Election | payment elections, if enabled |
   | Get and Put | Journal entry domains | journals, if enabled |

5. **Edit Business Process Security Policy** for Hire, Change Job, Change
   Organization Assignments for Worker, Termination, Assign Roles, Create
   Position, Contact Change and, if enabled, Payment Election Enrollment Event
   and Journal Entry. Add the group to the initiating action ending
   *(Web Service)* and to **View All**, plus **Cancel** and **Rescind** for
   `workday_recover_event`.
6. If the tenant uses authentication policies, allow the group *User Name
   Password* sign-in, limited to the deployment's outbound IP where possible.
7. **Activate Pending Security Policy Changes**.

Business-process steps assigned to the initiator wait in the ISU's inbox, where
nobody can complete them. Skip or reroute those steps for this group. Confirm a
tenant's requirements for an operation with **View Security for Securable Item**.
The host is the `-services` hostname in the address of any WSDL under **Public
Web Services**.

Organization maintenance needs particular care: `Add_Update_Organization` uses
**Manage: Organization Update Integration** and bypasses normal organization
business-process approval policies. Org creation, reparenting and inactivation
are immediate maintenance operations; application approval must precede them.
Other tools normally follow tenant business-process routing. Roles and journals
need permissions beyond the original worker/payment integration. The
[validation checklist](docs/tenant-validation.md) maps the operations to check.

ISU credentials and OAuth refresh tokens are supported. Repeated failed ISU
sign-ins can lock an account; the server does not retry authentication failures.
For IP-restricted tenants, allow the deployment's outbound address or use a
fixed-egress proxy.

## Configuration

| Environment variable | Required | Description |
| --- | --- | --- |
| `WORKDAY_HOST` | yes | API host, e.g. `wd3-impl-services1.workday.com`. Tenant URLs are accepted; only the hostname is used. |
| `WORKDAY_TENANT` | yes | Tenant ID; inferred from a pasted URL when omitted. |
| `WORKDAY_AUTH_MODE` | no | `isu` (default) or `oauth`. A complete OAuth trio with no ISU password selects OAuth. |
| `WORKDAY_ISU_USERNAME` | ISU | ISU name; `@tenant` is appended if missing. |
| `WORKDAY_ISU_PASSWORD` | ISU | ISU password. |
| `WORKDAY_CLIENT_ID` | OAuth | API client for integrations. |
| `WORKDAY_CLIENT_SECRET` | OAuth | Client secret. |
| `WORKDAY_REFRESH_TOKEN` | OAuth | Exchanged for a bearer token with the `soap_api` scope. |
| `WORKDAY_API_VERSION` | no | Defaults to `v44.2`, validated by the committed schema tests. |
| `WORKDAY_READ_ONLY` | no | `true` omits all writes and preview. |
| `WORKDAY_ENABLED_TOOLS` | no | Comma-separated exact tool allowlist. Include `workday_preview_write` and the reads used to reconcile writes. |
| `WORKDAY_ALLOW_AUTO_COMPLETE` | no | `true` permits a caller to request `auto_complete`, bypassing normal BP routing. Off by default. |
| `WORKDAY_PROXY_URL` | no | `socks5h://...` or `http://...`. SOCKS needs the `socks` extra. |
| `WORKDAY_HTTP_TIMEOUT` | no | Seconds per HTTP call; default `90`. |

### State

The server stores nothing, so it runs in short-lived sandboxes without a volume.
The host keeps approval records, suppresses duplicate submissions and reconciles
unknown outcomes. A preview's `state_token` is keyed from the configured
credential: any process with the same configuration accepts it, and rotating the
ISU password or client secret invalidates outstanding previews.

## Running

Pin deployments to a reviewed commit:

```bash
uvx --from 'git+https://github.com/JustParent/workday-advanced-mcp@<GIT_SHA>' workday-advanced-mcp
```

Check a source installation without calling Workday:

```bash
uvx --from . workday-advanced-mcp --test
```

`--test` prints the version, endpoint, authentication mode, whether credentials
are present, read-only state and registered tools, never credential values.
Startup does not download or parse a WSDL.

For a host using sandboxed MCP subprocess configuration:

```json
{
  "server_type": "sandboxed",
  "sandbox_command": "uvx",
  "sandbox_args": [
    "--from",
    "git+https://github.com/JustParent/workday-advanced-mcp@<GIT_SHA>",
    "workday-advanced-mcp"
  ],
  "sandbox_runtime": "python",
  "sandbox_timeout": "240",
  "sandbox_static_egress_ip": true,
  "auth_type": "none",
  "sandbox_env": {
    "WORKDAY_HOST": "<host>",
    "WORKDAY_TENANT": "<tenant>",
    "WORKDAY_ISU_USERNAME": "<isu>",
    "WORKDAY_ISU_PASSWORD": "$SECRET_KEY"
  }
}
```

A single tool can make several Workday calls of up to `WORKDAY_HTTP_TIMEOUT`
seconds each, and a host that kills a call mid-write leaves its outcome unknown.
Give the host the longest per-call timeout its outer deadlines allow: in Harriet,
240 keeps the bridge's extra 30 seconds inside the 300-second job deadline. If the host supports `$SECRET_KEY`
substitution, put the ISU password in the secret field rather than directly in
configuration. With a static egress IP, allow that address in the tenant's
authentication policy.

## Read tools

| Tool | Workday operation | Purpose |
| --- | --- | --- |
| `workday_find_worker` | `Get_Workers` | Resolve employee ID, WID or work email; bounded scans report completeness. |
| `workday_get_worker` | `Get_Workers` | Status, primary job, memberships, management chain and addresses. |
| `workday_list_organizations` | `Get_Organizations` | Organizations with effective-date/inactive filtering. |
| `workday_get_organization` | `Get_Organizations` | Exact org, superior, children, staffing model and roles. |
| `workday_get_manager_organizations` | `Get_Organizations` / `Get_Workers` | Managed orgs, separately from membership. |
| `workday_list_organization_workers` | `Get_Workers` | Direct members or include subordinate orgs. |
| `workday_list_positions` | `Get_Positions` | Exact position or open positions for an org. |
| `workday_list_reference_ids` | `Get_References` | Tenant-defined IDs such as reason codes. |
| `workday_list_job_profiles` | `Get_Job_Profiles` | Job profile references. |
| `workday_list_locations` | `Get_Locations` | Location references. |
| `workday_list_pre_hires` | `Get_Applicants` | Existing applicants/pre-hires. |
| `workday_get_payment_elections` | `Get_Payment_Election_Enrollments` | Masked accounts, rules and distributions. |
| `workday_get_event` | `Get_Event_Detail` | State, awaiting tasks/people, dates, history and sub-events. |
| `workday_get_worker_event_history` | `Get_Worker_Event_History` | Discover visible worker events, e.g. to reconcile an unknown write. |
| `workday_get_journal` | `Get_Journals` | Reconcile a journal by stable ID or WID. |

Internal paged scans use one entry timestamp. The manually paged member and
position tools return `as_of_entry_datetime`; pass it back on subsequent pages.
An incomplete or security-limited result cannot establish absence.

## Write tools

| Tool | Workday operation | Behavior |
| --- | --- | --- |
| `workday_create_supervisory_organization` | `Add_Update_Organization` | Stable ID, explicit parent, subtype and staffing model; never overwrites a visible existing org. |
| `workday_update_supervisory_organization` | `Add_Update_Organization` | Reparent/rename an active org; checks ancestor cycles. |
| `workday_inactivate_supervisory_organization` | `Inactivate_Organization` | Confirmed; requires no visible active members or children. |
| `workday_assign_organization_role` | `Assign_Roles` | Add/remove named assignees without remove-all. |
| `workday_create_position` | `Create_Position` | Stable position in a position-management org. |
| `workday_create_pre_hire` | `Put_Applicant` | Duplicate-email guard unless explicitly overridden. |
| `workday_hire_employee` | `Hire_Employee` | Hire or explicit former-worker rehire; supports selected positions. |
| `workday_change_job` | `Change_Job` | Supervisory moves; up to 25 workers per batch. |
| `workday_change_organization_assignments` | `Change_Organization_Assignments` | Cost centre/company/etc.; merges assignments by default. |
| `workday_update_home_address` | `Maintain_Contact_Information_for_Person_Event` | Patch selected HOME address; preserve other contact data and keep HOME private. |
| `workday_terminate_employee` | `Terminate_Employee` | Confirmation and visible employment status required. |
| `workday_set_payment_elections` | `Submit_Payment_Election_Enrollment` | Complete-set preview and confirmation before replacing elections. |
| `workday_recover_event` | `Cancel_Business_Process` / `Rescind_Business_Process` | Confirmed recovery; Workday determines eligibility. |
| `workday_submit_accounting_journal` | `Submit_Accounting_Journal` | Balanced, add-only, single-company/single-currency journal. |

`workday_preview_write(operation, arguments)` previews **any enabled write** using
the same validation and request builder. It returns `would_submit`, a redacted
`result` and a `state_token` without a Workday mutation. Disabled writes cannot be
reached through preview. No-change previews report `would_submit: false`.

### Submission and approval contract

Every write accepts an optional `expected_state_token`. After approval, submit the
same business arguments with the preview token. Changed arguments, business data
or credentials reject submission. This is an optimistic check before sending,
not a Workday transaction lock or an approver identity record; the caller owns approval.

The server keeps no record of submissions, so it sends every call it receives.
The caller must suppress repeats of the same source event, including while a
business process awaits approval. Organization creation, hire with an employee
ID and pre-hire creation also refuse when Workday already shows a match.

Treat `submitted` and `verification.verified` separately. Acceptance does not
mean BP completion. `verified: true` requires matching read-back where the tool
implements it; otherwise inspect the event and final data. Read-back failure
preserves the accepted write's event reference.

Timeouts, cancellation, malformed responses and processing faults can leave the
outcome unknown, reported as `outcome_unknown: true`. Reconcile with
`workday_get_event` or `workday_get_worker_event_history` before sending again.
Writes are never retried automatically. Batches retain earlier successes and mark
later entries `not_attempted` after a failed or unresolved entry.

Payment no-change checks compare full keyed account identities, routing, rules,
currencies and distributions, not masked suffixes. Optional `merge_with_current:
true` preserves readable untouched rule groups; masked accounts cannot be
reconstructed and make such a merge fail. Retaining unused bank-account objects
does **not** preserve omitted elections.

## Schema validation and development

The default remains **v44.2** for compatibility. Offline official-XSD subsets
retain the transitive types and constraints for all implemented request operations
across six services. Provenance and regeneration steps are in
[tenant validation](docs/tenant-validation.md). Schema conformance does not prove
tenant-specific rules, permissions or approval routing.

```bash
uv venv
uv pip install -e '.[test,lint,typecheck]'
uv run --no-sync pytest -q --cov=workday_advanced_mcp --cov-report=term-missing --cov-fail-under=95
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv build
```

CI enforces 95% combined line/branch coverage, including the real stdio subprocess
handshake. Tenant calls are mocked. CI also checks lint, types, packaging and
source/wheel CLI entry points.

### 0.3 migration notes

- The server is stateless. `WORKDAY_STATE_PATH`, the SQLite journal,
  `workday_get_write_status`, the `request_id` argument and the `duplicate`,
  `write_status` and `tracking_id` submission fields are gone. The host owns
  duplicate suppression; reconcile unknown outcomes through Workday events.
- Preview tokens are valid in any process with the same configuration and
  credential, so preview and submission may run in different sandboxes. Tokens
  issued by 0.2 are rejected; preview again.

### 0.2 migration notes

- Preview output now uses `result`, `would_submit` and `state_token`, replacing
  operation-specific `plan`/`plans` output.
- Awaiting tasks are structured objects with task, status and people.
- Payment replacement/merge and no-change rules are stricter. Supply the full
  desired set or explicitly request a safe merge, then review the preview.
- Deploy persistent journal storage before enabling writes or previews.
- Callers must handle partial batch results and unknown write outcomes.

## License

MIT
