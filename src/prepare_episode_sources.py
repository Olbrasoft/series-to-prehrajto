#!/usr/bin/env python3
"""Prepare upload-ready source choices for series episodes.

This is a pre-upload planning step. It walks episode sources, checks language
signals for every candidate it sees, stores the evidence, and picks the best
source for later uploading.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_language_sources import audit_one  # noqa: E402
from language_checks import resolution_score  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_prepared_episode_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    ids: set[int] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                ids.add(int(json.loads(line)["episode_id"]))
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    return ids


def group_by_episode(rows: list[dict]) -> list[dict]:
    grouped: dict[int, dict] = {}
    for row in rows:
        eid = int(row["episode_id"])
        if eid not in grouped:
            grouped[eid] = {
                "episode_id": eid,
                "series_id": row["series_id"],
                "series_slug": row["series_slug"],
                "series_title": row["series_title"],
                "season": row["season"],
                "episode": row["episode"],
                "episode_name": row.get("episode_name") or row.get("episode_title"),
                "sources": [],
            }
        grouped[eid]["sources"].append(row)
    return sorted(
        grouped.values(),
        key=lambda item: (
            item["series_slug"],
            int(item["season"] or 0),
            int(item["episode"] or 0),
            item["episode_id"],
        ),
    )


def probe_resolution(result: dict, source: dict) -> int:
    probe = result.get("signals", {}).get("provider_probe") or {}
    streams = probe.get("streams") or []
    if streams:
        return max(int(stream.get("res") or 0) for stream in streams)
    return resolution_score(source.get("resolution_hint"))


def source_score(result: dict, source: dict) -> tuple:
    verdict = result["verdict"]
    verdict_score = {
        "CZ_AUDIO": 100,
        "PROBABLE_CZ_AUDIO": 70,
        "CZ_SUBTITLES_ONLY": 35,
        "SK_AUDIO": 20,
        "UNKNOWN": 0,
        "NOT_CZ_AUDIO": -100,
    }.get(verdict, 0)
    detected_bonus = {"whisper": 30, "metadata": 20, "provider_tracks": 10, "title": 5}.get(result["detected_by"], 0)
    provider_bonus = {"prehrajto": 20, "sktorrent": 10, "sledujteto": 0}.get(source.get("provider"), 0)
    return (
        verdict_score + detected_bonus + provider_bonus,
        probe_resolution(result, source),
        int(source.get("view_count") or 0),
        -int(source["source_id"]),
    )


def prepare_episode(episode: dict, *, use_whisper: bool, sample_seconds: int, source_limit: int) -> dict:
    sources = episode["sources"][:source_limit] if source_limit > 0 else episode["sources"]
    audited = []
    for source in sources:
        result = audit_one(source, use_whisper=use_whisper, sample_seconds=sample_seconds)
        result["score"] = source_score(result, source)
        result["resolution_score"] = probe_resolution(result, source)
        audited.append(result)
    acceptable = [r for r in audited if r["verdict"] in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO", "CZ_SUBTITLES_ONLY"}]
    selected = max(acceptable, key=lambda r: tuple(r["score"])) if acceptable else None
    upload_ready = bool(selected and selected["verdict"] in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"})
    return {
        "prepared_at": audited[-1]["audited_at"] if audited else None,
        "episode_id": episode["episode_id"],
        "series_id": episode["series_id"],
        "series_slug": episode["series_slug"],
        "series_title": episode["series_title"],
        "season": episode["season"],
        "episode": episode["episode"],
        "episode_name": episode.get("episode_name"),
        "tested_source_count": len(audited),
        "upload_ready": upload_ready,
        "selected_source": selected,
        "tested_sources": audited,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="backlog/language-audit-queue.jsonl.gz")
    ap.add_argument("--out", default="plans/prepared-episodes.jsonl")
    ap.add_argument("--audit-out", default="audits/language-audit.jsonl")
    ap.add_argument("--episode-limit", type=int, default=10)
    ap.add_argument("--source-limit-per-episode", type=int, default=12)
    ap.add_argument("--series-slug")
    ap.add_argument("--use-whisper", action="store_true")
    ap.add_argument("--sample-seconds", type=int, default=45)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    rows = load_jsonl(Path(args.queue))
    if args.series_slug:
        rows = [row for row in rows if row["series_slug"] == args.series_slug]
    episodes = group_by_episode(rows)
    done = set() if args.refresh else latest_prepared_episode_ids(Path(args.out))
    todo = [episode for episode in episodes if int(episode["episode_id"]) not in done]
    todo = todo[: args.episode_limit]

    prepared = [
        prepare_episode(
            episode,
            use_whisper=args.use_whisper,
            sample_seconds=args.sample_seconds,
            source_limit=args.source_limit_per_episode,
        )
        for episode in todo
    ]
    append_jsonl(Path(args.out), prepared)
    audit_rows = [source for episode in prepared for source in episode["tested_sources"]]
    append_jsonl(Path(args.audit_out), audit_rows)

    for episode in prepared:
        selected = episode.get("selected_source")
        selected_text = (
            f"selected source_id={selected['source_id']} {selected['verdict']} by={selected['detected_by']}"
            if selected
            else "no acceptable source"
        )
        print(
            f"{episode['series_title']} S{int(episode['season']):02d}E{int(episode['episode']):02d}: "
            f"tested={episode['tested_source_count']} ready={episode['upload_ready']} {selected_text}"
        )
    print(f"Prepared {len(prepared)} episodes into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
