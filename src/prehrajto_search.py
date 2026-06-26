#!/usr/bin/env python3
"""Discover current prehraj.to uploads for a series episode."""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
import urllib.parse
from dataclasses import dataclass

import requests

SEARCH_URL = "https://prehraj.to/hledej/{query}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36"
_last_search_at = 0.0

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


def _split_env_list(name: str) -> list[str]:
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def _proxy_fetch_urls(search_url: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str, str]] = []
    base = os.environ.get("CZ_PROXY_URL", "").strip()
    key = os.environ.get("CZ_PROXY_KEY", "").strip()
    if base and key:
        pairs.append(("cz_proxy_1", base, key))
    base = os.environ.get("CZ_PROXY_URL_2", "").strip()
    key = os.environ.get("CZ_PROXY_KEY_2", "").strip()
    if base and key:
        pairs.append(("cz_proxy_2", base, key))
    for index, (base, key) in enumerate(zip(_split_env_list("CZ_PROXY_URLS"), _split_env_list("CZ_PROXY_KEYS")), start=1):
        if base and key:
            pairs.append((f"cz_proxy_list_{index}", base, key))

    seen: set[tuple[str, str]] = set()
    urls: list[tuple[str, str]] = []
    for label, base, key in pairs:
        pair_key = (base, key)
        if pair_key in seen:
            continue
        seen.add(pair_key)
        urls.append(
            (
                label,
                f"{base}?key={urllib.parse.quote(key, safe='')}"
                f"&url={urllib.parse.quote(search_url, safe='')}",
            )
        )
    return urls


def search(
    query: str,
    *,
    timeout: float = 30.0,
    min_interval: float = 3.0,
    retries: int = 3,
    session: requests.Session | None = None,
) -> list[SearchResult]:
    global _last_search_at
    sess = session or requests.Session()
    min_interval = max(min_interval, float(os.environ.get("PREHRAJTO_SEARCH_MIN_INTERVAL", "0") or 0))
    search_url = SEARCH_URL.format(query=urllib.parse.quote(query))
    fetch_urls = _proxy_fetch_urls(search_url)
    if fetch_urls:
        min_interval = max(
            min_interval,
            float(os.environ.get("CZ_PROXY_MIN_GAP_SECONDS", "5")),
        )
    else:
        fetch_urls = [("direct", search_url)]
    response: requests.Response | None = None
    for attempt in range(max(retries, 1)):
        for _fetch_label, fetch_url in fetch_urls:
            wait = min_interval - (time.monotonic() - _last_search_at)
            if wait > 0:
                time.sleep(wait)
            try:
                response = sess.get(
                    fetch_url,
                    timeout=timeout,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "cs,en;q=0.8",
                        "Accept-Encoding": "identity",
                    },
                )
            finally:
                _last_search_at = time.monotonic()
            if response.ok:
                return parse_search_html(response.text)
            if response.status_code != 429 or attempt + 1 >= retries:
                continue
        retry_after = response.headers.get("Retry-After")
        try:
            retry_seconds = float(retry_after) if retry_after else 0.0
        except ValueError:
            retry_seconds = 0.0
        time.sleep(max(retry_seconds, 10.0 * (attempt + 1)))
    if response is not None:
        response.raise_for_status()
    raise RuntimeError(f"Search failed without a response for {query!r}")
