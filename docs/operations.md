# Operations

## First run

1. Refresh the backlog from the production DB with a read-only connection.
2. Commit the backlog and state file.
3. Set repository secrets for the Přehraj.to account and CZ proxy.
4. Run the `sync` workflow manually with a small batch size.
5. Watch `state/sync.log` and the Přehraj.to uploaded videos page.

## Candidate priority

Candidates are sorted by:

1. Czech audio classes: `CZ_DUB`, then `CZ_NATIVE`,
2. resolution hints: 2160p/4K, 1080p, 720p,
3. source view count,
4. stable source id.

`CZ_SUB` is kept in the backlog as fallback, but the uploader skips it unless
`--allow-subtitles` is used.

## Safety

The export reads only from production. Use a DSN with:

```text
options=-c default_transaction_read_only=on
```

The uploader writes only local repository state and the destination Přehraj.to
account.
