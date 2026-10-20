import httpx
import pytest
import respx

from app.inference.client import OpenAICompatibleClient


@pytest.mark.asyncio
async def test_complete_accepts_text_content_blocks():
    with respx.mock(base_url="http://127.0.0.1:8080/v1") as mock:
        route = mock.post("/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{
                "message": {"content": [{"type": "text", "text": "  {\"no_setup\": true}  "}]},
                "finish_reason": "stop",
            }],
        }))
        result = await OpenAICompatibleClient().complete(
            "http://127.0.0.1:8080/v1", "test-model", [{"role": "user", "content": "test"}]
        )

    assert result == '{"no_setup": true}'
    assert route.calls[0].request.content
    assert b'"max_tokens":4096' in route.calls[0].request.content


@pytest.mark.asyncio
async def test_empty_response_has_diagnostic_metadata():
    with respx.mock(base_url="http://127.0.0.1:8080/v1") as mock:
        mock.post("/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{
                "message": {"content": "", "reasoning_content": "thinking"},
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens": 1200},
        }))
        with pytest.raises(ValueError, match="finish_reason=length") as error:
            await OpenAICompatibleClient().complete(
                "http://127.0.0.1:8080/v1", "test-model", [{"role": "user", "content": "test"}]
            )

    assert "reasoning_content" in str(error.value)
    assert "disable thinking mode" in str(error.value)
