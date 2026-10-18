"""JSON: document schema, field types, flat comment list, text from HTML, CLI and batch mode."""

import json
from datetime import UTC, datetime

import pytest

from conftest import (
    AVATAR_ALICE,
    AVATAR_AUTHOR,
    BATCH_IDS,
    COMPANY_LOGO,
    IMG_BROKEN,
    IMG_COMMENT,
    IMG_FIGURE,
    IMG_FORMULA,
    IMG_LAZY,
)
from habr_exporter import cli
from habr_exporter.batch import BatchSource, SourceKind, run_batch, scan_existing
from habr_exporter.exporters import ExportFormat, ExportOptions, JsonExporter, get_exporter
from habr_exporter.exporters.json import SCHEMA, SCHEMA_VERSION, build_document, html_to_text
from habr_exporter.images import ImageMode
from habr_exporter.service import export_article

JSON = ExportOptions(ExportFormat.JSON)


def _export(article, tmp_path, options=JSON) -> dict:
    path = JsonExporter().export(article, tmp_path / "a.json", options, None)
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"}\n")
    assert "Тестовая статья".encode() in raw  # ensure_ascii=False: Cyrillic without \u escapes
    return json.loads(raw.decode("utf-8"))


def _iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return parsed


def test_format_registered():
    assert isinstance(get_exporter(ExportFormat.JSON), JsonExporter)
    assert ExportFormat.JSON.label == "JSON"
    assert not ExportFormat.JSON.beta
    assert not ExportFormat.JSON.needs_qt
    assert not ExportFormat.JSON.embeds_images
    assert ExportFormat.JSON.extension == JsonExporter.extension == ".json"
    assert not ExportOptions(ExportFormat.JSON, ImageMode.EMBED).downloads_images
    assert ExportOptions(ExportFormat.HTML, ImageMode.EMBED).downloads_images
    assert not ExportOptions(ExportFormat.HTML, ImageMode.LINK).downloads_images


def test_envelope(article, tmp_path):
    doc = _export(article, tmp_path)
    assert list(doc) == ["schema", "version", "exported_at", "source_url", "article"]
    assert doc["schema"] == SCHEMA == "habr-exporter/article"
    assert doc["version"] == SCHEMA_VERSION == 1
    assert _iso(doc["exported_at"]).utcoffset().total_seconds() == 0
    assert doc["source_url"] == article.url == doc["article"]["url"]


def test_exported_at_is_deterministic(article):
    moment = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert (
        build_document(article, JSON, exported_at=moment)["exported_at"]
        == "2026-10-16T12:00:00+00:00"
    )


def test_article_fields_and_types(article, tmp_path):
    a = _export(article, tmp_path)["article"]
    assert list(a) == [
        "id", "url", "title", "lang", "post_type", "published", "reading_time", "complexity",
        "is_translation", "original_author", "original_url", "hubs", "tags", "statistics", "author",
        "company", "body_html", "body_text", "images", "comments",
    ]  # fmt: skip
    assert a["id"] == "1000001"
    assert a["title"] == "Тестовая статья: «всё» сразу"
    assert a["lang"] == "ru"
    assert a["post_type"] == "article"
    assert _iso(a["published"]) == datetime(2026, 3, 5, 9, 30, tzinfo=UTC)
    assert a["reading_time"] == 7
    assert a["complexity"] == "medium"  # the API value, not the label "Средний"
    assert a["is_translation"] is True
    assert a["original_author"] == "Jane Doe"
    assert a["original_url"] == "https://example.com/original"
    assert a["hubs"][0] == {
        "alias": "acme",
        "title": "Блог компании ACME",
        "url": "https://habr.com/ru/companies/acme/articles/",
        "is_corporative": True,
        "is_profiled": False,
    }
    assert a["hubs"][1]["url"] == "https://habr.com/ru/hubs/python/"
    assert a["tags"] == ["python", "тестирование"]
    assert a["statistics"] == {
        "score": 17, "votes": 19, "votes_plus": 18, "votes_minus": 1, "views": 15400,
        "favorites": 21, "comments": 7,
    }  # fmt: skip
    assert a["body_html"] == article.body_html
    assert "<script" not in a["body_html"]


