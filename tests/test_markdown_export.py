"""Markdown: front matter, body markup, reference images, comments as quotes, CLI and batch mode."""

import re
from datetime import UTC

import pytest

from conftest import BATCH_IDS, IMG_BROKEN, IMG_COMMENT, IMG_FIGURE, IMG_LAZY
from habr_exporter import cli
from habr_exporter.batch import BatchSource, SourceKind, run_batch, scan_existing
from habr_exporter.exporters import (
    ExportFormat,
    ExportOptions,
    MarkdownExporter,
    collect_image_urls,
    get_exporter,
)
from habr_exporter.exporters.markdown import (
    MAX_QUOTE_DEPTH,
    escape_text,
    html_to_markdown,
    render_markdown,
)
from habr_exporter.images import ImageMode, ImageStore
from habr_exporter.models import Comment, User

MD = ExportOptions(ExportFormat.MARKDOWN, tz=UTC)


@pytest.fixture
def images(client, article):
    store = ImageStore(client)
    store.fetch_all(collect_image_urls(article))
    return store


@pytest.fixture
def patched_client(monkeypatch, client):
    monkeypatch.setattr("habr_exporter.client.HabrClient", lambda *a, **k: client)
    return client


def _export(article, tmp_path, options=MD, images=None) -> str:
    path = MarkdownExporter().export(article, tmp_path / "a.md", options, images)
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    return raw.decode("utf-8")


def test_format_registered():
    assert isinstance(get_exporter(ExportFormat.MARKDOWN), MarkdownExporter)
    assert ExportFormat.MARKDOWN.extension == MarkdownExporter.extension == ".md"
    assert ExportFormat.MARKDOWN.label == "MD (beta)"
    assert not ExportFormat.MARKDOWN.needs_qt
    assert ExportFormat.MARKDOWN.embeds_images
    assert ExportOptions(ExportFormat.MARKDOWN, ImageMode.EMBED).downloads_images


def test_front_matter_and_header(article, tmp_path):
    text = _export(article, tmp_path)
    front, rest = text.removeprefix("---\n").split("\n---\n", 1)
    assert 'title: "Тестовая статья: «всё» сразу"' in front
    assert 'url: "https://habr.com/ru/articles/1000001/"' in front
    assert "published: 2026-03-05T09:30:00+00:00" in front
    assert 'tags: ["python", "тестирование"]' in front
    assert 'hubs: ["Блог компании ACME", "Python"]' in front
    assert rest.lstrip().startswith("# Тестовая статья: «всё» сразу\n")
    assert (
        "[@test_author](https://habr.com/ru/users/test_author/) (Тест Авторов) · 5 мар 2026 в 09:30"
        in rest
    )
    assert "Уровень сложности: Средний" in rest
    assert "[Python\\*](https://habr.com/ru/hubs/python/)" in rest
    assert "**Теги:** python, тестирование" in rest


def test_body_markup(article, tmp_path):
    text = _export(article, tmp_path)
    for expected in (
        "Вступление с **жирным**, *курсивом*, ~~зачёркнутым~~ и `inline_code()`.",
        "[относительная](https://habr.com/ru/articles/1/)",
        "H<sub>2</sub>O, x<sup>2</sup>.",
        "*Подпись к рисунку*",
        "## Раздел первый",
        "### Подраздел",
        "- Первый пункт\n- Второй пункт\n  - Вложенный пункт",
        "3. Третий\n4. Четвёртый",
        "> Цитата из документации.",
        "```python\ndef greet(name: str) -> str:\n    # приветствие\n"
        '    return f"Привет, {name}!"\n```',
        "```\nplain text block\nsecond line\n```",
        "| Ключ | Значение |\n| --- | --- |\n| a | 1 |\n| объединённая ячейка |  |",
        "Формула $x^2$ в тексте.",
        "<details>\n<summary>Под спойлером</summary>\n\nСкрытый текст.\n\n</details>",
        "Старый редактор:\\\nстрока после переноса",
        "[Встроенное видео: https://embedd.srv.habr.com/iframe/abc123](https://embedd.srv.habr.com/iframe/abc123)",
    ):
        assert expected in text
    assert "alert" not in text
    assert "sourcecraft" not in text


def test_images_embedded_as_references(article, tmp_path, images):
    text = _export(
        article, tmp_path, ExportOptions(ExportFormat.MARKDOWN, ImageMode.EMBED, tz=UTC), images
    )
    refs = dict(re.findall(r"^\[(img-\d+)\]: (\S+)$", text, flags=re.MULTILINE))
    assert refs
    assert all(uri.startswith("data:image/") for uri in refs.values())
    assert "![][img-1]" in text  # the figure comes first
    assert f"![битая]({IMG_BROKEN})" in text  # failed to download, so a link to the original
    assert IMG_FIGURE not in text
    # Image data goes at the very end, after the comments.
    assert text.index("[img-1]: data:") > text.index("## Комментарии")


