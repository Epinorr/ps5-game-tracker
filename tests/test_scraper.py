import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.update_games import _transform_game, extract_games, normalize_name

SAMPLE_HTML = """
<html><body>
<main>
<ol>
<li><a href="https://dlpsgame.com/returnal-ps5/">Returnal</a></li>
<li><a href="https://dlpsgame.com/astro-bot/">Astro Bot</a></li>
<li><a href="https://dlpsgame.com/category/action/">Action</a></li>
</ol>
</main></body></html>
"""


def test_extract_games_html(monkeypatch):
    monkeypatch.setattr("scripts.update_games.MIN_EXPECTED_GAMES", 2)
    games = extract_games(SAMPLE_HTML)
    assert [g["name"] for g in games] == ["Returnal", "Astro Bot"]
    assert all(g["url"].startswith("https://dlpsgame.com/") for g in games)


def test_normalize_name():
    assert normalize_name("  Returnal  ") == "Returnal"
    assert normalize_name("Returnal [PS5]") == "Returnal"


def test_ps5_exclusive_transform():
    item = {
        "id": 1,
        "name": "Returnal",
        "cover": {"url": "//images.igdb.com/igdb/image/upload/t_thumb/abc.jpg"},
        "platforms": [{"id": 167, "name": "PlayStation 5"}],
    }
    result = _transform_game(item)
    assert result["ps5_exclusive"] is True
    assert result["cover_url"].startswith("https://")


def test_ps5_is_not_exclusive_when_ps4_exists():
    item = {
        "id": 2,
        "name": "Hogwarts Legacy",
        "platforms": [
            {"id": 48, "name": "PlayStation 4"},
            {"id": 167, "name": "PlayStation 5"},
        ],
    }
    assert _transform_game(item)["ps5_exclusive"] is False



def test_extract_games_rejects_images_and_non_game_links(monkeypatch):
    monkeypatch.setattr("scripts.update_games.MIN_EXPECTED_GAMES", 1)
    sample = """
    <html><body><main>
      <a href="https://dlpsgame.com/returnal-ps5/">Returnal</a>
      <a href="https://dlpsgame.com/wp-content/uploads/2026/01/image-4.jpg">Image 4</a>
      <a href="https://dlpsgame.com/category/action/">Action</a>
      <a href="https://dlpsgame.com/image-4/">Image 4</a>
    </main></body></html>
    Markdown image: ![Image 4](https://dlpsgame.com/wp-content/uploads/2026/01/image-4.jpg)
    Markdown link: [Astro Bot](https://dlpsgame.com/astro-bot/)
    """
    games = extract_games(sample)
    assert {g["name"] for g in games} == {"Returnal", "Astro Bot"}
    assert all("image" not in g["url"].lower() for g in games)
    assert all("category" not in g["url"].lower() for g in games)
