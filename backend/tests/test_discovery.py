import httpx
import respx

from app.inference.discovery import EndpointDiscovery


async def test_discovery_selects_configured_model_sequentially():
    with respx.mock(assert_all_called=True) as mock:
        first = mock.get("http://127.0.0.1:1/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "qwen-local"}]})
        )
        second = mock.get("http://127.0.0.1:2/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "other"}, {"id": "wanted"}]})
        )
        discovery = EndpointDiscovery(
            ["http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1"],
            preferred_model="wanted",
        )
        results = await discovery.discover()
    assert first.called and second.called
    assert [x.model_id for x in results] == ["qwen-local", "wanted"]


async def test_discovery_ignores_offline_endpoints():
    with respx.mock:
        respx.get("http://127.0.0.1:9999/v1/models").mock(side_effect=httpx.ConnectError("offline"))
        results = await EndpointDiscovery(["http://127.0.0.1:9999/v1"]).discover()
    assert results == []
