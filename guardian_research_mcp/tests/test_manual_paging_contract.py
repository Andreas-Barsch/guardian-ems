"""Isolated MCP release contract; no Battery imports or productive endpoints."""
import asyncio
from urllib.parse import parse_qs
import httpx2
import pytest
from guardian_client import GuardianResearchClient
from server import build_app
from test_mcp_addon import settings, running_app, protocol_client, envelope


@pytest.mark.parametrize('tool,endpoint,extra', [
    ('query_module_history', 'module-history', {}),
    ('query_cell_history', 'cell-history', {'cell_numbers': [1]}),
    ('query_timeseries', 'timeseries', {'source': 'guardian.cell_history'}),
])
def test_explicit_empty_page_continuation(tool, endpoint, extra):
    calls = []
    response = envelope({'points': [], 'next_cursor': 'hp1.opaque'})
    response.update(next_cursor='hp1.opaque', truncated=True,
                    pagination={'coverage_scope': 'this_page_only',
                                'next_cursor': 'hp1.opaque'})

    async def backend(request):
        calls.append(request)
        return httpx2.Response(200, json=response)

    adapter = GuardianResearchClient(settings(), transport=httpx2.MockTransport(backend))
    with running_app(build_app(settings(), client=adapter)) as base:
        async def check(client):
            query = dict(physical_serial='TEST-M1', metric='soc',
                         timestamp_from='2026-10-01T00:00:00Z',
                         timestamp_to='2026-10-22T00:00:00Z', **extra)
            first = await client.call_tool(tool, query)
            assert not first.is_error
            assert first.structured_content == response
            assert len(calls) == 1  # Empty page does not cause an automatic follow-up.
            second = await client.call_tool(tool, {**query, 'cursor': first.structured_content['next_cursor']})
            assert not second.is_error and second.structured_content == response
            assert len(calls) == 2
            original = parse_qs(calls[0].url.query.decode())
            continued = parse_qs(calls[1].url.query.decode())
            assert original['paged'] == ['true']
            assert continued.pop('cursor') == ['hp1.opaque']
            assert continued == original
            assert all(r.method == 'GET' and r.url.path.endswith('/'+endpoint) for r in calls)
        asyncio.run(protocol_client(base, check))
