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
from urllib.parse import urljoin, urlparse

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
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("#", 1)[0]
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    # Strip tracking parameters while preserving meaningful query strings.
    if parsed.query:
        keep = []
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0].lower()
            if key.startswith("utm_") or key in {"fbclid", "gclid"}:
                continue
            keep.append(part)
        query = "&".join(keep)
    else:
        query = ""
    rebuilt = parsed._replace(query=query)
    return rebuilt.geturl().rstrip("/")


def load_data() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    try:
        text = DATA_PATH.read_text(encoding="utf-8").strip()
        # An empty file is safe to treat as a fresh dataset. This is useful
        # for the very first import/reset, while malformed non-empty JSON
        # still fails loudly so we never silently destroy existing data.
        if not text:
            LOG.warning("%s is empty; treating it as a fresh dataset", DATA_PATH)
            return []
        raw = json.loads(text)
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
        "User-Agent": "Mozilla/5.0 (compatible; PS5-Game-Tracker/2.1; +https://github.com/)",
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


def _is_source_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    return host in {"dlpsgame.com", "www.dlpsgame.com"}


def _is_image_or_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    asset_exts = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".ico",
        ".css", ".js", ".xml", ".txt", ".pdf", ".zip", ".rar", ".7z",
    )
    asset_tokens = ("/wp-content/uploads/", "/wp-content/themes/", "/wp-includes/", "/images/", "/image/")
    return path.endswith(asset_exts) or any(token in path for token in asset_tokens)


def _is_non_game_path(url: str) -> bool:
    path = urlparse(url).path.strip("/").lower()
    if not path:
        return True
    blocked_segments = {
        "category", "tag", "author", "page", "feed", "search", "wp-admin",
        "wp-login", "wp-json", "comments", "privacy-policy", "terms-and-conditions",
        "contact-us", "about-us", "list-game-ps5", "download", "downloads",
    }
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in blocked_segments for part in parts):
        return True
    # Normal game post URLs on the source are single-slug paths. This also
    # excludes most navigation and media paths while keeping real game posts.
    return len(parts) != 1


def _is_non_game_name(name: str) -> bool:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    lowered = cleaned.casefold()
    if len(cleaned) < 2 or len(cleaned) > 140:
        return True
    exact_blocked = {
        "next", "previous", "home", "search", "contact", "privacy policy",
        "terms and conditions", "login", "register", "menu", "facebook", "twitter",
        "instagram", "youtube", "read more", "continue reading", "download",
        "downloads", "click here", "more", "image", "photo", "picture",
    }
    if lowered in exact_blocked:
        return True
    if re.fullmatch(r"(?:image|img|photo|picture)\s*[-_#]?\s*\d+", lowered):
        return True
    if re.fullmatch(r"(?:download|next|previous)\s*[-_#]?\s*\d*", lowered):
        return True
    return False


def _add_candidate(candidates: list[dict[str, str]], name: str, href: str) -> None:
    name = normalize_name(name)
    href = canonical_url(href)
    if not name or not href:
        return
    if _is_non_game_name(name) or not _is_source_domain(href):
        return
    if _is_image_or_asset_url(href) or _is_non_game_path(href):
        return
    candidates.append({"name": name, "url": href})


def extract_games(text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    soup = BeautifulSoup(text, "html.parser")
    selectors = [
        ".entry-content a", ".post-content a", ".page-content a", "article a",
        "ol li a", "ul li a", "main a",
    ]
    for selector in selectors:
        for anchor in soup.select(selector):
            href = (anchor.get("href") or "").strip()
            name = anchor.get_text(" ", strip=True)
            # An image-only anchor has no trustworthy game title.
            if not name and anchor.find("img"):
                continue
            if href:
                _add_candidate(candidates, name, urljoin(SOURCE_URL, href))

    # Jina Reader returns Markdown. Do not treat image syntax ![alt](url) as a game link.
    for match in re.finditer(r"(?<!!)\[([^\]]{2,140})\]\((https?://[^)\s]+)\)", text):
        _add_candidate(candidates, match.group(1), match.group(2))

    seen_urls: set[str] = set()
    games: list[dict[str, str]] = []
    for item in candidates:
        url = canonical_url(item["url"])
        key = url.casefold()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        games.append({"name": item["name"], "url": url})

    if len(games) < MIN_EXPECTED_GAMES:
        raise RuntimeError(
            f"Only {len(games)} valid game links detected; expected at least {MIN_EXPECTED_GAMES}. "
            "Source layout may have changed."
        )
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
