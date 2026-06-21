#!/usr/bin/env python3
"""Build a single upload-ready manifest from prepared series data."""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path

from description_quality import is_valid_generated_description

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_by_episode(rows: list[dict]) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for row in rows:
        if row.get("episode_id") is None:
            continue
        latest[int(row["episode_id"])] = row
    return latest


def latest_audits_by_source(rows: list[dict]) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for row in rows:
        if row.get("source_id") is None:
            continue
        source_id = int(row["source_id"])
        old = latest.get(source_id)
        if old is None or str(row.get("audited_at") or "") >= str(old.get("audited_at") or ""):
            latest[source_id] = row
    return latest


def description_indexes(rows: list[dict]) -> tuple[dict[int, dict], dict[int, dict]]:
    series: dict[int, dict] = {}
    episodes: dict[int, dict] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        if not is_valid_generated_description(row.get("generated_description") or ""):
            continue
        if row.get("kind") == "series" and row.get("series_id") is not None:
            series[int(row["series_id"])] = row
        elif row.get("kind") == "episode" and row.get("episode_id") is not None:
            episodes[int(row["episode_id"])] = row
    return series, episodes


def selected_candidate(episode: dict, selected_source_id: int) -> dict | None:
    for candidate in episode.get("candidates") or []:
        if int(candidate["source_id"]) == selected_source_id:
            return candidate
    return None


def build_manifest(
    *,
    backlog_path: Path,
    prepared_path: Path,
    descriptions_path: Path,
    audits_path: Path,
    limit: int,
    require_episode_description: bool,
    require_whisper: bool,
) -> tuple[list[dict], Counter]:
    backlog = load_jsonl(backlog_path)
    prepared = latest_by_episode(load_jsonl(prepared_path))
    desc_series, desc_episodes = description_indexes(load_jsonl(descriptions_path))
    audits = latest_audits_by_source(load_jsonl(audits_path))
    rows: list[dict] = []
    stats: Counter = Counter()

    for episode in backlog:
        if limit and len(rows) >= limit:
            break
        episode_id = int(episode["episode_id"])
        plan = prepared.get(episode_id)
        if not plan:
            stats["missing_source_plan"] += 1
            continue
        selected = plan.get("selected_source") or {}
        if not plan.get("upload_ready") or not selected:
            stats["not_upload_ready"] += 1
            continue
        source_id = int(selected["source_id"])
        candidate = selected_candidate(episode, source_id)
        if not candidate:
            stats["selected_source_not_in_backlog"] += 1
            continue
        audit = audits.get(source_id) or selected
        whisper = (audit.get("signals") or {}).get("whisper") or {}
        if require_whisper and whisper.get("status") != "ok":
            stats["missing_whisper_confirmation"] += 1
            continue

        ep_desc = desc_episodes.get(episode_id)
        series_desc = desc_series.get(int(episode["series_id"]))
        description_plan = ep_desc or series_desc
        if not description_plan:
            stats["missing_description"] += 1
            continue
        if require_episode_description and not ep_desc:
            stats["missing_episode_description"] += 1
            continue

        manifest_row = {
            **episode,
            "candidates": [candidate],
            "upload_manifest": {
                "schema_version": 1,
                "source_plan": {
                    "prepared_at": plan.get("prepared_at"),
                    "tested_source_count": plan.get("tested_source_count"),
                    "selected_source_id": source_id,
                    "verdict": selected.get("verdict"),
                    "detected_by": selected.get("detected_by"),
                    "confidence": selected.get("confidence"),
                    "resolution_score": selected.get("resolution_score"),
                    "verification_status": selected.get("verification_status"),
                    "cz_audio_verified": selected.get("cz_audio_verified"),
                },
                "language_audit": {
                    "audited_at": audit.get("audited_at"),
                    "source_id": source_id,
                    "verdict": audit.get("verdict"),
                    "detected_by": audit.get("detected_by"),
                    "confidence": audit.get("confidence"),
                    "verification_status": audit.get("verification_status"),
                    "cz_audio_verified": audit.get("cz_audio_verified"),
                    "whisper": whisper,
                    "metadata_audio_lang": (audit.get("signals") or {}).get("metadata_audio_lang"),
                    "title_lang_class": (audit.get("signals") or {}).get("title_lang_class"),
                },
                "description": {
                    "kind": description_plan.get("kind"),
                    "generated_at": description_plan.get("generated_at"),
                    "model": description_plan.get("model"),
                    "source_hash": description_plan.get("source_hash"),
                    "text": description_plan.get("generated_description"),
                },
            },
        }
        rows.append(manifest_row)
        stats["ready"] += 1
        stats[f"description_{description_plan.get('kind')}"] += 1
        stats[f"language_{selected.get('detected_by') or 'unknown'}"] += 1

    return rows, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--prepared", default="plans/prepared-episodes.jsonl")
    ap.add_argument("--descriptions", default="plans/descriptions.jsonl")
    ap.add_argument("--audits", default="audits/language-audit-latest.jsonl")
    ap.add_argument("--out", default="manifests/upload-ready.jsonl.gz")
    ap.add_argument("--report", default="reports/upload-manifest.json")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--require-episode-description", action="store_true")
    ap.add_argument("--require-whisper", action="store_true")
    args = ap.parse_args()

    rows, stats = build_manifest(
        backlog_path=REPO_ROOT / args.backlog,
        prepared_path=REPO_ROOT / args.prepared,
        descriptions_path=REPO_ROOT / args.descriptions,
        audits_path=REPO_ROOT / args.audits,
        limit=args.limit,
        require_episode_description=args.require_episode_description,
        require_whisper=args.require_whisper,
    )
    write_jsonl(REPO_ROOT / args.out, rows)
    report = {"count": len(rows), "stats": dict(stats)}
    report_path = REPO_ROOT / args.report
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
