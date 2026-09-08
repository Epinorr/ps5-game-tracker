# PS5 Game Tracker

A lightweight, automated tracker for discovering newly added PS5 games and highlighting titles that are exclusive to PlayStation 5.

The project monitors the PS5 game directory published by **DLPSGames**, keeps a local history of discovered titles, enriches the data with game metadata, and presents everything through a simple static web interface.

## Overview

PS5 Game Tracker is designed around a simple idea: turn a constantly changing game list into a useful, browsable feed.

The tracker continuously compares the current DLPSGames PS5 list with the previously saved dataset. When a new game appears for the first time, the project records when it was discovered. This makes it possible to provide a **"New in the last 72 hours"** view even though the source website does not publish an official date for when each entry was added.

The frontend then turns that data into game cards with cover art, title, platform information, and a direct link to the game's page on DLPSGames.

## Features

- **New games — 72 hours**
  Shows titles first detected within the previous 72 hours.

- **PS5-only games**
  Highlights games identified as having no PS4 release.

- **Game covers and metadata**
  Uses IGDB metadata when available to provide cover artwork and platform information.

- **Direct game pages**
  Each game card links to its corresponding page on DLPSGames.

- **Automatic updates**
  GitHub Actions periodically checks the source list and updates the project's dataset automatically.

- **Static frontend**
  The website is built with plain HTML, CSS, and JavaScript and can be served through GitHub Pages without a separate backend.

- **Persistent discovery history**
  Detected games and their metadata are stored in `data/games.json`, allowing the project to keep track of what it has already seen.

## How it works

```text
DLPSGames PS5 List
        │
        ▼
   Game list scraper
        │
        ▼
Compare with games.json
        │
        ├── New title → record first_seen
        │
        ▼
   Metadata enrichment
        │
        ▼
     games.json
        │
        ▼
   Static web frontend
        │
        ▼
  Browse / Search / Filter
```

The updater extracts game names and their original page URLs from the source list. Existing entries are preserved, while newly discovered titles receive a `first_seen` timestamp. Metadata such as platform information and cover art can then be attached to each record.

The website reads the generated JSON directly and builds the interface in the browser, so no application server or database is required.
