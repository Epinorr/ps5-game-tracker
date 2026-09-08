# PS5 Game Tracker

PS5 Game Tracker is a small automated tracker that monitors the PS5 game list published by DLPSGames and turns it into a searchable, mobile-friendly catalog.

## What it does

The tracker watches the source list on a fixed schedule and records when each title is first observed. This powers the **Latest · 72h** view without relying on publication metadata that the source page does not provide.

Each game can also be enriched with metadata from IGDB, including cover art and platform information. Titles that have PS5 metadata and no PS4 release are marked **PS5 ONLY**.

Every card links directly to the corresponding game page on DLPSGames.

## Highlights

- Automatic source monitoring every 6 hours.
- Direct source fetching with Jina Reader fallback when the source blocks automated requests.
- Source URL preservation for direct game-page links.
- IGDB enrichment with batched requests and fuzzy title matching.
- Latest 72-hour, PS5-only, and all-games views.
- Search and client-side pagination for large datasets.
- English / Persian interface with remembered language preference.
- GitHub Actions handles testing, data updates, commits, and GitHub Pages deployment.
- Automatic warning issue when the fallback fetch path is required.
- Local tests for scraper behavior and title normalization.

## How the data is produced

```text
DLPSGames
   │
   ├── direct request
   │      └── 403 / failure → Jina Reader fallback
   │
   ▼
Game names + source URLs
   │
   ├── compare with data/games.json
   ├── assign first_seen to new discoveries
   └── enrich missing metadata through IGDB
   │
   ▼
data/games.json
   │
   ▼
GitHub Pages frontend
```

The source page does not expose a reliable “date added” field, so `first_seen` represents the first time this tracker observed a title. It is not a claim about when the game was actually added by the source.

## Reliability model

The updater intentionally fails when the detected list becomes implausibly small, instead of silently replacing a healthy dataset with bad or incomplete data. Large count swings are logged as warnings so source changes are visible in Actions logs.

When direct access to DLPSGames fails, the updater falls back to Jina Reader. A GitHub issue is opened once while that fallback remains active, making source-access problems visible without opening duplicate issues every run.

Metadata enrichment is best-effort: temporary IGDB failures do not remove metadata already saved in the dataset.

## Repository structure

```text
.
├── .github/
│   └── workflows/
│       └── update.yml
├── data/
│   └── games.json
├── scripts/
│   └── update_games.py
├── tests/
│   └── test_scraper.py
├── .gitignore
├── index.html
├── requirements.txt
└── README.md
```

## Main components

### `scripts/update_games.py`

The data pipeline. It fetches the source list, extracts game names and URLs, compares them with the existing dataset, enriches missing metadata through IGDB, and writes the normalized JSON dataset.

### `data/games.json`

The generated data consumed by the frontend. Records include the observed timestamp, source URL, platform information, optional IGDB identifiers, cover art, and the PS5-only flag.

### `index.html`

A static frontend served directly by GitHub Pages. It provides language switching, search, filters, pagination, lazy-loaded covers, and direct source links.

### `.github/workflows/update.yml`

The single automation pipeline. It runs every 6 hours (and manually on demand), executes the tests, updates the dataset, commits changes when needed, and deploys the current repository to GitHub Pages.

## Data shape

A typical record looks like this:

```json
{
  "name": "Returnal",
  "first_seen": "2026-09-05T00:00:00Z",
  "source_url": "https://dlpsgame.com/...",
  "igdb_id": 12345,
  "igdb_name": "Returnal",
  "cover_url": "https://images.igdb.com/...",
  "platforms": ["PlayStation 5", "PC"],
  "ps5_exclusive": true
}
```

## Automation requirements

The repository expects two GitHub Actions secrets for IGDB enrichment:

- `IGDB_CLIENT_ID`
- `IGDB_CLIENT_SECRET`

The workflow also uses GitHub's built-in `GITHUB_TOKEN` for repository commits and issue reporting. No application server or external database is required.

## Limitations

- The 72-hour view is based on observation time, not source publication time.
- PS5-only detection depends on IGDB platform metadata and therefore may be incomplete for ambiguous or missing records.
- Source-site HTML changes can still require scraper maintenance even with multiple extraction strategies and health checks.

## License

No license is included yet. Until one is added, the repository should be treated as **all rights reserved**.
