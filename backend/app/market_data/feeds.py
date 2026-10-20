from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings


_TAG_RE = re.compile(r"<[^>]+>")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(element: ET.Element, *names: str) -> str:
    accepted = {name.lower() for name in names}
    for child in list(element):
        if _local_name(child.tag) in accepted:
            value = "".join(child.itertext()).strip()
            if value:
                return unescape(_TAG_RE.sub(" ", value)).strip()
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return ""


def parse_feed(xml_text: str, source_url: str, category: str) -> list[dict[str, str]]:
    """Parse a basic RSS 2.0 or Atom feed without executing embedded markup."""
    root = ET.fromstring(xml_text)
    entries = [
        node for node in root.iter()
        if _local_name(node.tag) in {"item", "entry"}
    ]
    items: list[dict[str, str]] = []
    for entry in entries:
        title = _text(entry, "title")
        summary = _text(entry, "description", "summary", "content", "encoded")
        link = _text(entry, "link")
        published = _text(entry, "pubdate", "published", "updated", "date")
        if title:
            items.append({
                "title": title[:500],
                "summary": summary[:2000],
                "link": link[:2000],
                "published": published[:200],
                "source": urlparse(source_url).hostname or "configured-feed",
                "category": category,
            })
    return items


class ConfiguredFeedService:
    """Reads operator-configured RSS/Atom feeds sequentially; no feed URL is model-controlled."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.timeout = settings.news_feed_timeout_seconds

    @staticmethod
    def _split_urls(value: str) -> list[str]:
        return list(dict.fromkeys(url.strip() for url in value.split(",") if url.strip()))

    async def _fetch(
        self, urls: list[str], category: str, symbol: str | None, limit: int
    ) -> dict[str, Any]:
        if not urls:
            return {"configured": False, "items": [], "errors": []}
        items: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for url in urls:
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.hostname:
                    errors.append({"source": parsed.hostname or "invalid-url", "error": "https_required"})
                    continue
                try:
                    response = await client.get(url, headers={"User-Agent": "TradeResearchAgent/0.1"})
                    response.raise_for_status()
                    items.extend(parse_feed(response.text, url, category))
                except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                    errors.append({"source": parsed.hostname, "error": type(exc).__name__})
        seen: set[tuple[str, str]] = set()
        cleaned: list[dict[str, str]] = []
        needle = symbol.upper().strip() if symbol else ""
        for item in items:
            identity = (item["title"], item["link"])
            if identity in seen:
                continue
            seen.add(identity)
            if needle and needle not in f'{item["title"]} {item["summary"]}'.upper():
                continue
            cleaned.append(item)
        return {"configured": True, "items": cleaned[:max(1, min(limit, 100))], "errors": errors}

    async def fetch_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        return await self._fetch(
            self._split_urls(self.settings.news_rss_urls), "news", symbol, limit
        )

    async def fetch_events(self, limit: int = 20) -> dict[str, Any]:
        return await self._fetch(
            self._split_urls(self.settings.economic_events_rss_urls),
            "economic_event", None, limit,
        )
