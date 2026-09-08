#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import WRatio, ratio, token_set_ratio

SOURCE_URL = os.getenv("SOURCE_URL", "https://dlpsgame.com/list-game-ps5/")
JINA_URL = f"https://r.jina.ai/{SOURCE_URL}"
DATA_PATH = Path(os.getenv("DATA_PATH", "data/games.json"))
CACHE_PATH = Path(os.getenv("CACHE_PATH", "data/igdb_cache.json"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
MIN_EXPECTED_GAMES = int(os.getenv("MIN_EXPECTED_GAMES", "50"))
MAX_COUNT_SWING = float(os.getenv("MAX_COUNT_SWING", "0.30"))
IGDB_INTERVAL = float(os.getenv("IGDB_INTERVAL", "0.27"))
IGDB_MATCH_THRESHOLD = float(os.getenv("IGDB_MATCH_THRESHOLD", "72"))
MIN_IGDB_MATCH_RATE = float(os.getenv("MIN_IGDB_MATCH_RATE", "0.25"))

LOG = logging.getLogger("ps5-tracker")
SESSION = requests.Session()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_name(value: str) -> str:
    value = value or ""
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\([^)]*\b(?:PS4|PS5|PKG|CUSA)\b[^)]*\)", " ", value, flags=re.I)
    value = re.sub(r"\b(?:PS4|PS5)\b", " ", value, flags=re.I)
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_url(url: str) -> str:
    url = (url or "").strip().split("#", 1)[0]
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    query = ""
    if parsed.query:
        kept = []
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0].lower()
            if not key.startswith("utm_") and key not in {"fbclid", "gclid"}:
                kept.append(part)
        query = "&".join(kept)
    return parsed._replace(query=query).geturl().rstrip("/")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


def load_data() -> list[dict[str, Any]]:
    raw = load_json(DATA_PATH, [])
    if not isinstance(raw, list):
        raise RuntimeError(f"Could not read {DATA_PATH}: expected a JSON array")
    return [x for x in raw if isinstance(x, dict) and x.get("name")]


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_cache() -> dict[str, dict[str, Any]]:
    raw = load_json(CACHE_PATH, {})
    return raw if isinstance(raw, dict) else {}


def _cache_meta(entry: Any) -> dict[str, Any] | None:
    # Current cache format: {"meta": {...}, "checked_at": "..."}.
    if isinstance(entry, dict) and ("meta" in entry or "checked_at" in entry):
        meta = entry.get("meta")
        return meta if isinstance(meta, dict) else None
    # Backward compatibility with the earlier raw-meta cache format.
    return entry if isinstance(entry, dict) else None


def _cache_should_retry(entry: Any, *, negative_after_days: int = 7) -> bool:
    if not isinstance(entry, dict) or "meta" not in entry:
        return True
    checked = entry.get("checked_at")
    if not checked:
        return True
    try:
        checked_at = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - checked_at >= timedelta(days=negative_after_days)
    except ValueError:
        return True


def _request_source(url: str) -> requests.Response:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PS5-Game-Tracker/2.3)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    }
    return SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)


def fetch_source() -> tuple[str, bool]:
    try:
        response = _request_source(SOURCE_URL)
        response.raise_for_status()
        if len(response.text) < 1000:
            raise RuntimeError("Source page returned unexpectedly little content")
        LOG.info("Source fetched directly")
        return response.text, False
    except Exception as exc:
        LOG.warning("Direct source request failed: %s", exc)
    response = _request_source(JINA_URL)
    response.raise_for_status()
    if len(response.text) < 1000:
        raise RuntimeError("Jina Reader returned unexpectedly little content")
    LOG.info("Source fetched via jina")
    report_jina_fallback()
    return response.text, True


def _is_source_domain(url: str) -> bool:
    return urlparse(url).netloc.lower().split(":", 1)[0] in {"dlpsgame.com", "www.dlpsgame.com"}


def _is_bad_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".ico", ".css", ".js", ".xml", ".txt", ".pdf", ".zip", ".rar", ".7z")):
        return True
    blocked = ("/wp-content/", "/wp-includes/", "/images/", "/image/", "/tag/", "/category/", "/author/", "/feed/", "/page/")
    return any(token in path for token in blocked)


def _is_bad_name(name: str) -> bool:
    n = re.sub(r"\s+", " ", name or "").strip().casefold()
    if not 2 <= len(n) <= 140:
        return True
    if n in {"image", "photo", "picture", "next", "previous", "download", "downloads", "home", "menu", "search", "read more", "continue reading"}:
        return True
    return bool(re.fullmatch(r"(?:image|img|photo|picture)\s*[-_#]?\s*\d+", n))


