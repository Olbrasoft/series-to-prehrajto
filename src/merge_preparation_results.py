#!/usr/bin/env python3
"""Merge prepared episode artifacts into the current checkout.

GitHub Actions can run multiple preparation jobs against different base commits.
This helper applies a job's prepared artifacts onto the latest origin/main
checkout without replacing newer rows from another job.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Callable, Iterable


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def episode_sort_key(row: dict) -> tuple:
    return (
        str(row.get("series_slug") or ""),
        int(row.get("season") or 0),
        int(row.get("episode") or 0),
        int(row.get("episode_id") or 0),
    )


def merge_by_key(
    current_path: Path,
    incoming_path: Path,
    *,
    key_fn: Callable[[dict], tuple | None],
    sort_fn: Callable[[dict], tuple] | None = None,
) -> int:
    current = load_jsonl(current_path)
    incoming = load_jsonl(incoming_path)
    rows: dict[tuple, dict] = {}
    for row in current:
        key = key_fn(row)
        if key is not None:
            rows[key] = row
    changed = 0
    for row in incoming:
        key = key_fn(row)
        if key is None:
            continue
        old = rows.get(key)
        if old != row:
            changed += 1
        rows[key] = row
    values = list(rows.values())
    if sort_fn:
        values.sort(key=sort_fn)
    write_jsonl(current_path, values)
    return changed


def prepared_key(row: dict) -> tuple | None:
    if row.get("episode_id") is None:
        return None
    return (int(row["episode_id"]),)


def subtitle_key(row: dict) -> tuple | None:
    if row.get("episode_id") is None:
        return None
    return (int(row["episode_id"]),)


def whisper_key(row: dict) -> tuple | None:
    if row.get("episode_id") is None or row.get("source_id") is None:
        return None
    return (int(row["episode_id"]), int(row["source_id"]))


def audit_key(row: dict) -> tuple | None:
    if row.get("source_id") is None:
        return None
    return (int(row["source_id"]),)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incoming-dir", required=True)
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()

    repo = Path(args.repo)
    incoming = Path(args.incoming_dir)
    changes = {
        "prepared": merge_by_key(
            repo / "plans" / "prepared-episodes.jsonl",
            incoming / "plans" / "prepared-episodes.jsonl",
            key_fn=prepared_key,
            sort_fn=episode_sort_key,
        ),
        "subtitle_followup": merge_by_key(
            repo / "plans" / "subtitle-followup-queue.jsonl",
            incoming / "plans" / "subtitle-followup-queue.jsonl",
            key_fn=subtitle_key,
            sort_fn=episode_sort_key,
        ),
        "whisper_review": merge_by_key(
            repo / "plans" / "whisper-review-queue.jsonl",
            incoming / "plans" / "whisper-review-queue.jsonl",
            key_fn=whisper_key,
            sort_fn=lambda row: (
                str(row.get("series_slug") or ""),
                int(row.get("season") or 0),
                int(row.get("episode") or 0),
                -int(row.get("filesize_bytes") or 0),
                int(row.get("source_id") or 0),
            ),
        ),
        "audits": merge_by_key(
            repo / "audits" / "language-audit-latest.jsonl.gz",
            incoming / "audits" / "language-audit-latest.jsonl.gz",
            key_fn=audit_key,
            sort_fn=lambda row: (int(row.get("source_id") or 0),),
        ),
    }
    print(json.dumps(changes, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
