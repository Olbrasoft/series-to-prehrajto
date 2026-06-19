#!/usr/bin/env python3
"""Export episode video sources that need language verification.

The queue is stored in the repository so GitHub runners can audit sources
without production DB access. Results from `audit_language_sources.py` are
import-ready back into `video_sources` / episode language rollups later.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras


def sktorrent_url(external_id: str | None) -> str | None:
    if not external_id:
        return None
    return f"https://online.sktorrent.eu/video/{external_id}/x"


def fetch_rows(
    conn,
    *,
    limit: int,
    series_slug: str | None,
    season: int | None,
    episode: int | None,
) -> list[dict[str, Any]]:
    where = [
        "vs.is_alive",
        "vs.episode_id IS NOT NULL",
        "vp.slug = ANY(%(providers)s)",
    ]
    params: dict[str, Any] = {"limit": limit, "providers": ["prehrajto", "sktorrent", "sledujteto"]}
    if series_slug:
        where.append("s.slug = %(series_slug)s")
        params["series_slug"] = series_slug
    if season is not None:
        where.append("e.season = %(season)s")
        params["season"] = season
    if episode is not None:
        where.append("e.episode = %(episode)s")
        params["episode"] = episode
    if not series_slug:
        where.append(
            """(
                vs.lang_class IN ('UNKNOWN', 'CZ_DUB', 'CZ_NATIVE', 'CZ_SUB')
                OR vs.audio_lang IS NULL
                OR vs.audio_detected_by IS NULL
                OR coalesce(array_length(e.audio_langs, 1), 0) = 0
            )"""
        )

    sql = f"""
        SELECT
            s.id AS series_id,
            s.title AS series_title,
            s.slug AS series_slug,
            s.imdb_rating,
            s.imdb_votes,
            e.id AS episode_id,
            e.season,
            e.episode,
            e.title AS episode_title,
            e.episode_name,
            e.audio_langs AS episode_audio_langs,
            e.subtitle_langs AS episode_subtitle_langs,
            vp.slug AS provider,
            vs.id AS source_id,
            vs.external_id,
            vs.title AS source_title,
            vs.duration_sec,
            vs.resolution_hint,
            vs.filesize_bytes,
            vs.view_count,
            vs.lang_class,
            vs.audio_lang,
            vs.audio_confidence,
            vs.audio_detected_by,
            vs.metadata,
            coalesce(vs.metadata->>'url', NULL) AS metadata_url
        FROM video_sources vs
        JOIN video_providers vp ON vp.id = vs.provider_id
        JOIN episodes e ON e.id = vs.episode_id
        JOIN series s ON s.id = e.series_id
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE WHEN s.slug = 'hvezdne-mestecko' THEN 0 ELSE 1 END,
            coalesce(s.imdb_votes, 0) DESC,
            coalesce(s.imdb_rating, 0) DESC,
            s.id,
            e.season NULLS LAST,
            e.episode NULLS LAST,
            CASE vp.slug WHEN 'prehrajto' THEN 0 WHEN 'sktorrent' THEN 1 ELSE 2 END,
            CASE vs.lang_class WHEN 'CZ_DUB' THEN 0 WHEN 'CZ_NATIVE' THEN 1 WHEN 'CZ_SUB' THEN 2 ELSE 3 END,
            coalesce(vs.view_count, 0) DESC,
            vs.id
        LIMIT %(limit)s;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def row_to_queue_item(row: dict[str, Any]) -> dict[str, Any]:
    provider = row["provider"]
    url = row["metadata_url"]
    if provider == "sktorrent" and not url:
        url = sktorrent_url(str(row["external_id"]) if row["external_id"] is not None else None)
    if url and url.startswith("https://prehrajto.cz/"):
        url = url.replace("https://prehrajto.cz/", "https://prehraj.to/")
    return {
        "series_id": row["series_id"],
        "series_slug": row["series_slug"],
        "series_title": row["series_title"],
        "episode_id": row["episode_id"],
        "season": row["season"],
        "episode": row["episode"],
        "episode_title": row["episode_title"],
        "episode_name": row["episode_name"],
        "episode_audio_langs": row["episode_audio_langs"] or [],
        "episode_subtitle_langs": row["episode_subtitle_langs"] or [],
        "provider": provider,
        "source_id": row["source_id"],
        "external_id": row["external_id"],
        "source_url": url,
        "source_title": row["source_title"],
        "duration_sec": row["duration_sec"],
        "resolution_hint": row["resolution_hint"],
        "filesize_bytes": row["filesize_bytes"],
        "view_count": row["view_count"],
        "db_lang_class": row["lang_class"],
        "db_audio_lang": row["audio_lang"],
        "db_audio_confidence": row["audio_confidence"],
        "db_audio_detected_by": row["audio_detected_by"],
        "metadata": row["metadata"] or {},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backlog/language-audit-queue.jsonl.gz")
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--series-slug")
    ap.add_argument("--season", type=int)
    ap.add_argument("--episode", type=int)
    args = ap.parse_args()

    if not args.db_url:
        print("ERROR: --db-url or DATABASE_URL required", file=sys.stderr)
        return 2

    conn = psycopg2.connect(args.db_url)
    try:
        rows = fetch_rows(
            conn,
            limit=args.limit,
            series_slug=args.series_slug,
            season=args.season,
            episode=args.episode,
        )
    finally:
        conn.close()

    items = [row_to_queue_item(row) for row in rows]
    opener = gzip.open if args.out.endswith(".gz") else open
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with opener(args.out, "wt", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Wrote {len(items)} sources to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
