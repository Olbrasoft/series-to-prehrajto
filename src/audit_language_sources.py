#!/usr/bin/env python3
"""Audit source language signals and persist import-ready evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from language_checks import CZECH_AUDIO_LANGS, title_language_hint, whisper_language  # noqa: E402
from resolve_stream import ResolveError, pick_best, resolve as resolve_prehrajto  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

CZ_DUB_RE = re.compile(
    r"(?:\bcz[\s\-_]*dab(?:ing)?\b|\bczdab\w*|\bczdub\w*|\bcesk[aáyý][\s\-_]*dab(?:ing)?\b|\bc[zs][\s\-_]*dabing\b|cesky[\s\-_]*dabing|cz[\s\-_]*\.dab\b)",
    re.IGNORECASE,
)
CZ_SUB_RE = re.compile(
    r"(?:\bcz[\s\-_]*tit(?:ulky)?\b|\bcztit\w*|\bcz[\s\-_]*subs?\b|\bc[zs][\s\-_]*titulky\b|cesk[yé][\s\-_]*titulky)",
    re.IGNORECASE,
)
SK_DUB_RE = re.compile(r"(?:\bsk[\s\-_]*dab(?:ing)?\b|\bskdab\w*|\bskdub\w*)", re.IGNORECASE)
SK_SUB_RE = re.compile(r"(?:\bsk[\s\-_]*tit(?:ulky)?\b|\bsktit\w*)", re.IGNORECASE)


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


def title_lang_class(title: str | None) -> str:
    if not title:
        return "UNKNOWN"
    if CZ_DUB_RE.search(title):
        return "CZ_DUB"
    if SK_DUB_RE.search(title):
        return "SK_DUB"
    if CZ_SUB_RE.search(title):
        return "CZ_SUB"
    if SK_SUB_RE.search(title):
        return "SK_SUB"
    return "UNKNOWN"


def metadata_audio_lang(item: dict) -> tuple[str | None, float | None, str | None]:
    audio = (item.get("db_audio_lang") or "").strip().lower()
    if audio:
        return audio, item.get("db_audio_confidence"), item.get("db_audio_detected_by") or "db"
    metadata = item.get("metadata") or {}
    for key in ("audio_lang", "language", "lang"):
        value = (metadata.get(key) or "").strip().lower() if isinstance(metadata.get(key), str) else ""
        if value:
            return value, None, f"metadata.{key}"
    return None, None, None


def verdict_from_signals(title_class: str, audio_lang: str | None, whisper_lang: str | None) -> tuple[str, str, float]:
    if whisper_lang:
        if whisper_lang in {"cs", "cz", "ces", "cze"}:
            return "CZ_AUDIO", "whisper", 0.95
        if whisper_lang == "sk":
            return "SK_AUDIO", "whisper", 0.95
        return "NOT_CZ_AUDIO", "whisper", 0.95
    if audio_lang:
        if audio_lang in CZECH_AUDIO_LANGS:
            return "CZ_AUDIO", "metadata", 0.85
        if audio_lang == "sk":
            return "SK_AUDIO", "metadata", 0.85
        return "NOT_CZ_AUDIO", "metadata", 0.80
    if title_class in {"CZ_DUB", "CZ_NATIVE"}:
        return "PROBABLE_CZ_AUDIO", "title", 0.55
    if title_class == "CZ_SUB":
        return "CZ_SUBTITLES_ONLY", "title", 0.50
    return "UNKNOWN", "none", 0.0


def sample_audio(stream_url: str, out_path: Path, *, start_sec: int, seconds: int) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start_sec),
        "-t",
        str(seconds),
        "-headers",
        "Referer: https://prehraj.to/\r\n",
        "-i",
        stream_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return False, proc.stderr.strip()[:300]
    return True, "ok"


def audit_stream_with_whisper(item: dict, *, sample_seconds: int) -> dict:
    if os.environ.get("WHISPER_LANGUAGE_CHECK") != "1":
        return {"status": "disabled"}
    if item.get("provider") != "prehrajto" or not item.get("source_url"):
        return {"status": "unsupported_provider"}
    try:
        resolved = resolve_prehrajto(item["source_url"], max_retries=1, backoff_seconds=(3,))
        best = pick_best(resolved.videos, prefer=(1080, 720))
    except ResolveError as exc:
        return {"status": "resolve_failed", "error": str(exc), "permanent": exc.permanent}
    except Exception as exc:
        return {"status": "resolve_crashed", "error": f"{type(exc).__name__}: {exc}"}
    with tempfile.TemporaryDirectory() as td:
        sample = Path(td) / "sample.wav"
        ok, msg = sample_audio(best.url, sample, start_sec=180, seconds=sample_seconds)
        if not ok:
            return {"status": "sample_failed", "error": msg, "resolution": best.label}
        lang, prob, status = whisper_language(sample, seconds=sample_seconds)
        return {"status": status, "language": lang, "probability": prob, "resolution": best.label}


def audit_one(item: dict, *, use_whisper: bool, sample_seconds: int) -> dict:
    title_class = title_lang_class(item.get("source_title") or "")
    title_hint = title_language_hint(item.get("source_title") or "")
    audio_lang, audio_conf, audio_by = metadata_audio_lang(item)
    whisper = {"status": "disabled"}
    old = os.environ.get("WHISPER_LANGUAGE_CHECK")
    if use_whisper:
        os.environ["WHISPER_LANGUAGE_CHECK"] = "1"
        whisper = audit_stream_with_whisper(item, sample_seconds=sample_seconds)
    if old is None:
        os.environ.pop("WHISPER_LANGUAGE_CHECK", None)
    else:
        os.environ["WHISPER_LANGUAGE_CHECK"] = old

    whisper_lang = whisper.get("language") if whisper.get("status") == "ok" else None
    verdict, detected_by, confidence = verdict_from_signals(title_class, audio_lang, whisper_lang)
    mismatch = bool(title_class.startswith("CZ") and verdict == "NOT_CZ_AUDIO")
    if item.get("db_lang_class") in {"CZ_DUB", "CZ_NATIVE"} and verdict == "NOT_CZ_AUDIO":
        mismatch = True
    unresolved = verdict == "UNKNOWN" and (
        item.get("db_lang_class") in {None, "UNKNOWN"}
        or not item.get("db_audio_lang")
        or not item.get("source_title")
        or whisper.get("status") in {"unsupported_provider", "resolve_failed", "sample_failed"}
    )

    return {
        "audited_at": now_iso(),
        "series_id": item["series_id"],
        "series_slug": item["series_slug"],
        "series_title": item["series_title"],
        "episode_id": item["episode_id"],
        "season": item["season"],
        "episode": item["episode"],
        "episode_name": item.get("episode_name") or item.get("episode_title"),
        "provider": item["provider"],
        "source_id": item["source_id"],
        "external_id": item.get("external_id"),
        "source_url": item.get("source_url"),
        "source_title": item.get("source_title"),
        "db_lang_class": item.get("db_lang_class"),
        "db_audio_lang": item.get("db_audio_lang"),
        "signals": {
            "title_lang_class": title_class,
            "title_hint": title_hint,
            "metadata_audio_lang": audio_lang,
            "metadata_audio_confidence": audio_conf,
            "metadata_audio_detected_by": audio_by,
            "whisper": whisper,
        },
        "verdict": verdict,
        "detected_by": detected_by,
        "confidence": confidence,
        "needs_db_update": (
            verdict != "UNKNOWN"
            and (
                item.get("db_lang_class") in {None, "UNKNOWN"}
                or item.get("db_audio_lang") != ("cs" if verdict in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"} else item.get("db_audio_lang"))
                or mismatch
            )
        ),
        "needs_manual_resolution": unresolved,
        "mismatch": mismatch,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="backlog/language-audit-queue.jsonl.gz")
    ap.add_argument("--out", default="audits/language-audit.jsonl")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--source-id", type=int, action="append")
    ap.add_argument("--series-slug")
    ap.add_argument("--use-whisper", action="store_true")
    ap.add_argument("--sample-seconds", type=int, default=45)
    args = ap.parse_args()

    rows = load_jsonl(Path(args.queue))
    if args.source_id:
        wanted = set(args.source_id)
        rows = [row for row in rows if int(row["source_id"]) in wanted]
    if args.series_slug:
        rows = [row for row in rows if row["series_slug"] == args.series_slug]
    rows = rows[: args.limit]
    results = [audit_one(row, use_whisper=args.use_whisper, sample_seconds=args.sample_seconds) for row in rows]
    append_jsonl(Path(args.out), results)
    for result in results:
        print(
            f"{result['source_id']} {result['series_title']} "
            f"S{int(result['season']):02d}E{int(result['episode']):02d} "
            f"{result['verdict']} by={result['detected_by']} update={result['needs_db_update']}"
        )
    print(f"Appended {len(results)} audit rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
