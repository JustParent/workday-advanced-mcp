# workday-advanced-mcp

An MCP server for the Workday writes a standard HRIS integration does not cover:
**supervisory organization moves, cost centre assignment, worker creation and
termination, and payment (bank) elections.**

It complements a first-party Workday integration rather than replacing it.
Reading people, time off and documents belongs on that connector; this server
exposes the staffing and payment business processes that sit behind them, so it
can be enabled only for the customers who need those changes made.

It runs over stdio, is installable with `uvx`, and talks to **Workday Web
Services (SOAP)** because hire, terminate and payment elections have no
equivalent in Workday's REST API.

## Why SOAP, and why no SOAP library

Workday's WSDLs run to several megabytes per web service. A client that parses
one at startup would spend that cost on every cold start, and a sandboxed MCP
server has to answer `initialize` within a fixed deadline. So this package
builds envelopes by hand with the standard library: each operation owns a small
builder that emits elements in the order Workday's schema requires, and the
tests assert that order. Startup touches no network at all.

## Workday setup

1. Create an **Integration System User (ISU)**:
   - Create the user, set *Session Timeout Minutes* to `0` and tick
     *Do Not Allow UI Sessions*.
   - Exempt it from password expiry (Maintain Password Rules -> *System Users
     exempt from password expiration*), or the integration breaks silently when
     the password ages out.
2. Create an **Integration System Security Group (Unconstrained)**, add the ISU,
   and grant it:
   - **Domain policies:** Worker Data: Public Worker Reports; Worker Data:
     Current Staffing Information; Worker Data: Organization Information;
     Set Up: Organization; Manage: Pre-Hire Process; Worker Data: Payment
     Elections.
   - **Business process policies** (Initiate, plus View): Hire, Change Job,
     Change Organization Assignments, Termination, Payment Election Enrollment.
3. **Activate Pending Security Policy Changes.** Nothing above takes effect
   until you do.
4. If the tenant restricts API access by IP, allow the outbound address of
   wherever this server runs, or route it through a fixed IP with
   `WORKDAY_PROXY_URL`.

Repeated failed sign-ins lock an ISU, so this server never retries an
authentication failure.

## Configuration

| Environment variable | Required | Description |
| --- | --- | --- |
| `WORKDAY_HOST` | yes | API host, e.g. `wd3-impl-services1.workday.com`. A pasted tenant URL is accepted; only the hostname is used. |
| `WORKDAY_TENANT` | yes | Tenant id, e.g. `acme_preview`. Inferred from a pasted URL when omitted. |
| `WORKDAY_AUTH_MODE` | no | `isu` (default) or `oauth`. With no ISU password and a complete OAuth trio, `oauth` is inferred. |
| `WORKDAY_ISU_USERNAME` | ISU | Integration System User name. `@tenant` is appended if missing. |
| `WORKDAY_ISU_PASSWORD` | ISU | That user's password. |
| `WORKDAY_CLIENT_ID` | OAuth | API client for integrations. |
| `WORKDAY_CLIENT_SECRET` | OAuth | |
| `WORKDAY_REFRESH_TOKEN` | OAuth | Exchanged for a bearer token with the `soap_api` scope. |
| `WORKDAY_API_VERSION` | no | Defaults to `v44.2`. Must exist in the tenant. |
| `WORKDAY_READ_ONLY` | no | `true` registers only the read tools; the write tools do not exist at all. |
| `WORKDAY_ENABLED_TOOLS` | no | Comma-separated allowlist. Only these tools register - e.g. omit the payment tools entirely. |
| `WORKDAY_ALLOW_AUTO_COMPLETE` | no | `true` permits `auto_complete` on a write, which bypasses the tenant's own approval routing. Off by default; turn it on only for a supervised bulk load. |
| `WORKDAY_PROXY_URL` | no | `socks5h://...` or `http://...` for fixed-IP egress. |
| `WORKDAY_HTTP_TIMEOUT` | no | Seconds per call, default `90`. Keep it below the host's per-request timeout. |

## Running it

Pinned to a commit, which is how it should be deployed:

```bash
uvx --from 'git+https://github.com/JustParent/workday-advanced-mcp@<GIT_SHA>' workday-advanced-mcp
```

From a local checkout, during development:

