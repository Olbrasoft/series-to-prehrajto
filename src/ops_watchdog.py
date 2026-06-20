#!/usr/bin/env python3
"""Keep upload and preparation workflows running based on repo state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_status import main as build_status  # noqa: E402
from upload_queue_status import upload_ready_rows  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "reports" / "ops-status.json"

RUNNING = {"queued", "in_progress", "waiting", "pending", "requested"}


def run_gh(args: list[str], *, dry_run: bool) -> None:
    print("+ gh " + " ".join(args), flush=True)
    if dry_run:
        return
    subprocess.run(["gh", *args], check=True)


def active_workflows(report: dict) -> set[str]:
    active: set[str] = set()
    for row in report.get("workflow_runs") or []:
        if row.get("status") in RUNNING:
            active.add(str(row.get("workflowName")))
    return active


def load_report() -> dict:
    if not REPORT.exists():
        return {}
    return json.loads(REPORT.read_text(encoding="utf-8"))


def queue_workflow(workflow: str, fields: dict[str, str], *, active: set[str], dry_run: bool) -> bool:
    if workflow in active:
        print(f"{workflow}: already active")
        return False
    args = ["workflow", "run", f"{workflow}.yml"]
    for key, value in fields.items():
        args.extend(["-f", f"{key}={value}"])
    run_gh(args, dry_run=dry_run)
    active.add(workflow)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-upload-ready", type=int, default=200)
    ap.add_argument("--target-episodes", type=int, default=1000)
    ap.add_argument("--emergency-episodes", type=int, default=160)
    ap.add_argument("--target-series", type=int, default=80)
    ap.add_argument("--min-pending-whisper", type=int, default=100)
    ap.add_argument("--min-description-gap", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    build_status()
    report = load_report()
    counts = report.get("counts") or {}
    gaps = report.get("gaps") or {}
    active = active_workflows(report)
    upload_ready = len(upload_ready_rows())
    backlog_count = int(counts.get("backlog_episodes") or 0)
    manifest_ready = int(counts.get("manifest_upload_ready_episodes") or 0)
    pending_whisper = int(counts.get("language_pending_whisper_sources") or 0)
    description_gap = len(gaps.get("backlog_without_episode_description") or [])
    uploaded_needing_desc_update = len(gaps.get("uploaded_not_marked_description_updated") or [])

    print(
        json.dumps(
            {
                "active_workflows": sorted(active),
                "upload_ready_remaining": upload_ready,
                "backlog_episodes": backlog_count,
                "manifest_upload_ready_episodes": manifest_ready,
                "language_pending_whisper_sources": pending_whisper,
                "description_gap_sample": description_gap,
                "uploaded_needing_description_update": uploaded_needing_desc_update,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    prepare_episode_target = args.emergency_episodes if upload_ready == 0 else args.target_episodes
    prepare_series_target = min(args.target_series, 30) if upload_ready == 0 else args.target_series

    if upload_ready <= args.min_upload_ready or backlog_count == 0 or manifest_ready < args.target_episodes:
        queue_workflow(
            "prepare-manifest",
            {
                "series_limit": str(prepare_series_target),
                "episode_limit": str(prepare_episode_target),
                "source_limit_per_episode": "12",
                "use_whisper": "false",
            },
            active=active,
            dry_run=args.dry_run,
        )

    if upload_ready > 0:
        queue_workflow(
            "sync",
            {
                "batch_size": "20",
                "num_shards": "2",
                "allow_subtitles": "false",
                "continue_uploads": "true",
            },
            active=active,
            dry_run=args.dry_run,
        )

    if pending_whisper >= args.min_pending_whisper:
        queue_workflow(
            "audit-language",
            {"limit": "40", "sample_seconds": "45"},
            active=active,
            dry_run=args.dry_run,
        )

    if description_gap >= args.min_description_gap or manifest_ready < args.target_episodes:
        queue_workflow(
            "prepare-descriptions",
            {"series_limit": str(args.target_series), "episode_limit": str(args.target_episodes)},
            active=active,
            dry_run=args.dry_run,
        )

    if uploaded_needing_desc_update:
        queue_workflow(
            "update-descriptions",
            {"limit": "100"},
            active=active,
            dry_run=args.dry_run,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
