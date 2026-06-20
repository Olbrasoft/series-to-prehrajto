#!/usr/bin/env python3
"""Report upload-ready episodes using the same constraints as sync_batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pick_next_episode import load_backlog, load_state, uploaded_episode_ids, uploaded_episode_keys  # noqa: E402
from sync_batch import load_description_plans, load_source_plans  # noqa: E402


def episode_key(row: dict) -> tuple[int, int, int]:
    return (int(row["series_id"]), int(row["season"]), int(row["episode"]))


def upload_ready_rows(*, require_description: bool = True, require_source_plan: bool = True) -> list[dict]:
    rows = load_backlog()
    state = load_state()
    done_ids = uploaded_episode_ids(state)
    done_keys = uploaded_episode_keys(state)
    source_plans = load_source_plans()
    descriptions = load_description_plans()
    ready = []
    for row in rows:
        episode_id = int(row["episode_id"])
        if episode_id in done_ids or episode_key(row) in done_keys:
            continue
        if require_description:
            has_description = (
                episode_id in descriptions["episode"]
                or int(row["series_id"]) in descriptions["series"]
            )
            if not has_description:
                continue
        if require_source_plan:
            plan = source_plans.get(episode_id)
            if not plan or not plan.get("upload_ready") or not plan.get("selected_source"):
                continue
        ready.append(row)
    return ready


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-description", action="store_true", default=True)
    ap.add_argument("--no-require-description", action="store_false", dest="require_description")
    ap.add_argument("--require-source-plan", action="store_true", default=True)
    ap.add_argument("--no-require-source-plan", action="store_false", dest="require_source_plan")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ready = upload_ready_rows(
        require_description=args.require_description,
        require_source_plan=args.require_source_plan,
    )
    if args.json:
        print(json.dumps({"remaining_upload_ready": len(ready)}, ensure_ascii=False))
    else:
        print(f"remaining_upload_ready={len(ready)}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