def _add_candidate(candidates: list[dict[str, str]], name: str, href: str) -> None:
    name = normalize_name(name)
    href = canonical_url(href)
    if not name or not href or _is_bad_name(name) or not _is_source_domain(href) or _is_bad_url(href):
        return
    candidates.append({"name": name, "url": href})


def extract_games(text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    soup = BeautifulSoup(text, "html.parser")
    selectors = [".entry-content a", ".post-content a", ".page-content a", "article a", "ol li a", "ul li a", "main a"]
    for selector in selectors:
        for anchor in soup.select(selector):
            href = anchor.get("href") or ""
            name = anchor.get_text(" ", strip=True)
            if name and href:
                _add_candidate(candidates, name, urljoin(SOURCE_URL, href))
    # Markdown links from Jina; explicitly exclude image syntax.
    for match in re.finditer(r"(?<!!)\[([^\]]{2,140})\]\((https?://[^)\s]+)\)", text):
        _add_candidate(candidates, match.group(1), match.group(2))
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in candidates:
        key = item["url"].casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    if len(result) < MIN_EXPECTED_GAMES:
        raise RuntimeError(f"Only {len(result)} valid game links detected; expected at least {MIN_EXPECTED_GAMES}")
    return result


def report_jina_fallback() -> None:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    try:
        env = {**os.environ, "GH_TOKEN": token}
        found = subprocess.run(["gh", "issue", "list", "--state", "open", "--search", "Jina fallback in:title", "--limit", "5"], capture_output=True, text=True, check=True, env=env)
        if found.stdout.strip():
            return
        subprocess.run(["gh", "issue", "create", "--title", "Jina fallback in use", "--body", "Direct DLPSGames access returned an error, so the scheduled updater used Jina Reader fallback.", "--repo", repo], check=True, env=env)
    except Exception as exc:
        LOG.warning("Could not report Jina fallback: %s", exc)


def get_igdb_token() -> str:
    client_id = os.getenv("IGDB_CLIENT_ID")
    client_secret = os.getenv("IGDB_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("IGDB credentials are not configured")
    response = SESSION.post("https://id.twitch.tv/oauth2/token", params={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("IGDB authentication returned no access token")
    return token


def _igdb_headers(client_id: str, token: str) -> dict[str, str]:
    return {"Client-ID": client_id, "Authorization": f"Bearer {token}", "Content-Type": "text/plain"}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _platforms(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for platform in item.get("platforms") or []:
        if isinstance(platform, dict):
            name = platform.get("name") or platform.get("abbreviation")
            if name and name not in result:
                result.append(name)
    return result


def _cover_url(item: dict[str, Any]) -> str | None:
    cover = item.get("cover") or {}
    url = cover.get("url") if isinstance(cover, dict) else None
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    return url.replace("t_thumb", "t_cover_big")


def _transform_game(item: dict[str, Any]) -> dict[str, Any]:
    pnames = _platforms(item)
    pids = {int(p.get("id")) for p in item.get("platforms") or [] if isinstance(p, dict) and str(p.get("id", "")).isdigit()}
    has_ps5 = 167 in pids or any(p.casefold() in {"ps5", "playstation 5"} for p in pnames)
    has_ps4 = 48 in pids or any(p.casefold() in {"ps4", "playstation 4"} for p in pnames)
    return {"igdb_id": item.get("id"), "igdb_name": item.get("name"), "cover_url": _cover_url(item), "platforms": pnames, "ps5_exclusive": bool(has_ps5 and not has_ps4)}


def _score(item: dict[str, Any], target: str) -> float:
    a = normalize_name(str(item.get("name", ""))).casefold()
    b = normalize_name(target).casefold()
    base = max(ratio(a, b), token_set_ratio(a, b), WRatio(a, b))
    pids = {int(p.get("id")) for p in item.get("platforms") or [] if isinstance(p, dict) and str(p.get("id", "")).isdigit()}
    # Prefer a candidate actually available on PS4/PS5 over unrelated games.
    if pids & {48, 167}:
        base += 5
    if item.get("cover"):
        base += 1
    return min(base, 100)


def _best_match(items: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    if not items:
        return None
    target_key = normalize_name(target).casefold()
    exact = [x for x in items if normalize_name(str(x.get("name", ""))).casefold() == target_key]
    pool = exact or items
    best = max(pool, key=lambda x: _score(x, target))
    threshold = 70 if exact else IGDB_MATCH_THRESHOLD
    return best if _score(best, target) >= threshold else None


def lookup_igdb(name: str, client_id: str, token: str) -> dict[str, Any] | None:
    body = f'fields id,name,cover.url,platforms.id,platforms.name,platforms.abbreviation; search "{_escape(name)}"; limit 20;'
    for attempt in range(3):
        try:
            response = SESSION.post("https://api.igdb.com/v4/games", headers=_igdb_headers(client_id, token), data=body.encode("utf-8"), timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise RuntimeError("Unexpected IGDB response")
            best = _best_match(data, name)
            return _transform_game(best) if best else None
        except Exception as exc:
            if attempt == 2:
                LOG.warning("IGDB lookup failed for %r: %s", name, exc)
            else:
                time.sleep(0.5 + attempt)
    return None


def enrich_with_igdb(games: list[dict[str, Any]]) -> tuple[int, int, int]:
    client_id = os.getenv("IGDB_CLIENT_ID")
    client_secret = os.getenv("IGDB_CLIENT_SECRET")
    if not client_id or not client_secret:
        if any(not g.get("cover_url") or not g.get("platforms") for g in games):
            raise RuntimeError("IGDB credentials are missing; cannot enrich metadata")
        return 0, 0, sum(1 for g in games if g.get("ps5_exclusive"))
    token = get_igdb_token()
    cache = load_cache()
    names = [g["name"] for g in games if not g.get("igdb_id") or not g.get("cover_url") or not g.get("platforms")]
    hits = covers = exclusive = 0
    LOG.info("Enriching %d games with IGDB metadata", len(names))
    for idx, name in enumerate(names, 1):
        key = normalize_name(name).casefold()
        entry = cache.get(key)
        meta = _cache_meta(entry) if entry is not None else None
        should_lookup = entry is None or (not meta and _cache_should_retry(entry))
        if should_lookup:
            meta = lookup_igdb(name, client_id, token)
            cache[key] = {"meta": meta or {}, "checked_at": utc_now()}
            save_json(CACHE_PATH, cache)
            time.sleep(max(IGDB_INTERVAL, 0.25))
        if meta:
            hits += 1
            covers += int(bool(meta.get("cover_url")))
            exclusive += int(bool(meta.get("ps5_exclusive")))
            for game in games:
                if normalize_name(game["name"]).casefold() == key:
                    # Never erase already-valid fields with empty values.
                    for field, value in meta.items():
                        if value not in (None, [], ""):
                            game[field] = value
                    break
        if idx % 50 == 0 or idx == len(names):
            LOG.info("IGDB progress: %d/%d processed, %d matched, %d covers, %d PS5-only", idx, len(names), hits, covers, exclusive)
    save_json(CACHE_PATH, cache)
    return hits, covers, exclusive


def index_existing(existing: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_url: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for game in existing:
        url = canonical_url(str(game.get("source_url", ""))).casefold()
        name = normalize_name(str(game.get("name", ""))).casefold()
        if url:
            by_url[url] = game
        elif name:
            by_name[name] = game
    return by_url, by_name


def validate_metadata(games: list[dict[str, Any]], fresh: bool, matched: int) -> None:
    if not fresh or not games:
        return
    rate = matched / len(games)
    if rate < MIN_IGDB_MATCH_RATE:
        raise RuntimeError(f"IGDB metadata match rate too low: {matched}/{len(games)} ({rate:.1%}). Refusing to publish an un-enriched dataset.")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    text, used_jina = fetch_source()
    discovered = extract_games(text)
    LOG.info("Detected %d game links", len(discovered))

    existing = load_data()
    old_count = len(existing)
    if old_count:
        swing = abs(len(discovered) - old_count) / old_count
        if swing > MAX_COUNT_SWING:
            LOG.warning("Game count changed by %.0f%% (%d -> %d)", swing * 100, old_count, len(discovered))

    now = utc_now()
    by_url, by_name = index_existing(existing)
    result: list[dict[str, Any]] = []
    for item in discovered:
        old = by_url.get(canonical_url(item["url"]).casefold()) or by_name.get(normalize_name(item["name"]).casefold())
        game = dict(old) if old else {"name": item["name"], "first_seen": now, "platforms": [], "ps5_exclusive": False, "cover_url": None}
        game["name"] = item["name"]
        game["source_url"] = item["url"]
        game.setdefault("first_seen", now)
        game.setdefault("platforms", [])
        game.setdefault("ps5_exclusive", False)
        game.setdefault("cover_url", None)
        result.append(game)

    fresh = not bool(existing)
    matched, covers, exclusive = enrich_with_igdb(result)
    LOG.info("IGDB enrichment: %d matched, %d covers, %d PS5-only", matched, covers, exclusive)
    validate_metadata(result, fresh, matched)

    result.sort(key=lambda g: (g.get("first_seen", ""), g.get("name", "").casefold()), reverse=True)
    save_json(DATA_PATH, result)
    LOG.info("Saved %d games to %s", len(result), DATA_PATH)
    if used_jina:
        LOG.warning("Jina fallback was used for this run")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
