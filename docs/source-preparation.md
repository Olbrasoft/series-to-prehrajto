# Source Preparation

Before uploading an episode, prepare its source choices:

1. Walk series, seasons, episodes in order.
2. Check every available source candidate for that episode.
3. Record language evidence from title, DB metadata, provider page tracks, and
   optionally Whisper.
4. Pick one best source for upload, preferring Czech audio and 1080p+.
5. Store both the selected source and rejected alternatives in the repository.

Output:

```text
plans/prepared-episodes.jsonl
```

Each line is one episode plan with:

- `selected_source`: best source and evidence,
- `tested_sources`: every checked source,
- `upload_ready`: true only when the selected source has Czech audio evidence.

Run locally or from GitHub Actions:

```bash
python src/prepare_episode_sources.py --episode-limit 10
```

For stricter language confirmation:

```bash
WHISPER_LANGUAGE_CHECK=1 python src/prepare_episode_sources.py --use-whisper --episode-limit 3
```
