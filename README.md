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

## Limitations

- The 72-hour view is based on observation time, not source publication time.
- PS5-only detection depends on IGDB platform metadata and therefore may be incomplete for ambiguous or missing records.
- Source-site HTML changes can still require scraper maintenance even with multiple extraction strategies and health checks.
