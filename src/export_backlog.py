#!/usr/bin/env python3
"""Export a small upload backlog of series episodes from the production CR DB.

The query is intentionally scoped: pick top series first, then first N episodes,
then source candidates. This avoids expensive global sorting over millions of
video_sources rows and gets the upload workflow running quickly.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras

LANG_CLASSES = ("CZ_DUB", "CZ_NATIVE", "CZ_SUB")


def sxe(season: int | None, episode: int | None) -> str:
    return f"S{int(season or 0):02d}E{int(episode or 0):02d}"


def display_name(row: dict[str, Any]) -> str:
    base = f"{row['series_title']} {sxe(row['season'], row['episode'])}"
    subtitle = (row.get("episode_name") or row.get("episode_title") or "").strip()
    if subtitle and subtitle.lower() != str(row["series_title"]).lower():
        base = f"{base} - {subtitle}"
    if row.get("preferred_lang_class") == "CZ_SUB":
        return f"{base} CZ Titulky"
    return f"{base} CZ Dabing"


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("https://prehrajto.cz/", "https://prehraj.to/")


def fetch_rows(conn, *, series_limit: int, episode_limit: int, source_limit_per_episode: int) -> list[dict[str, Any]]:
    sql = """
        WITH selected_series AS (
            SELECT
                s.*,
                row_number() OVER (
                    ORDER BY coalesce(s.imdb_votes, 0) DESC,
                             coalesce(s.imdb_rating, 0) DESC,
                             coalesce(s.csfd_rating, 0) DESC,
                             s.id
                ) AS series_rank
            FROM series s
            WHERE EXISTS (
                SELECT 1
                FROM episodes e
                JOIN video_sources vs ON vs.episode_id = e.id
                WHERE e.series_id = s.id
                  AND vs.provider_id = 2
                  AND vs.is_alive
                  AND vs.lang_class = ANY(%s)
                  AND vs.metadata ? 'url'
            )
            LIMIT %s
        ),
        selected_episodes AS (
            SELECT
                e.*,
                ss.series_rank,
                ss.title AS series_title,
                ss.original_title AS series_original_title,
                ss.slug AS series_slug,
                ss.first_air_year,
                ss.description AS series_description,
                ss.tmdb_overview_en AS series_overview_en,
                ss.imdb_id,
                ss.tmdb_id,
                ss.imdb_rating,
                ss.imdb_votes,
                ss.csfd_rating
            FROM selected_series ss
            JOIN episodes e ON e.series_id = ss.id
            WHERE EXISTS (
                SELECT 1
                FROM video_sources vs
                WHERE vs.episode_id = e.id
                  AND vs.provider_id = 2
                  AND vs.is_alive
                  AND vs.lang_class = ANY(%s)
                  AND vs.metadata ? 'url'
            )
            ORDER BY ss.series_rank, e.season NULLS LAST, e.episode NULLS LAST, e.id
            LIMIT %s
        ),
        ranked_sources AS (
            SELECT
                se.*,
                vs.id AS source_id,
                vs.external_id,
                vs.title AS source_title,
                vs.duration_sec AS source_duration_sec,
                vs.resolution_hint,
                vs.filesize_bytes,
                vs.view_count,
                vs.lang_class,
                vs.audio_lang,
                vs.audio_confidence,
                vs.metadata->>'url' AS source_url,
                row_number() OVER (
                    PARTITION BY se.id
                    ORDER BY
                        CASE vs.lang_class WHEN 'CZ_DUB' THEN 0 WHEN 'CZ_NATIVE' THEN 1 ELSE 2 END,
                        CASE
                            WHEN coalesce(vs.resolution_hint, '') ~* '(2160|4k|uhd)' THEN 4
                            WHEN coalesce(vs.resolution_hint, '') ~* '1080|full.?hd' THEN 3
                            WHEN coalesce(vs.resolution_hint, '') ~* '720|hd' THEN 2
                            ELSE 0
                        END DESC,
                        coalesce(vs.view_count, 0) DESC,
                        vs.id
                ) AS source_rank
            FROM selected_episodes se
            JOIN video_sources vs ON vs.episode_id = se.id
            WHERE vs.provider_id = 2
              AND vs.is_alive
              AND vs.lang_class = ANY(%s)
              AND vs.metadata ? 'url'
        )
        SELECT *
        FROM ranked_sources
        WHERE source_rank <= %s
        ORDER BY series_rank, season NULLS LAST, episode NULLS LAST, id, source_rank;
    """
    params = (
        list(LANG_CLASSES),
        series_limit,
        list(LANG_CLASSES),
        episode_limit,
        list(LANG_CLASSES),
        source_limit_per_episode,
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def resolution_score(value: str | None) -> int:
    text = (value or "").lower()
    if re.search(r"2160|4k|uhd", text):
        return 2160
    match = re.search(r"(?<!\d)(1080|720|576|480)(?!\d)", text)
    return int(match.group(1)) if match else 0


def group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_episode: dict[int, dict[str, Any]] = {}
    for row in rows:
        eid = row["id"]
        if eid not in by_episode:
            preferred = row["lang_class"]
            item = {
                "episode_id": eid,
                "series_id": row["series_id"],
                "series_slug": row["series_slug"],
                "series_title": row["series_title"],
                "series_original_title": row["series_original_title"],
                "first_air_year": row["first_air_year"],
                "season": row["season"],
                "episode": row["episode"],
                "episode_code": sxe(row["season"], row["episode"]),
                "episode_title": row["title"],
                "episode_name": row["episode_name"],
                "air_date": row["air_date"].isoformat() if row["air_date"] else None,
                "runtime": row["runtime"],
                "imdb_id": row["imdb_id"],
                "tmdb_id": row["tmdb_id"],
                "imdb_rating": row["imdb_rating"],
                "imdb_votes": row["imdb_votes"],
                "csfd_rating": row["csfd_rating"],
                "preferred_lang_class": preferred,
                "series_description": row["series_description"] or "",
                "series_overview_en": row["series_overview_en"] or "",
                "source_description": row["description"] or row["overview"] or row["series_description"] or row["series_overview_en"] or "",
                "description": row["description"] or row["overview"] or row["series_description"] or "",
                "candidates": [],
            }
            item["display_name"] = display_name(item)
            by_episode[eid] = item
        item = by_episode[eid]
        url = normalize_url(row["source_url"])
        if not url:
            continue
        item["candidates"].append(
            {
                "source_id": row["source_id"],
                "external_id": row["external_id"],
                "url": url,
                "title": row["source_title"],
                "duration_sec": row["source_duration_sec"],
                "resolution_hint": row["resolution_hint"],
                "resolution_score": resolution_score(row["resolution_hint"]),
                "filesize_bytes": row["filesize_bytes"],
                "view_count": row["view_count"],
                "lang_class": row["lang_class"],
                "audio_lang": row["audio_lang"],
                "audio_confidence": row["audio_confidence"],
            }
        )

    for item in by_episode.values():
        item["candidates"].sort(
            key=lambda c: (
                0 if c["lang_class"] in {"CZ_DUB", "CZ_NATIVE"} else 1,
                -int(c.get("resolution_score") or 0),
                -(c.get("view_count") or 0),
                c["source_id"],
            )
        )
    return list(by_episode.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--series-limit", type=int, default=8)
    ap.add_argument("--episode-limit", type=int, default=80)
    ap.add_argument("--source-limit-per-episode", type=int, default=8)
    args = ap.parse_args()

    if not args.db_url:
        print("ERROR: --db-url or DATABASE_URL required", file=sys.stderr)
        return 2

    conn = psycopg2.connect(args.db_url)
    try:
        rows = fetch_rows(
            conn,
            series_limit=args.series_limit,
            episode_limit=args.episode_limit,
            source_limit_per_episode=args.source_limit_per_episode,
        )
    finally:
        conn.close()

    episodes = group_rows(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    opener = gzip.open if args.out.endswith(".gz") else open
    with opener(args.out, "wt", encoding="utf-8") as fh:
        for episode in episodes:
            fh.write(json.dumps(episode, ensure_ascii=False) + "\n")

    counts: dict[str, int] = defaultdict(int)
    for episode in episodes:
        counts[episode["series_title"]] += 1
    print(f"Wrote {len(episodes)} episodes to {args.out}")
    for title, count in counts.items():
        print(f"  {title}: {count}")
    print(f"  total candidate sources: {sum(len(e['candidates']) for e in episodes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
