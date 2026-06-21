#!/usr/bin/env python3
"""Prepare original Gemma/Gemini descriptions before upload."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from description_quality import is_valid_generated_description

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemma-4-31b-it")
DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_THINKING_BUDGET = os.environ.get("GEMINI_THINKING_BUDGET", "0")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def api_keys() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    keys = [key.strip() for key in raw.replace("\n", ",").split(",") if key.strip()]
    if not keys:
        raise RuntimeError("GEMINI_API_KEY or GEMINI_API_KEYS is required")
    return keys


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def existing_ok(path: Path, *, replace_fallback: bool = False) -> set[tuple[str, int, str]]:
    out: set[tuple[str, int, str]] = set()
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ok":
                continue
            if replace_fallback and row.get("model") == "fallback-template-v1":
                continue
            if replace_fallback and not is_valid_generated_description(row.get("generated_description") or ""):
                continue
            kind = row.get("kind")
            entity_id = row.get("episode_id") if kind == "episode" else row.get("series_id")
            if kind and entity_id and row.get("source_hash"):
                out.add((kind, int(entity_id), row["source_hash"]))
    return out


def build_tasks(backlog: list[dict], done: set[tuple[str, int, str]], *, series_limit: int, episode_limit: int) -> list[dict]:
    tasks: list[dict] = []
    by_series: dict[int, dict] = {}
    for row in backlog:
        by_series.setdefault(int(row["series_id"]), row)

    for row in list(by_series.values())[:series_limit]:
        source = row.get("series_description") or row.get("series_overview_en") or row.get("source_description") or ""
        if not source.strip():
            continue
        h = source_hash(source)
        if ("series", int(row["series_id"]), h) in done:
            continue
        tasks.append({
            "kind": "series",
            "series_id": row["series_id"],
            "series_slug": row["series_slug"],
            "series_title": row["series_title"],
            "title": row["series_title"],
            "source_description": source,
            "source_hash": h,
        })

    for row in backlog[:episode_limit]:
        source = row.get("source_description") or row.get("description") or row.get("series_description") or ""
        if not source.strip():
            continue
        h = source_hash(source)
        if ("episode", int(row["episode_id"]), h) in done:
            continue
        title = f"{row['series_title']} {row['episode_code']}"
        if row.get("episode_name"):
            title += f" - {row['episode_name']}"
        tasks.append({
            "kind": "episode",
            "series_id": row["series_id"],
            "series_slug": row["series_slug"],
            "series_title": row["series_title"],
            "episode_id": row["episode_id"],
            "season": row["season"],
            "episode": row["episode"],
            "episode_code": row["episode_code"],
            "episode_name": row.get("episode_name"),
            "title": title,
            "source_description": source,
            "source_hash": h,
        })
    return tasks


def prompt_for(task: dict) -> str:
    title = task["title"]
    source = task["source_description"]
    if task["kind"] == "series":
        return (
            "Napiš finální popis seriálu pro videohosting.\n"
            "Výstup musí být jen samotný český popis v odstavci.\n"
            "Zakázáno: rozbor, odrážky, seznam, kontrola, varianty, Draft, Task, Constraints, vysvětlení, angličtina.\n"
            "Neopisuj zdrojové formulace, ale zachovej fakta a neprozrazuj zásadní zvraty.\n"
            "Délka: 3 až 5 vět. Začni rovnou první větou popisu.\n\n"
            f"Název seriálu: {title}\n"
            f"Zdrojový obsah:\n{source}\n\n"
            "Vrať pouze finální popis:"
        )
    return (
        "Napiš finální popis seriálové epizody pro videohosting.\n"
        "Výstup musí být jen samotný český popis v odstavci.\n"
        "Zakázáno: rozbor, odrážky, seznam, kontrola, varianty, Draft, Task, Constraints, vysvětlení, angličtina.\n"
        "Neopisuj zdrojové formulace, ale zachovej fakta a neprozrazuj pointu.\n"
        "Délka: 2 až 4 věty. Nepoužívej frázi 'v této epizodě uvidíte'. Začni rovnou první větou popisu.\n\n"
        f"Název epizody: {title}\n"
        f"Zdrojový obsah:\n{source}\n\n"
        "Vrať pouze finální popis:"
    )


def fallback_description(task: dict) -> str:
    if task["kind"] == "series":
        return (
            f"{task['title']} je seriál, který postupně rozvíjí osudy hlavních postav a jejich vzájemné vztahy. "
            "Příběh staví na napětí, charakterech a situacích, které se proměňují s každou další epizodou. "
            "Popis je připraven tak, aby nepřebíral původní text a neprozrazoval zásadní zvraty."
        )
    return (
        f"{task['title']} pokračuje v ději seriálu a soustředí se na další vývoj postav i jejich rozhodnutí. "
        "Epizoda zapadá do širšího příběhu série a drží prostor pro napětí bez prozrazení hlavních zvratů. "
        "Text je připraven jako dočasný originální popis pro upload."
    )


def generation_config(model: str) -> dict:
    config: dict = {
        "temperature": 0.25,
        "topP": 0.7,
        "maxOutputTokens": 180,
        "stopSequences": ["\n*", "\n-", "\nDraft", "\nTask", "\nConstraints"],
    }
    if DEFAULT_THINKING_BUDGET.strip() and not model.startswith("gemma-"):
        config["thinkingConfig"] = {"thinkingBudget": int(DEFAULT_THINKING_BUDGET)}
    return config


def response_error(resp: requests.Response) -> tuple[str, int | None, float | None, dict[str, str]]:
    headers = {
        key: value
        for key, value in resp.headers.items()
        if key.lower() in {"retry-after", "date", "server"}
        or key.lower().startswith("x-ratelimit")
        or key.lower().startswith("x-goog")
    }
    retry_after = None
    if resp.headers.get("Retry-After"):
        try:
            retry_after = float(resp.headers["Retry-After"])
        except ValueError:
            retry_after = None
    try:
        body = resp.json()
    except ValueError:
        text = resp.text[:500]
        match = re.search(r"retry in ([0-9.]+)s", text, flags=re.IGNORECASE)
        if match:
            retry_after = float(match.group(1))
        return text, None, retry_after, headers

    error = body.get("error") or {}
    message = str(error.get("message") or resp.text[:500])
    match = re.search(r"retry in ([0-9.]+)s", message, flags=re.IGNORECASE)
    if match:
        retry_after = float(match.group(1))
    return message[:500], int(error["code"]) if error.get("code") else None, retry_after, headers


def generate(task: dict, key: str, model: str, *, retries: int = 3, fallback_on_error: bool = False) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": prompt_for(task)}]}],
        "generationConfig": generation_config(model),
    }
    last_error_status = None
    last_error_headers: dict[str, str] = {}
    last_retry_after = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not is_valid_generated_description(text):
                raise ValueError("Generated description failed validation")
            return {**task, "status": "ok", "model": model, "generated_at": now_iso(), "generated_description": text}
        except Exception as exc:
            error_text = str(exc)
            retry_after = None
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                error_message, status_code, retry_after, headers = response_error(exc.response)
                last_error_status = status_code or exc.response.status_code
                last_error_headers = headers
                last_retry_after = retry_after
                error_text = f"HTTP {exc.response.status_code}: {error_message}"
            if attempt == retries - 1:
                if fallback_on_error:
                    return {
                        **task,
                        "status": "ok",
                        "model": "fallback-template-v1",
                        "generated_at": now_iso(),
                        "generated_description": fallback_description(task),
                        "fallback_reason": type(exc).__name__,
                        "fallback_error": error_text[:500],
                    }
                return {
                    **task,
                    "status": "error",
                    "model": model,
                    "generated_at": now_iso(),
                    "error": type(exc).__name__,
                    "error_message": error_text[:500],
                    "error_status_code": last_error_status,
                    "error_retry_after_seconds": last_retry_after,
                    "error_headers": last_error_headers,
                }
            time.sleep(max(DEFAULT_RETRY_SECONDS + attempt * 3, retry_after or 0))
    raise AssertionError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--out", default="plans/descriptions.jsonl")
    ap.add_argument("--series-limit", type=int, default=8)
    ap.add_argument("--episode-limit", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--fallback-on-error", action="store_true")
    ap.add_argument(
        "--replace-fallback",
        action="store_true",
        help="Treat temporary fallback descriptions as missing so later runs can replace them with model output.",
    )
    args = ap.parse_args()

    rows = load_jsonl(Path(args.backlog))
    done = existing_ok(Path(args.out), replace_fallback=args.replace_fallback)
    tasks = build_tasks(rows, done, series_limit=args.series_limit, episode_limit=args.episode_limit)
    keys = api_keys()
    results: list[dict] = []
    def task(index_task: tuple[int, dict]) -> dict:
        index, task_row = index_task
        key_index = index % len(keys)
        result = generate(
            task_row,
            keys[key_index],
            args.model,
            retries=args.retries,
            fallback_on_error=args.fallback_on_error,
        )
        result["key_slot"] = key_index + 1
        result["key_count"] = len(keys)
        return result

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [
            ex.submit(task, item)
            for item in enumerate(tasks)
        ]
        for fut in as_completed(futures):
            row = fut.result()
            results.append(row)
            ident = row.get("episode_code") or row.get("series_slug")
            suffix = f" {row.get('error') or row.get('fallback_reason')}" if row.get("error") or row.get("fallback_reason") else ""
            key_suffix = f" key_slot={row.get('key_slot')}/{row.get('key_count')}"
            print(f"{row['status']} {row['kind']} {ident}{key_suffix}{suffix}", file=sys.stderr)
    append_jsonl(Path(args.out), results)
    ok = sum(1 for row in results if row.get("status") == "ok")
    print(f"Prepared descriptions: ok={ok} total={len(results)} out={args.out}")
    return 0 if ok > 0 or not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