def test_author_and_company(article, tmp_path):
    a = _export(article, tmp_path)["article"]
    author = a["author"]
    assert author["alias"] == "test_author"
    assert author["url"] == "https://habr.com/ru/users/test_author/"
    assert author["avatar_url"] == AVATAR_AUTHOR
    assert author["followers"] == 22
    assert isinstance(author["rating"], float)
    assert author["workplace"] == ["ACME"]
    assert _iso(author["registered"]).year == 2020
    assert author["deleted"] is False
    # All fields of the User model end up in JSON.
    assert set(author) == {*article.author.__slots__, "url"}

    company = a["company"]
    assert set(company) == {*article.company.__slots__, "url"}
    assert company["rating"] == 467.81
    assert company["subscribers"] == 25905
    assert company["logo_url"] == COMPANY_LOGO
    assert company["contacts"][1] == {"title": "Telegram", "url": "https://t.me/acme"}
    assert company["url"] == "https://habr.com/ru/companies/acme/"


def test_no_company_and_missing_values(article, tmp_path):
    article.company = None
    article.complexity = None
    article.published = None
    article.reading_time = None
    a = _export(article, tmp_path)["article"]
    assert a["company"] is None
    assert a["complexity"] is None
    assert a["published"] is None
    assert a["reading_time"] is None
    assert COMPANY_LOGO not in [img["url"] for img in a["images"]]


def test_body_text(article, tmp_path):
    text = _export(article, tmp_path)["article"]["body_text"]
    paragraphs = text.split("\n\n")
    assert paragraphs[0] == "Вступление с жирным, курсивом, зачёркнутым и inline_code()."
    assert "<" not in text.replace("->", "")
    assert "Раздел первый" in paragraphs
    assert "• Первый пункт" in paragraphs
    assert "    • Вложенный пункт" in paragraphs
    assert "3. Третий" in paragraphs
    assert (
        'def greet(name: str) -> str:\n    # приветствие\n    return f"Привет, {name}!"'
        in paragraphs
    )
    assert "Ключ\tЗначение" in paragraphs
    assert "Формула x^2 в тексте." in paragraphs  # TeX source of the formula
    assert "Старый редактор:\nстрока после переноса" in paragraphs
    assert "code-explainer" not in text
    assert "\n\n\n" not in text


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("просто текст", "просто текст"),
        ("<p>  много   \n пробелов </p>", "много пробелов"),
        ("начало<p>абзац</p>хвост", "начало\n\nабзац\n\nхвост"),
        ("<ul><li><p>в абзаце</p></li><li></li><li>после</li></ul>", "• в абзаце\n\n• после"),
        ("<ol><li>а</li><li>б</li></ol>", "1. а\n\n2. б"),
        ("<p>a<!-- comment -->b</p>", "ab"),
        ('<p><img src="x.png" alt="картинка">текст</p>', "текст"),
        ("<pre>   </pre><p>после</p>", "после"),
        ("<table><tr><td></td></tr><tr><th>a</th><td>b</td></tr></table>", "a\tb"),
        ("<details><summary>Спойлер</summary><div>внутри</div></details>", "Спойлер\n\nвнутри"),
    ],
)
def test_html_to_text(markup, expected):
    assert html_to_text(markup) == expected


def test_images_are_links_only(article, tmp_path):
    images = _export(article, tmp_path)["article"]["images"]
    urls = [img["url"] for img in images]
    assert urls[0] == IMG_FIGURE
    assert {
        IMG_LAZY,
        IMG_FORMULA,
        IMG_BROKEN,
        IMG_COMMENT,
        AVATAR_AUTHOR,
        AVATAR_ALICE,
        COMPANY_LOGO,
    } <= set(urls)
    assert len(urls) == len(set(urls))
    assert {"url": IMG_BROKEN, "alt": "битая"} in images
    assert {"url": IMG_FORMULA, "alt": "x^2"} in images
    assert {"url": IMG_FIGURE} in images  # no alt, no key
    assert all(not url.startswith("data:") for url in urls)