```bash
uvx --from . workday-advanced-mcp --test
```

`--test` prints the version, the resolved endpoint, the auth mode, whether
credentials are set (never their values), the read-only state and every
registered tool, then exits. It verifies an install without an MCP client or
live credentials.

### Plugging into a sandboxed MCP integration

For a host that runs MCP servers as sandboxed subprocesses using the Claude
Desktop config shape:

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
  "sandbox_timeout": "120",
  "auth_type": "none",
  "sandbox_env": {
    "WORKDAY_HOST": "<host>",
    "WORKDAY_TENANT": "<tenant>",
    "WORKDAY_ISU_USERNAME": "<isu>",
    "WORKDAY_ISU_PASSWORD": "$SECRET_KEY"
  }
}
```

Put the ISU password in the integration's secret key field: `$SECRET_KEY` is
substituted inside the sandbox, so it is never stored in the config itself.

No `--with 'mcp<2'` argument is needed - this package pins the MCP SDK itself.

## Tools

### Read

| Tool | Workday operation | Notes |
| --- | --- | --- |
| `workday_find_worker` | `Get_Workers` | By employee ID, WID, or work email. An email costs a bounded scan and reports `scan_complete`. |
| `workday_get_worker` | `Get_Workers` | Job, position, supervisory org, organization assignments, status. Read this before any write. |
| `workday_list_organizations` | `Get_Organizations` | One tool for supervisory orgs, cost centres, companies, regions, business units. |
| `workday_list_reference_ids` | `Get_References` | Tenant-defined vocabularies: termination reasons, job change reasons. |
| `workday_list_job_profiles` | `Get_Job_Profiles` | |
| `workday_list_locations` | `Get_Locations` | |
| `workday_list_pre_hires` | `Get_Applicants` | Find an existing pre-hire before creating one. |
| `workday_get_payment_elections` | `Get_Payment_Election_Enrollments` | Account numbers masked to the last four digits. |
| `workday_get_event` | `Get_Event_Details` | Where a submitted business process got to. The recovery path after a timeout. |
| `workday_preview_write` | none | Resolves references and shows what a write would send and change, without sending it. |

### Write (omitted when `WORKDAY_READ_ONLY` is set)

| Tool | Workday operation | Notes |
| --- | --- | --- |
| `workday_create_pre_hire` | `Put_Applicant` | Refuses a duplicate email unless `force`. |
| `workday_hire_employee` | `Hire_Employee` | Refuses when an active worker already matches. |
| `workday_change_job` | `Change_Job` | The supervisory organization move. Accepts a batch. |
| `workday_change_organization_assignments` | `Change_Organization_Assignments` | Cost centre and company. Merges with current assignments. Accepts a batch. |
| `workday_terminate_employee` | `Terminate_Employee` | Destructive. Refuses an already-terminated worker. |
| `workday_set_payment_elections` | `Submit_Payment_Election_Enrollment` | Destructive. Merges by default; `replace` is explicit. |

### Rules the write tools follow

- **Nothing is guessed.** Reference IDs are tenant-specific; resolve them with
  the list tools and pass them in.
- **`auto_complete` defaults to false**, so the tenant's approval routing runs.
  When it is false, a write is verified by the *event* it created, not by
  re-reading the worker: the data has not changed yet.
- **A timeout is ambiguous.** The write may have committed. Call
  `workday_get_event` or `workday_find_worker` before sending it again; the
  server never retries a write on its own.
- **Replace-all operations merge first.** Payment elections and organization
  assignments both replace the set they are given, so both tools read the
  current set and merge into it unless told otherwise.

## Schema verification status

Envelope shapes follow the published Workday Web Services v44.2 schemas, and the
element order of every request is locked by `tests/test_envelopes.py`. Business
processes are configured per tenant, so required fields, reason codes and
sub-processes still have to be confirmed against the customer's implementation
tenant; captured request/response pairs belong in `tests/fixtures/` as they are
verified.

## Development

```bash
uv venv
uv pip install -e '.[test,lint,typecheck]'
pytest
ruff check .          # add --fix to apply the automatic fixes
ruff format .         # CI runs --check, so format before pushing
mypy
```

Inspect the tools interactively:

```bash
npx @modelcontextprotocol/inspector uvx --from . workday-advanced-mcp
```

## License

MIT
