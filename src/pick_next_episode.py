#!/usr/bin/env python3
"""Pick the next not-yet-uploaded series episode from backlog."""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKLOG = REPO_ROOT / "backlog" / "series-episodes.jsonl.gz"
DEFAULT_MANIFEST = REPO_ROOT / "manifests" / "upload-ready.jsonl.gz"


def default_backlog_path() -> Path:
    env_path = os.environ.get("UPLOAD_BACKLOG_PATH")
    if env_path:
        return Path(env_path)
    if DEFAULT_MANIFEST.exists():
        return DEFAULT_MANIFEST
    return DEFAULT_BACKLOG


BACKLOG = default_backlog_path()

NUM_SHARDS = int(os.environ.get("SYNC_NUM_SHARDS", "1"))
SHARD_ID = int(os.environ.get("SYNC_SHARD_ID", "0"))


def state_path(shard_id: int | None = None) -> Path:
    sid = shard_id if shard_id is not None else SHARD_ID
    if NUM_SHARDS <= 1:
        return REPO_ROOT / "state" / "uploaded.json"
    return REPO_ROOT / "state" / f"uploaded-shard-{sid}.json"


STATE = state_path()


def load_backlog(path: Path = BACKLOG) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_state(path: Path | None = None) -> dict:
    p = path or state_path()
    if not p.exists():
        return {"schema_version": 1, "uploads": [], "failed_attempts": []}
    return json.loads(p.read_text())


def uploaded_episode_ids(state: dict) -> set[int]:
    return {int(u["episode_id"]) for u in state.get("uploads", [])}


def episode_key(row: dict) -> tuple[int, int, int]:
    return (int(row["series_id"]), int(row["season"]), int(row["episode"]))


def uploaded_episode_keys(state: dict) -> set[tuple[int, int, int]]:
    return {episode_key(u) for u in state.get("uploads", [])}


def burned_source_ids(state: dict) -> set[int]:
    return {int(a["source_id"]) for a in state.get("failed_attempts", []) if a.get("permanent")}


def pick_next(state: dict, rows: list[dict], extra_exclude: set[int] | None = None) -> dict | None:
    done = uploaded_episode_ids(state)
    done_keys = uploaded_episode_keys(state)
    burned = burned_source_ids(state)
    extras = extra_exclude or set()
    for item in rows:
        episode_id = int(item["episode_id"])
        if NUM_SHARDS > 1 and episode_id % NUM_SHARDS != SHARD_ID:
            continue
        if episode_id in done or episode_id in extras:
            continue
        if episode_key(item) in done_keys:
            continue
        candidates = [c for c in item["candidates"] if int(c["source_id"]) not in burned]
        if not candidates:
            continue
        return {**item, "candidates": candidates}
    return None


def main() -> int:
    if not BACKLOG.is_file():
        print(f"ERROR: backlog missing: {BACKLOG}", file=sys.stderr)
        return 2
    state = load_state()
    rows = load_backlog()
    pick = pick_next(state, rows)
    print(f"state uploads={len(state.get('uploads', []))} failed={len(state.get('failed_attempts', []))}")
    print(f"backlog episodes={len(rows)}")
    if not pick:
        print("backlog exhausted")
        return 1
    print(f"episode_id={pick['episode_id']}")
    print(f"display_name={pick['display_name']!r}")
    print(f"candidates={len(pick['candidates'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