def test_images_as_links(article, tmp_path, images):
    text = _export(
        article, tmp_path, ExportOptions(ExportFormat.MARKDOWN, ImageMode.LINK, tz=UTC), images
    )
    assert "data:" not in text
    assert f"![]({IMG_FIGURE})" in text
    assert f"![]({IMG_LAZY})" in text
    assert f"![]({IMG_COMMENT})" in text


def test_comments(article, tmp_path):
    text = _export(article, tmp_path)
    assert "## Комментарии (7)" in text
    assert (
        "**[@alice](https://habr.com/ru/users/alice/)** · 5 мар 2026 в 10:00 · рейтинг +5" in text
    )
    assert "> **[@test_author](https://habr.com/ru/users/test_author/)** (автор)" in text
    assert "> > **[@alice]" in text
    assert "> ```bash\n> echo hi\n> ```" in text
    assert "*НЛО прилетело и опубликовало эту надпись здесь*" in text


def test_without_comments(article, tmp_path):
    text = _export(
        article, tmp_path, ExportOptions(ExportFormat.MARKDOWN, include_comments=False, tz=UTC)
    )
    assert "Комментарии (" not in text
    assert "@alice" not in text


def test_deep_replies_are_capped(article):
    user = User(alias="u")
    root = leaf = Comment(id="0", author=user, html="<p>0</p>", published=None)
    for n in range(1, MAX_QUOTE_DEPTH + 4):
        child = Comment(id=str(n), author=user, html=f"<p>{n}</p>", published=None, level=n)
        leaf.children.append(child)
        leaf = child
    article.comments = [root]
    text = render_markdown(article, MD)
    assert "> " * MAX_QUOTE_DEPTH + "**[@u]" in text
    assert "> " * (MAX_QUOTE_DEPTH + 1) + "**[@u]" not in text


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (
            "<p>a * b _c_ [d] &lt;tag&gt; $5 snake_case</p>",
            "a \\* b \\_c\\_ \\[d\\] \\<tag\\> \\$5 snake_case",
        ),
        ("<p># не заголовок</p>", "\\# не заголовок"),
        ("<p>1. не список</p>", "1\\. не список"),
        ("<p>- не пункт</p>", "\\- не пункт"),
        ("<p>&amp;copy;</p>", "&amp;copy;"),
        ("<p><strong> пробелы </strong>снаружи</p>", "**пробелы** снаружи"),
        ("<p><code>a`b</code></p>", "``a`b``"),
        ("<pre><code>```\nfence\n```</code></pre>", "````\n```\nfence\n```\n````"),
        ("<p><a href='https://x.ru/a (1)'>x</a></p>", "[x](<https://x.ru/a (1)>)"),
        ("<p><a href='https://x.ru/'></a></p>", "[https://x.ru/](https://x.ru/)"),
        ("<p><a href='#anchor'>якорь</a></p>", "якорь"),
        ("<h1>Заголовок</h1><h4>Мелкий</h4>", "## Заголовок\n\n#### Мелкий"),
        ("<ol><li><p>раз</p><p>ещё</p></li><li>два</li></ol>", "1. раз\n\n   ещё\n\n2. два"),
        ("<blockquote><p>a</p><p>b</p></blockquote>", "> a\n>\n> b"),
        (
            "<table><tr><td>a|b</td><td><code>x|y</code></td></tr></table>",
            "| a\\|b | `x\\|y` |\n| --- | --- |",
        ),
        ("<p><img class='formula' source='E = mc^2' src='f.svg'></p>", "$$E = mc^2$$"),
        (
            "<details><summary>Код</summary><pre><code>x</code></pre></details>",
            "<details>\n<summary>Код</summary>\n\n```\nx\n```\n\n</details>",
        ),
        ("<hr>", "---"),
        ("", ""),
    ],
)
def test_html_to_markdown(markup, expected):
    assert html_to_markdown(markup) == expected


def test_escape_text():
    assert escape_text("x_y _z_ `q`") == "x_y \\_z\\_ \\`q\\`"


def test_cli_markdown(tmp_path, patched_client, capsys):
    assert cli.main(["1000001", "-f", "md", "-i", "link", "-o", str(tmp_path), "-q"]) == 0
    [saved] = tmp_path.glob("*.md")
    assert str(saved) in capsys.readouterr().out
    assert saved.read_text(encoding="utf-8").startswith("---\ntitle: ")


def test_batch_markdown(client, batch_session, tmp_path):
    options = ExportOptions(ExportFormat.MARKDOWN, ImageMode.LINK)
    source = BatchSource(SourceKind.BOOKMARKS, "alice")
    report = run_batch(source, tmp_path, options, client=client, delay=0)
    assert report.saved == 3
    assert set(scan_existing(tmp_path, ExportFormat.MARKDOWN)) == set(BATCH_IDS)
    assert not list(tmp_path.glob("~*"))
