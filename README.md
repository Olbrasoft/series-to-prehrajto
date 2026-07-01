# series-to-prehrajto

Uploads selected series episodes from the CR production catalog to the
`serialy.prehrajto@seznam.cz` Přehraj.to account.

For the current production architecture, persisted state, health checks and
handoff instructions, read [`docs/operations-handoff.md`](docs/operations-handoff.md)
first. The rest of this README describes the original minimal workflow.

The first version is deliberately small and operational:

- export a limited backlog of top series episodes from the production DB,
- prefer Přehraj.to source candidates with Czech audio and 1080p or better,
- try candidates one by one until an episode uploads,
- store progress in `state/uploaded.json` after every successful upload,
- run from GitHub Actions on schedule or by manual dispatch.

## Layout

```text
backlog/series-episodes.jsonl.gz  exported episode backlog
src/export_backlog.py             read-only production DB export
src/pick_next_episode.py          backlog minus uploaded state
src/sync_batch.py                 resolve, download, language check, upload
src/prehrajto_upload.py           direct Přehraj.to multipart upload
src/resolve_stream.py             Přehraj.to detail page to playable MP4
state/uploaded.json               persistent upload state
.github/workflows/sync.yml        GitHub Actions runner
```

## Export

Use a read-only connection string. The production tunnel is expected to set
`default_transaction_read_only=on`.

```bash
DATABASE_URL='postgres://cr:***@127.0.0.1:15432/cr?options=-c default_transaction_read_only=on' \
  python src/export_backlog.py \
  --out backlog/series-episodes.jsonl.gz \
  --series-limit 8 \
  --episode-limit 80 \
  --source-limit-per-episode 8
```

## Upload

```bash
PREHRAJTO_EMAIL='serialy.prehrajto@seznam.cz' \
PREHRAJTO_PASSWORD='***' \
CZ_PROXY_URL='***' \
CZ_PROXY_KEY='***' \
python src/sync_batch.py --count 5
```

Set `WHISPER_LANGUAGE_CHECK=1` to verify a downloaded sample with Whisper
before upload. The default is faster startup: accept Czech audio when the DB
or title identifies it as Czech.

## GitHub Secrets

Required repository secrets:

- `PREHRAJTO_EMAIL`
- `PREHRAJTO_PASSWORD`
- `CZ_PROXY_URL`
- `CZ_PROXY_KEY`

Optional repository variable:

- `WHISPER_LANGUAGE_CHECK=1`

## Notes

Episode display names always include the series title and `SxxExx`, for example
`Teorie velkého třesku S02E17 - Setkání s Terminátorem CZ Dabing`.

Descriptions use the episode description when available, otherwise the series
description. `src/rewrite_descriptions.py` can rewrite backlog descriptions with
Gemini API keys, but uploads do not wait for that step.