def test_comments_flat_in_display_order(article, tmp_path):
    comments = _export(article, tmp_path)["article"]["comments"]
    assert [c["id"] for c in comments] == [c.id for c in article.iter_comments()]
    assert len(comments) == article.comments_total == 7
    assert list(comments[0]) == [
        "id", "parent_id", "level", "author", "published", "edited", "score", "votes",
        "is_post_author", "deleted", "html", "text",
    ]  # fmt: skip

    by_id = {c["id"]: c for c in comments}
    assert [(c["id"], c["parent_id"], c["level"]) for c in comments] == [
        ("1", None, 0),
        ("2", "1", 1),
        ("4", "2", 2),
        ("3", "1", 1),
        ("5", None, 0),
        ("6", None, 0),
        ("7", "6", 1),
    ]
    # The parent always comes before the reply and one level higher.
    seen: set[str] = set()
    for comment in comments:
        if comment["parent_id"] is not None:
            assert comment["parent_id"] in seen
            assert comment["level"] == by_id[comment["parent_id"]]["level"] + 1
        seen.add(comment["id"])

    first = by_id["1"]
    assert first["author"] == {"alias": "alice", "fullname": "Алиса", "avatar_url": AVATAR_ALICE}
    assert _iso(first["published"])
    assert first["edited"] is None
    assert first["score"] == 5
    assert first["text"] == "Первый комментарий с ссылкой."
    assert 'href="https://example.com"' in first["html"]
    assert by_id["2"]["is_post_author"] is True
    assert by_id["3"]["score"] == -1
    assert by_id["3"]["text"] == "Ещё ответ\n\necho hi"
    deleted = by_id["5"]
    assert deleted["deleted"] is True
    assert deleted["author"] is None
    assert deleted["text"] == ""


def test_without_comments(article, tmp_path):
    a = _export(article, tmp_path, ExportOptions(ExportFormat.JSON, include_comments=False))[
        "article"
    ]
    assert a["comments"] is None
    urls = {img["url"] for img in a["images"]}
    assert IMG_COMMENT not in urls
    assert AVATAR_ALICE not in urls
    assert AVATAR_AUTHOR in urls  # the article author's avatar stays


def test_export_article_skips_image_downloads(tmp_path, client, fake_session, article):
    path = export_article(
        article, tmp_path / "a", ExportOptions(ExportFormat.JSON, ImageMode.EMBED), client=client
    )
    assert path == tmp_path / "a.json"
    assert not any("habrastorage" in url for url in fake_session.requests)
    assert json.loads(path.read_text(encoding="utf-8"))["article"]["id"] == "1000001"


# --- CLI and batch mode -------------------------------------------------------------


@pytest.fixture
def patched_client(monkeypatch, client):
    monkeypatch.setattr("habr_exporter.client.HabrClient", lambda *a, **k: client)
    return client


def test_cli_json(tmp_path, patched_client, fake_session, capsys):
    assert cli.main(["1000001", "-f", "json", "-o", str(tmp_path), "-q"]) == 0
    [saved] = tmp_path.glob("*.json")
    assert str(saved) in capsys.readouterr().out
    doc = json.loads(saved.read_text(encoding="utf-8"))
    assert doc["schema"] == SCHEMA
    assert len(doc["article"]["comments"]) == 7
    assert not any("habrastorage" in url for url in fake_session.requests)


def test_cli_json_no_comments(tmp_path, patched_client):
    target = tmp_path / "out.json"
    assert cli.main(["1000001", "-f", "json", "-o", str(target), "--no-comments", "-q"]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["article"]["comments"] is None


def test_batch_json(client, batch_session, tmp_path):
    options = ExportOptions(ExportFormat.JSON, ImageMode.EMBED)
    source = BatchSource(SourceKind.BOOKMARKS, "alice")
    report = run_batch(source, tmp_path, options, client=client, delay=0)
    assert report.saved == 3
    assert set(scan_existing(tmp_path, ExportFormat.JSON)) == set(BATCH_IDS)
    assert set(scan_existing(tmp_path, None)) == set(BATCH_IDS)
    assert not any("habrastorage" in url for url in batch_session.requests)
    index = report.index_path.read_text(encoding="utf-8")
    assert ".json" in index
    # A repeated run skips what is already saved.
    again = run_batch(source, tmp_path, options, client=client, delay=0)
    assert again.saved == 0
    assert again.skipped == 3
