#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCE_URL = os.getenv('SOURCE_URL', 'https://dlpsgame.com/list-game-ps5/')
DATA_PATH = Path(os.getenv('DATA_PATH', 'data/games.json'))
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '25'))
IGDB_SLEEP = float(os.getenv('IGDB_SLEEP', '0.35'))

LOG = logging.getLogger('ps5-tracker')


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def normalize_name(value: str) -> str:
    value = re.sub(r'\s+', ' ', value or '').strip()
    return value


def load_data() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    try:
        raw = json.loads(DATA_PATH.read_text(encoding='utf-8'))
        if not isinstance(raw, list):
            raise ValueError('games.json must contain a JSON array')
        return [x for x in raw if isinstance(x, dict) and x.get('name')]
    except Exception as exc:
        raise RuntimeError(f'Could not read {DATA_PATH}: {exc}') from exc


def save_data(games: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(games, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def fetch_source() -> tuple[str, str]:
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; PS5-Tracker/1.0; +https://github.com/)',
        'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.8',
        'Cache-Control': 'no-cache',
    }

    # Try the source directly first. Some hosts block GitHub Actions IPs.
    try:
        resp = requests.get(SOURCE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if len(resp.text) >= 1000:
            return resp.text, 'direct'
        LOG.warning('Direct source response was unexpectedly small (%d bytes)', len(resp.text))
    except requests.RequestException as exc:
        LOG.warning('Direct source request failed: %s', exc)

    # Fallback: Jina Reader fetches the page through its own infrastructure.
    reader_url = 'https://r.jina.ai/' + SOURCE_URL
    reader_headers = {
        'User-Agent': 'PS5-Tracker/1.0',
        'Accept': 'text/plain,text/markdown;q=0.9,*/*;q=0.8',
    }
    jina_key = os.getenv('JINA_API_KEY')
    if jina_key:
        reader_headers['Authorization'] = f'Bearer {jina_key}'

    try:
        resp = requests.get(reader_url, headers=reader_headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if len(resp.text) < 100:
            raise RuntimeError('Jina Reader returned unexpectedly little content')
        return resp.text, 'jina'
    except requests.RequestException as exc:
        raise RuntimeError(
            f'Could not fetch source directly or through Jina Reader. Source={SOURCE_URL}. Error={exc}'
        ) from exc


def extract_games(content: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(content, 'html.parser')
    candidates: list[tuple[int, str, str]] = []

    # First preference: numbered list items / anchors containing game-like links.
    for anchor in soup.select('ol li a, ul li a'):
        name = normalize_name(anchor.get_text(' ', strip=True))
        href = (anchor.get('href') or '').strip()
        if not name or not href:
            continue
        href = urljoin(SOURCE_URL, href)
        if not href.startswith(('http://', 'https://')):
            continue
        candidates.append((0, name, href))

    # Fallback: common WordPress content containers.
    if len(candidates) < 10:
        candidates = []
        for anchor in soup.select('article a, .entry-content a, .post-content a, .page-content a'):
            name = normalize_name(anchor.get_text(' ', strip=True))
            href = (anchor.get('href') or '').strip()
            if not name or not href:
                continue
            href = urljoin(SOURCE_URL, href)
            if href.startswith(('http://', 'https://')):
                candidates.append((1, name, href))

    # Jina Reader normally returns Markdown rather than raw HTML. Extract Markdown links too.
    if len(candidates) < 10:
        for match in re.finditer(r'\[([^\]]{2,140})\]\((https?://[^)\s]+)\)', content):
            name = normalize_name(match.group(1))
            href = match.group(2).strip()
            candidates.append((2, name, href))

    # Some Reader responses expose bare URLs in a list. Use nearby line text as the title when possible.
    if len(candidates) < 10:
        for line in content.splitlines():
            line = line.strip()
            match = re.match(r'[-*]\s*(?:\d+[.)]\s*)?(.+?)\s*[-–—:]\s*(https?://\S+)$', line)
            if match:
                candidates.append((3, normalize_name(match.group(1)), match.group(2).rstrip(').,')))

    # Filter obvious navigation / utility links while preserving natural source ordering.
    bad = {
        'next', 'previous', 'home', 'search', 'contact', 'privacy policy',
        'terms and conditions', 'login', 'register', 'menu', 'facebook',
        'twitter', 'instagram', 'youtube'
    }
    seen: set[str] = set()
    games: list[dict[str, str]] = []
    for _, name, href in candidates:
        key = normalize_name(name).casefold()
        if key in bad or len(name) < 2 or len(name) > 140:
            continue
        if key in seen:
            continue
        # Keep likely game links; skip obvious category/tag URLs.
        if any(token in href.lower() for token in ('/category/', '/tag/', '/author/', '/page/')):
            continue
        seen.add(key)
        games.append({'name': name, 'url': href})

    if not games:
        raise RuntimeError('No game links were detected. The source HTML structure may have changed.')
    return games


def get_igdb_token() -> str | None:
    client_id = os.getenv('IGDB_CLIENT_ID')
    client_secret = os.getenv('IGDB_CLIENT_SECRET')
    if not client_id or not client_secret:
        return None
    resp = requests.post(
        'https://id.twitch.tv/oauth2/token',
        params={'client_id': client_id, 'client_secret': client_secret, 'grant_type': 'client_credentials'},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def igdb_lookup(name: str, token: str) -> dict[str, Any] | None:
    client_id = os.environ['IGDB_CLIENT_ID']
    # Exact-ish search, preferring a close normalized title match.
    body = (
        'search "' + name.replace('"', '\\"') + '"; '
        'fields id,name,cover.url,platforms.name,platforms.abbreviation; '
        'limit 10;'
    )
    resp = requests.post(
        'https://api.igdb.com/v4/games',
        headers={'Client-ID': client_id, 'Authorization': f'Bearer {token}'},
        data=body.encode('utf-8'),
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 429:
        LOG.warning('IGDB rate limited while looking up %s', name)
        time.sleep(2)
        return None
    resp.raise_for_status()
    items = resp.json()
    if not items:
        return None

    target = normalize_name(name).casefold()
    items.sort(key=lambda x: (normalize_name(x.get('name', '')).casefold() != target, x.get('name', '')))
    item = items[0]
    platforms = []
    for p in item.get('platforms', []) or []:
        pname = p.get('name') or p.get('abbreviation')
        if pname and pname not in platforms:
            platforms.append(pname)
    cover = None
    if item.get('cover', {}).get('url'):
        cover = item['cover']['url']
        if cover.startswith('//'):
            cover = 'https:' + cover
        cover = cover.replace('t_thumb', 't_cover_big')
    return {
        'igdb_id': item.get('id'),
        'igdb_name': item.get('name'),
        'cover_url': cover,
        'platforms': platforms,
        'ps5_exclusive': ('PlayStation 5' in platforms or 'PS5' in platforms) and not any('PlayStation 4' in p or p == 'PS4' for p in platforms),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    content, source_method = fetch_source()
    LOG.info('Source fetched via %s', source_method)
    discovered = extract_games(content)
    LOG.info('Detected %d game links', len(discovered))

    now = utc_now()
    existing = load_data()
    by_name = {normalize_name(g['name']).casefold(): g for g in existing}

    token = None
    if os.getenv('IGDB_CLIENT_ID') and os.getenv('IGDB_CLIENT_SECRET'):
        try:
            token = get_igdb_token()
        except Exception as exc:
            LOG.error('Could not authenticate to IGDB: %s', exc)

    result: list[dict[str, Any]] = []
    for item in discovered:
        key = normalize_name(item['name']).casefold()
        old = by_name.get(key)
        game = dict(old) if old else {
            'name': item['name'],
            'first_seen': now,
            'platforms': [],
            'ps5_exclusive': False,
            'cover_url': None,
        }
        game['name'] = item['name']
        game['source_url'] = item['url']
        game.setdefault('first_seen', now)
        game.setdefault('platforms', [])
        game.setdefault('ps5_exclusive', False)
        game.setdefault('cover_url', None)

        needs_meta = token and (not game.get('platforms') or not game.get('cover_url') or 'ps5_exclusive' not in game)
        if needs_meta:
            try:
                meta = igdb_lookup(item['name'], token)
                if meta:
                    game.update(meta)
            except Exception as exc:
                LOG.warning('IGDB lookup failed for %s: %s', item['name'], exc)
            time.sleep(IGDB_SLEEP)

        result.append(game)

    # Newest discoveries first; stable secondary order.
    result.sort(key=lambda g: (g.get('first_seen', ''), g.get('name', '').casefold()), reverse=True)
    save_data(result)
    LOG.info('Saved %d games to %s', len(result), DATA_PATH)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception('Update failed: %s', exc)
        raise SystemExit(1)
