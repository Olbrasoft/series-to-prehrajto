#!/usr/bin/env python3
"""Summarize operational status for uploads, descriptions and source prep."""

from __future__ import annotations

import gzip
import json
import subprocess
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def gh_runs() -> list[dict]:
    try:
        out = subprocess.check_output(
            [
                "gh", "run", "list",
                "--repo", "Olbrasoft/series-to-prehrajto",
                "--limit", "20",
                "--json", "databaseId,workflowName,status,conclusion,createdAt,headBranch",
            ],
            text=True,
        )
        return json.loads(out)
    except Exception:
        return []


def latest_descriptions(rows: list[dict]) -> tuple[set[int], set[int]]:
    series: set[int] = set()
    episodes: set[int] = set()
    for row in rows:
        if row.get("status") != "ok":
            continue
        if row.get("kind") == "series":
            series.add(int(row["series_id"]))
        elif row.get("kind") == "episode":
            episodes.add(int(row["episode_id"]))
    return series, episodes


def main() -> int:
    backlog = load_jsonl(REPO / "backlog" / "series-episodes.jsonl.gz")
    state = load_json(REPO / "state" / "uploaded.json")
    descriptions = load_jsonl(REPO / "plans" / "descriptions.jsonl")
    prepared = load_jsonl(REPO / "plans" / "prepared-episodes.jsonl")
    audits = load_jsonl(REPO / "audits" / "language-audit.jsonl")
    desc_series, desc_episodes = latest_descriptions(descriptions)
    uploaded = state.get("uploads", [])
    uploaded_episode_ids = {int(row["episode_id"]) for row in uploaded}
    backlog_episode_ids = {int(row["episode_id"]) for row in backlog}
    prepared_episode_ids = {int(row["episode_id"]) for row in prepared}
    uploaded_missing_desc = [
        row for row in uploaded
        if int(row["episode_id"]) not in desc_episodes and int(row["series_id"]) not in desc_series
    ]
    not_ready = [row for row in prepared if not row.get("upload_ready")]
    report = {
        "counts": {
            "backlog_episodes": len(backlog_episode_ids),
            "uploaded_episodes": len(uploaded_episode_ids),
            "prepared_source_episodes": len(prepared_episode_ids),
            "description_series": len(desc_series),
            "description_episodes": len(desc_episodes),
            "language_audit_rows": len(audits),
        },
        "workflow_runs": gh_runs(),
        "gaps": {
            "backlog_without_source_plan": sorted(backlog_episode_ids - prepared_episode_ids)[:50],
            "backlog_without_episode_description": sorted(backlog_episode_ids - desc_episodes)[:50],
            "uploaded_without_gemma_description": [
                {"episode_id": row["episode_id"], "display_name": row["display_name"], "video_id": row["prehrajto_video_id"]}
                for row in uploaded_missing_desc
            ],
            "prepared_not_upload_ready": [
                {"episode_id": row["episode_id"], "series_title": row["series_title"], "season": row["season"], "episode": row["episode"]}
                for row in not_ready[:50]
            ],
        },
        "language_verdicts": dict(Counter(row.get("verdict", "UNKNOWN") for row in audits)),
    }
    out = REPO / "reports" / "ops-status.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    print(f"uploaded_without_gemma_description={len(uploaded_missing_desc)}")
    print(f"backlog_without_source_plan={len(backlog_episode_ids - prepared_episode_ids)}")
    print(f"backlog_without_episode_description={len(backlog_episode_ids - desc_episodes)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
