#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio, token_set_ratio, WRatio

SOURCE_URL = os.getenv("SOURCE_URL", "https://dlpsgame.com/list-game-ps5/")
JINA_URL = f"https://r.jina.ai/{SOURCE_URL}"
DATA_PATH = Path(os.getenv("DATA_PATH", "data/games.json"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
IGDB_BATCH_SIZE = max(1, min(int(os.getenv("IGDB_BATCH_SIZE", "10")), 10))
IGDB_SLEEP = float(os.getenv("IGDB_SLEEP", "0.3"))
MIN_EXPECTED_GAMES = int(os.getenv("MIN_EXPECTED_GAMES", "50"))
MAX_COUNT_SWING = float(os.getenv("MAX_COUNT_SWING", "0.30"))

LOG = logging.getLogger("ps5-tracker")
SESSION = requests.Session()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_name(value: str) -> str:
    value = value or ""
    value = re.sub(r"\[\s*PS[45]\s*\]", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    url = url.split("#", 1)[0]
    return url.rstrip("/")


def load_data() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("games.json must contain a JSON array")
        return [x for x in raw if isinstance(x, dict) and x.get("name")]
    except Exception as exc:
        raise RuntimeError(f"Could not read {DATA_PATH}: {exc}") from exc


def save_data(games: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(games, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _request_source(url: str) -> requests.Response:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PS5-Game-Tracker/2.0; +https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    }
    return SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)


def fetch_source() -> tuple[str, bool]:
    try:
        resp = _request_source(SOURCE_URL)
        resp.raise_for_status()
        if len(resp.text) < 1000:
            raise RuntimeError("Source page returned unexpectedly little content")
        LOG.info("Source fetched directly")
        return resp.text, False
    except Exception as exc:
        LOG.warning("Direct source request failed: %s", exc)

    resp = _request_source(JINA_URL)
    resp.raise_for_status()
    if len(resp.text) < 1000:
        raise RuntimeError("Jina Reader returned unexpectedly little content")
    LOG.info("Source fetched via jina")
    report_jina_fallback()
    return resp.text, True


def _add_candidate(candidates: list[dict[str, str]], name: str, href: str) -> None:
    name = normalize_name(name)
    href = canonical_url(href)
    if not name or not href:
        return
    candidates.append({"name": name, "url": href})


def extract_games(text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    bad_names = {
        "next", "previous", "home", "search", "contact", "privacy policy",
        "terms and conditions", "login", "register", "menu", "facebook",
        "twitter", "instagram", "youtube", "read more", "continue reading",
    }

    soup = BeautifulSoup(text, "html.parser")
    selectors = [
        ".entry-content a", ".post-content a", ".page-content a",
        "article a", "ol li a", "ul li a", "main a",
    ]
    for selector in selectors:
        for anchor in soup.select(selector):
            href = (anchor.get("href") or "").strip()
            name = anchor.get_text(" ", strip=True)
            if href:
                _add_candidate(candidates, name, urljoin(SOURCE_URL, href))

    # Jina Reader often returns Markdown-style links.
    for match in re.finditer(r"\[([^\]]{2,140})\]\((https?://[^)\s]+)\)", text):
        _add_candidate(candidates, match.group(1), match.group(2))

    # De-duplicate while preserving source order.
    seen_urls: set[str] = set()
    games: list[dict[str, str]] = []
    for item in candidates:
        name = normalize_name(item["name"])
        url = canonical_url(item["url"])
        key = url.casefold()
        if key in seen_urls:
            continue
        if name.casefold() in bad_names or len(name) < 2 or len(name) > 140:
            continue
        lower = url.lower()
        if any(token in lower for token in ("/category/", "/tag/", "/author/", "/page/", "/feed/")):
            continue
        if "dlpsgame.com" not in lower:
            continue
        seen_urls.add(key)
        games.append({"name": name, "url": url})

    if len(games) < MIN_EXPECTED_GAMES:
        raise RuntimeError(f"Only {len(games)} game links detected; expected at least {MIN_EXPECTED_GAMES}. Source layout may have changed.")
    return games


def report_jina_fallback() -> None:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--search", 'Jina fallback in:title', "--limit", "5"],
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "GH_TOKEN": token},
        )
        if result.stdout.strip():
            return
        subprocess.run(
            [
                "gh", "issue", "create", "--title", "Jina fallback in use",
                "--body", "The scheduled updater could not fetch DLPSGames directly and used Jina Reader as fallback. Review source accessibility and scraper health.",
                "--repo", repo,
            ],
            check=True,
            env={**os.environ, "GH_TOKEN": token},
        )
        LOG.warning("Opened a GitHub issue because Jina fallback was required")
    except Exception as exc:
        LOG.warning("Could not report Jina fallback: %s", exc)


def get_igdb_token() -> str | None:
    client_id = os.getenv("IGDB_CLIENT_ID")
    client_secret = os.getenv("IGDB_CLIENT_SECRET")
    if not client_id or not client_secret:
        LOG.warning("IGDB credentials are not configured; metadata enrichment will be skipped")
        return None
    resp = SESSION.post(
        "https://id.twitch.tv/oauth2/token",
        params={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _escape_apicalypse(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _platforms(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for platform in item.get("platforms", []) or []:
        name = platform.get("name") or platform.get("abbreviation")
        if name and name not in result:
            result.append(name)
    return result


def _transform_game(item: dict[str, Any]) -> dict[str, Any]:
    cover = None
    if item.get("cover", {}).get("url"):
        cover = item["cover"]["url"]
        if cover.startswith("//"):
            cover = "https:" + cover
        cover = cover.replace("t_thumb", "t_cover_big")
    platforms = _platforms(item)
    has_ps5 = any(p in ("PlayStation 5", "PS5") for p in platforms)
    has_ps4 = any(p in ("PlayStation 4", "PS4") for p in platforms)
    return {
        "igdb_id": item.get("id"),
        "igdb_name": item.get("name"),
        "cover_url": cover,
        "platforms": platforms,
        "ps5_exclusive": bool(has_ps5 and not has_ps4),
    }


def igdb_batch_lookup(names: list[str], token: str) -> dict[str, dict[str, Any]]:
    client_id = os.environ["IGDB_CLIENT_ID"]
    output: dict[str, dict[str, Any]] = {}
    for start in range(0, len(names), IGDB_BATCH_SIZE):
        batch = names[start : start + IGDB_BATCH_SIZE]
        parts: list[str] = []
        for idx, name in enumerate(batch):
            escaped = _escape_apicalypse(name)
            parts.append(
                f'query games "q{idx}" {{ search "{escaped}"; fields id,name,cover.url,platforms.name,platforms.abbreviation; where platforms !=n & (platforms = 48 | platforms = 167) & version_parent = null; limit 10; }};'
            )
        body = "".join(parts)
        try:
            resp = SESSION.post(
                "https://api.igdb.com/v4/multiquery",
                headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
                data=body.encode("utf-8"),
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:
                LOG.warning("IGDB rate limited; retrying batch")
                time.sleep(3)
                resp = SESSION.post(
                    "https://api.igdb.com/v4/multiquery",
                    headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
                    data=body.encode("utf-8"),
                    timeout=REQUEST_TIMEOUT,
                )
            resp.raise_for_status()
            for block in resp.json():
                name = block.get("name", "")
                items = block.get("result") or []
                target = normalize_name(batch[int(name[1:])]) if name.startswith("q") and name[1:].isdigit() and int(name[1:]) < len(batch) else ""
                if not items or not target:
                    continue
                ranked = sorted(
                    items,
                    key=lambda x: ratio(normalize_name(x.get("name", "")).casefold(), target.casefold()),
                    reverse=True,
                )
                best = ranked[0]
                best_name = normalize_name(best.get("name", "")).casefold()
                target_name = target.casefold()
                score = max(ratio(best_name, target_name), token_set_ratio(best_name, target_name), WRatio(best_name, target_name))
                if score >= 78:
                    output[target.casefold()] = _transform_game(best)
        except Exception as exc:
            LOG.warning("IGDB batch lookup failed: %s", exc)
        if start + IGDB_BATCH_SIZE < len(names):
            time.sleep(IGDB_SLEEP)
    return output


def index_existing(existing: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_url: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    legacy_by_name: dict[str, dict[str, Any]] = {}
    for game in existing:
        if game.get("source_url"):
            by_url[canonical_url(str(game["source_url"])).casefold()] = game
        else:
            legacy_by_name[normalize_name(str(game.get("name", ""))).casefold()] = game
    return by_url, legacy_by_name


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    html, used_jina = fetch_source()
    discovered = extract_games(html)
    LOG.info("Detected %d game links", len(discovered))

    existing = load_data()
    if existing:
        old_count = len(existing)
        swing = abs(len(discovered) - old_count) / max(old_count, 1)
        if swing > MAX_COUNT_SWING:
            LOG.warning("Game count changed by %.0f%% (%d -> %d)", swing * 100, old_count, len(discovered))

    now = utc_now()
    by_url, legacy_by_name = index_existing(existing)
    result: list[dict[str, Any]] = []
    metadata_names: list[str] = []

    for item in discovered:
        url_key = canonical_url(item["url"]).casefold()
        name_key = normalize_name(item["name"]).casefold()
        old = by_url.get(url_key) or legacy_by_name.get(name_key)
        if old:
            game = dict(old)
        else:
            game = {
                "name": item["name"],
                "first_seen": now,
                "platforms": [],
                "ps5_exclusive": False,
                "cover_url": None,
            }
        game["name"] = item["name"]
        game["source_url"] = item["url"]
        game.setdefault("first_seen", now)
        game.setdefault("platforms", [])
        game.setdefault("ps5_exclusive", False)
        game.setdefault("cover_url", None)
        needs_meta = not game.get("igdb_id") or not game.get("platforms") or not game.get("cover_url")
        if needs_meta:
            metadata_names.append(item["name"])
        result.append(game)

    token = None
    if os.getenv("IGDB_CLIENT_ID") and os.getenv("IGDB_CLIENT_SECRET"):
        try:
            token = get_igdb_token()
        except Exception as exc:
            LOG.error("Could not authenticate to IGDB: %s", exc)

    if token and metadata_names:
        metadata = igdb_batch_lookup(metadata_names, token)
        for game in result:
            key = normalize_name(game["name"]).casefold()
            if key in metadata:
                game.update(metadata[key])

    result.sort(key=lambda g: (g.get("first_seen", ""), g.get("name", "").casefold()), reverse=True)
    save_data(result)
    LOG.info("Saved %d games to %s", len(result), DATA_PATH)
    if used_jina:
        LOG.warning("Jina fallback was used for this run")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Update failed: %s", exc)
        raise SystemExit(1)
