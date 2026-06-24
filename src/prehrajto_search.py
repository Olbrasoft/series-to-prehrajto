#!/usr/bin/env python3
"""Discover current prehraj.to uploads for a series episode."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from dataclasses import dataclass

import requests

SEARCH_URL = "https://prehraj.to/hledej/{query}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36"

_ITEM_RE = re.compile(
    r'<div class="video-wrapper">(?P<body>.*?)(?=<div class="video-wrapper">|</main>|$)',
    re.DOTALL,
)
_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*\bvideo--link\b[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]+title="(?P<title>[^"]+)"',
    re.DOTALL,
)
_DURATION_RE = re.compile(r'video__tag--time">\s*(?P<duration>\d{1,2}:\d{2}(?::\d{2})?)\s*<')
_SIZE_RE = re.compile(r'video__tag--size[^"]*">\s*(?P<size>[0-9.,]+)\s*(?P<unit>[MG]B)\s*<', re.IGNORECASE)
_FORMAT_RE = re.compile(r'format__text">\s*(?P<format>[^<]+)\s*<', re.IGNORECASE)


@dataclass(frozen=True)
class SearchResult:
    source_id: int
    external_id: str
    url: str
    title: str
    duration_sec: int | None
    resolution_hint: str | None
    filesize_bytes: int | None


def synthetic_source_id(external_id: str) -> int:
    digest = hashlib.sha256(f"prehrajto:{external_id}".encode()).digest()
    return -int.from_bytes(digest[:7], "big")


def duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def filesize_bytes(value: str | None, unit: str | None) -> int | None:
    if not value or not unit:
        return None
    number = float(value.replace(",", "."))
    multiplier = 1024**3 if unit.upper() == "GB" else 1024**2
    return int(number * multiplier)


def parse_search_html(page_html: str) -> list[SearchResult]:
    rows: list[SearchResult] = []
    seen: set[str] = set()
    for item in _ITEM_RE.finditer(page_html):
        body = item.group("body")
        link = _LINK_RE.search(body)
        if not link:
            continue
        href = html.unescape(link.group("href"))
        external_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9a-fA-F]{12,32}", external_id) or external_id in seen:
            continue
        duration_match = _DURATION_RE.search(body)
        size_match = _SIZE_RE.search(body)
        format_match = _FORMAT_RE.search(body)
        rows.append(
            SearchResult(
                source_id=synthetic_source_id(external_id),
                external_id=external_id,
                url=urllib.parse.urljoin("https://prehraj.to", href),
                title=html.unescape(link.group("title")).strip(),
                duration_sec=duration_seconds(duration_match.group("duration") if duration_match else None),
                resolution_hint=(format_match.group("format").strip() if format_match else None),
                filesize_bytes=filesize_bytes(
                    size_match.group("size") if size_match else None,
                    size_match.group("unit") if size_match else None,
                ),
            )
        )
        seen.add(external_id)
    return rows


def search(query: str, *, timeout: float = 30.0, session: requests.Session | None = None) -> list[SearchResult]:
    sess = session or requests.Session()
    response = sess.get(
        SEARCH_URL.format(query=urllib.parse.quote(query)),
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "cs,en;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    response.raise_for_status()
    return parse_search_html(response.text)
