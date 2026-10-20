import httpx
import respx

from app.config import Settings
from app.market_data.feeds import ConfiguredFeedService, parse_feed


def test_parse_rss_feed():
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <title>AAPL earnings preview</title>
      <description><![CDATA[Analysts review <b>Apple</b> outlook.]]></description>
      <link>https://news.example/story</link><pubDate>Tue, 20 Oct 2026 15:00:00 GMT</pubDate>
    </item></channel></rss>"""
    items = parse_feed(xml, "https://news.example/rss", "news")
    assert len(items) == 1
    assert items[0]["title"] == "AAPL earnings preview"
    assert "Apple" in items[0]["summary"]


async def test_configured_feed_service_filters_symbol():
    settings = Settings(news_rss_urls="https://feeds.example.test/rss")
    with respx.mock:
        respx.get("https://feeds.example.test/rss").mock(return_value=httpx.Response(
            200, text="""<rss><channel>
            <item><title>AAPL earnings</title><description>Apple forecast</description>
            <link>https://example.test/a</link></item>
            <item><title>Oil outlook</title><description>Energy market</description>
            <link>https://example.test/b</link></item>
            </channel></rss>"""
        ))
        result = await ConfiguredFeedService(settings).fetch_news("AAPL")
    assert result["configured"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["title"] == "AAPL earnings"
