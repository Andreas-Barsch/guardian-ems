"""Explicit safe history failures, distinct from transport unavailability."""
import asyncio
import httpx2
import pytest
from errors import GatewayError
from guardian_client import GuardianResearchClient
from test_mcp_addon import settings


@pytest.mark.parametrize('code', ['live_acquisition_priority', 'history_busy',
    'history_index_required', 'history_context_index_required',
    'history_preparation_required', 'history_record_too_large', 'cancelled',
    'history_window_budget', 'history_product_invalid'])
def test_history_error_survives_transport_without_backend_details_or_retry(code):
    calls = []
    async def backend(request):
        calls.append(request)
        return httpx2.Response(503, json={'error': {'code': code, 'message': '/private/secret token=hidden'}})
    client = GuardianResearchClient(settings(), transport=httpx2.MockTransport(backend))
    with pytest.raises(GatewayError) as error:
        asyncio.run(client.get('module-history', {}))
    result = error.value.as_tool_result()
    assert error.value.code == code and error.value.origin == 'guardian_api'
    assert result.is_error and result.structured_content['error']['code'] == code
    assert 'secret' not in str(result) and 'hidden' not in str(result)
    assert len(calls) == 1


def test_unknown_error_still_redacted():
    error = GatewayError('/private/secret', 'token=hidden', origin='guardian_api')
    assert error.code == 'source_unavailable' and 'hidden' not in str(error)
