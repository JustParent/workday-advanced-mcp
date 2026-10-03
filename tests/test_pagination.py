"""Paged scans must not drift while workers or accounts change in Workday."""

from __future__ import annotations

import httpx
import pytest
import respx

from conftest import call_tool, service_url, soap_response
from test_lifecycle import org, org_response
from test_schema_contracts import assert_schema, samples
from test_tools_read import ENROLLMENTS
from workday_advanced_mcp.client import get_client
from workday_advanced_mcp.pagination import at_entry_moment, entry_moment
from workday_advanced_mcp.resolve import scan_for_email
from workday_advanced_mcp.soap import parse_envelope
from workday_advanced_mcp.tools_read import _scan_enrollments

MOMENT = "2026-10-01T08:00:00Z"
HR = service_url("Human_Resources")


@pytest.mark.parametrize(
    "service,operation,payload",
    [(s, o, p) for s, o, p in samples() if "Response_Filter" in p],
)
def test_paged_snapshot_matches_published_schema(service, operation, payload):
    assert_schema(service, operation, at_entry_moment(payload, MOMENT))


def test_snapshot_timestamp_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        entry_moment("2026-10-01T08:00:00")


@pytest.mark.parametrize("scan", ["email", "enrollments", "organizations", "manager"])
async def test_all_internal_scans_pin_one_entry_moment(
    server_factory, worker_response, scan
):
    moments = []

    def handle(request):
        body = parse_envelope(request.content.decode()).body
        root = next(iter(body))
        data = body[root]
        filters = data.get("Response_Filter", {})
        if "Page" not in filters:
            return httpx.Response(200, text=worker_response)
        moments.append(filters["As_Of_Entry_DateTime"])
        page = int(filters["Page"])
        pages = f"<wd:Response_Results><wd:Page>{page}</wd:Page><wd:Total_Pages>2</wd:Total_Pages></wd:Response_Results>"
        if scan == "email":
            response = worker_response.replace(
                "<wd:Response_Data>", pages + "<wd:Response_Data>"
            )
        elif scan == "enrollments":
            # The first worker isn't the requested one; page two finds the match.
            response = ENROLLMENTS.replace(
                "<wd:Payment_Election_Enrollment_Response_Data>",
                pages + "<wd:Payment_Election_Enrollment_Response_Data>",
            )
            if page == 1:
                response = response.replace("worker-wid-1", "someone-else")
        else:
            response = org_response(org(f"ORG-{page}"), pages=2).replace(
                "<bsvc:Page>1</bsvc:Page>", f"<bsvc:Page>{page}</bsvc:Page>"
            )
        return httpx.Response(200, text=response)

    with respx.mock:
        respx.post(HR).mock(side_effect=handle)
        respx.post(service_url("Cash_Management")).mock(side_effect=handle)
        if scan == "email":
            _, complete = await scan_for_email(get_client(), "jane.doe@example.com")
            assert complete
        elif scan == "enrollments":
            _, complete = await _scan_enrollments(get_client(), "worker-wid-1", 2)
            assert complete
        elif scan == "organizations":
            result = await call_tool(
                server_factory(),
                "workday_list_organizations",
                {"organization_type": "Supervisory"},
            )
            assert result["count"] == 2
        else:
            result = await call_tool(
                server_factory(),
                "workday_get_manager_organizations",
                {"worker_id": "21001"},
            )
            assert result["scan_complete"]
    assert len(moments) == 2
    assert moments[0] == moments[1]
    assert entry_moment(moments[0]) == moments[0]


@pytest.mark.parametrize(
    "operation,tool,arguments",
    [
        ("Get_Positions", "workday_list_positions", {"organization_id": "TOP"}),
        (
            "Get_Workers",
            "workday_list_organization_workers",
            {"organization_id": "TOP"},
        ),
    ],
)
async def test_caller_paging_can_reuse_returned_snapshot(
    server_factory, operation, tool, arguments
):
    service = "Staffing" if operation == "Get_Positions" else "Human_Resources"
    with respx.mock:
        route = respx.post(service_url(service)).mock(
            return_value=httpx.Response(
                200, text=soap_response(operation, "<wd:Response_Data/>")
            )
        )
        server = server_factory()
        first = await call_tool(server, tool, arguments)
        moment = first["as_of_entry_datetime"]
        await call_tool(
            server, tool, {**arguments, "page": 2, "as_of_entry_datetime": moment}
        )
    for call in route.calls:
        assert moment in call.request.content.decode()
