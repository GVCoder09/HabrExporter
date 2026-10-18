"""EPUB and FB2: container structure, XML validity, conversion of article and comment markup."""

import base64
import re
import zipfile
from datetime import UTC, datetime

import pytest
from lxml import etree
from lxml import html as lhtml

from conftest import IMG_FIGURE, IMG_LAZY
from habr_exporter import cli
from habr_exporter.exporters import ExportFormat, ExportOptions, collect_image_urls
from habr_exporter.exporters.base import section_headings
from habr_exporter.exporters.epub import EpubExporter, _Images, _sanitize
from habr_exporter.exporters.fb2 import FB_NS, HREF, Fb2Builder, Fb2Exporter
from habr_exporter.images import ImageMode, ImageStore
from habr_exporter.models import Article, Comment, User

OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
XHTML = "{http://www.w3.org/1999/xhtml}"
FB = f"{{{FB_NS}}}"


@pytest.fixture
def images(client, article):
    store = ImageStore(client)
    store.fetch_all(collect_image_urls(article))
    return store


def _bare_article(body: str, comments: list[Comment] | None = None, **kwargs) -> Article:
    return Article(
        id="42",
        url="https://habr.com/ru/articles/42/",
        title=kwargs.pop("title", "Статья"),
        body_html=body,
        author=kwargs.pop("author", User(alias="anon")),
        published=kwargs.pop("published", None),
        comments=comments or [],
        **kwargs,
    )


# --- EPUB -----------------------------------------------------------------------------


def _epub(tmp_path, article, images=None, **options):
    path = EpubExporter().export(
        article, tmp_path / "book.epub", ExportOptions(ExportFormat.EPUB, **options), images
    )
    return zipfile.ZipFile(path)


def _xhtml(book, name):
    return etree.fromstring(book.read(f"OEBPS/{name}"))


def test_epub_container_and_manifest(tmp_path, article, images):
    book = _epub(tmp_path, article, images)
    first = book.infolist()[0]
    assert (first.filename, first.compress_type) == ("mimetype", zipfile.ZIP_STORED)
    assert book.read("mimetype") == b"application/epub+zip"
    container = etree.fromstring(book.read("META-INF/container.xml"))
    assert container.find(".//{*}rootfile").get("full-path") == "OEBPS/content.opf"

    opf = etree.fromstring(book.read("OEBPS/content.opf"))
    manifest = {i.get("href"): i for i in opf.iter(f"{OPF}item")}
    files = {
        n.removeprefix("OEBPS/")
        for n in book.namelist()
        if n.startswith("OEBPS/") and n != "OEBPS/content.opf"
    }
    assert set(manifest) == files  # everything in the book is in the manifest, and vice versa
    assert manifest["nav.xhtml"].get("properties") == "nav"
    assert [i.get("idref") for i in opf.iter(f"{OPF}itemref")] == ["article", "comments"]
    for name in files:
        if name.endswith((".xhtml", ".ncx")):
            etree.fromstring(book.read(f"OEBPS/{name}"))

    meta = opf.find(f"{OPF}metadata")
    assert meta.findtext(f"{DC}title") == article.title
    assert meta.findtext(f"{DC}identifier") == "urn:habr:article:1000001"
    assert meta.findtext(f"{DC}creator") == "Тест Авторов"
    assert meta.findtext(f"{DC}date") == "2026-03-05T09:30:00Z"
    assert [s.text for s in meta.iter(f"{DC}subject")] == ["python", "тестирование"]
    modified = meta.find(f"{OPF}meta[@property='dcterms:modified']").text
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", modified)


def test_epub_images_are_packaged(tmp_path, article, images):
    book = _epub(tmp_path, article, images)
    names = book.namelist()
    assert not any(n.endswith(".webp") for n in names)  # WebP converted to PNG
    assert any(n.endswith(".svg") for n in names)  # SVG formulas stay vector
    xhtml = book.read("OEBPS/article.xhtml").decode()
    assert "data:" not in xhtml
    for src in re.findall(r'<img [^>]*?\ssrc="([^"]+)"', xhtml):
        assert f"OEBPS/{src}" in names
    assert f'data-original-src="{IMG_LAZY}"' in xhtml
    assert 'class="image-link"' in xhtml  # a broken image becomes a link
    figure = next(
        i for i in _xhtml(book, "article.xhtml").iter(f"{XHTML}img") if i.get("width") == "1560"
    )
    assert book.read(f"OEBPS/{figure.get('src')}").startswith(b"\x89PNG")


def test_epub_markup_is_cleaned(tmp_path, article, images):
    book = _epub(tmp_path, article, images)
    root = _xhtml(book, "article.xhtml")
    assert root.find(f".//{XHTML}details") is None
    spoiler = root.find(f".//{XHTML}div[@class='spoiler']")
    assert spoiler.find(f"{XHTML}p").text.startswith("▸ Под спойлером")
    assert root.find(f".//{XHTML}h2").get("id") == "section-1"
    assert root.find(f".//{XHTML}pre/{XHTML}code/{XHTML}span[@style]") is not None
    assert not root.xpath("//@source")  # the non-standard formula attribute is removed
    nav = _xhtml(book, "nav.xhtml")
    links = [(a.text, a.get("href")) for a in nav.iter(f"{XHTML}a")]
    assert links == [
        ("Тестовая статья: «всё» сразу", "article.xhtml"),
        ("Раздел первый", "article.xhtml#section-1"),
        ("Об авторе", "article.xhtml#authors"),
        ("Комментарии (7)", "comments.xhtml"),
    ]
    ncx = etree.fromstring(book.read("OEBPS/toc.ncx"))
    assert [p.get("playOrder") for p in ncx.iter("{*}navPoint")] == ["1", "2", "3", "4"]
    comments = book.read("OEBPS/comments.xhtml").decode()
    assert "Ответ автора." in comments
    assert "comment-children" in comments


def test_epub_link_mode_and_no_comments(tmp_path, article):
    book = _epub(tmp_path, article, None, image_mode=ImageMode.LINK, include_comments=False)
    names = book.namelist()
    assert "OEBPS/comments.xhtml" not in names
    assert not any(n.endswith((".png", ".jpg")) for n in names)  # only avatar placeholders (SVG)
    xhtml = book.read("OEBPS/article.xhtml").decode()
    assert f'href="{IMG_FIGURE}"' in xhtml
    assert "Комментарии (" not in book.read("OEBPS/nav.xhtml").decode()


def test_epub_sanitize():
    root = lhtml.fragment_fromstring(
        '<p align="center" onclick="x()">a</p><a name="top">t</a>'
        '<p id="top">dup</p><p id="1bad">n</p>'
        '<font color="red">f</font><center>c</center>'
        '<img src="x.png" width="100%"><script>1</script>'
        '<!-- комментарий --><img src="data:image/png;base64,!!!">'
        '<img src="data:image/svg+xml,<svg/>">',
        create_parent="div",
    )
    images = _Images(ImageMode.EMBED, None)
    _sanitize(root, images)
    html = lhtml.tostring(root, encoding="unicode")
    assert '<p style="text-align:center">a</p>' in html
    assert '<a id="top">t</a><p>dup</p><p>n</p>' in html
    assert "<span>f</span><div>c</div>" in html
    assert '<img src="x.png" alt="">' in html
    assert "script" not in html
    assert "комментарий" not in html
    assert html.count("<img") == 2  # a broken data: URI is dropped, the SVG is stored as a file
    assert [r.href for r in images.resources] == ["images/img001.svg"]


def test_epub_minimal_article(tmp_path):
    article = _bare_article("Текст без разметки <b>и</b> заголовков\x0b.", lang="en")
    book = _epub(tmp_path, article, None)
    root = _xhtml(book, "article.xhtml")
    assert root.get("lang") == "en"
    assert "Текст без разметки" in "".join(root.itertext())
    opf = etree.fromstring(book.read("OEBPS/content.opf"))
    assert opf.find(f".//{DC}date") is None
    nav_links = [a.text for a in _xhtml(book, "nav.xhtml").iter(f"{XHTML}a")]
    assert nav_links == ["Статья", "Об авторе", "Комментарии (0)"]


# --- FB2 ------------------------------------------------------------------------------


def _fb2(article, images=None, **options):
    root = Fb2Builder(ExportOptions(ExportFormat.FB2, **options), images).build(article)
    return etree.fromstring(etree.tostring(root))  # as if the file were read anew


def _texts(element):
    return " ".join("".join(e.itertext()) for e in element)


def _check_fb2_structure(root):
    """Check FictionBook 2 schema rules that are easy to break when converting from HTML."""
    allowed_in_section = {
        "title",
        "epigraph",
        "image",
        "annotation",
        "p",
        "poem",
        "subtitle",
        "cite",
        "empty-line",
        "table",
    }
    allowed_in_cite = {"p", "poem", "empty-line", "subtitle", "table", "text-author"}
    body = root.find(f"{FB}body")
    assert [etree.QName(c).localname for c in body] == ["title"] + ["section"] * (len(body) - 1)
    for section in root.iter(f"{FB}section"):
        names = [etree.QName(c).localname for c in section]
        assert set(names) <= allowed_in_section, names
        assert any(n != "title" for n in names), "пустой раздел"
    for cite in root.iter(f"{FB}cite"):
        assert {etree.QName(c).localname for c in cite} <= allowed_in_cite
    for p in root.iter(f"{FB}p"):
        assert p.find(f".//{FB}p") is None
        assert "".join(p.itertext()).strip() or p.find(f".//{FB}image") is not None
    binaries = {b.get("id") for b in root.iter(f"{FB}binary")}
    refs = {i.get(HREF).lstrip("#") for i in root.iter(f"{FB}image")}
    assert refs == binaries


def test_fb2_description(article, images):
    root = _fb2(article, images)
    assert root.tag == f"{FB}FictionBook"
    info = root.find(f"{FB}description/{FB}title-info")
    assert info.findtext(f"{FB}genre") == "comp_www"
    author = info.find(f"{FB}author")
    assert [author.findtext(f"{FB}{t}") for t in ("first-name", "last-name", "nickname")] == [
        "Тест",
        "Авторов",
        "test_author",
    ]
    assert info.findtext(f"{FB}book-title") == article.title
    assert info.find(f"{FB}date").get("value") == "2026-03-05"
    assert info.findtext(f"{FB}lang") == "ru"
    assert info.findtext(f"{FB}keywords") == "python, тестирование"
    assert info.findtext(f"{FB}annotation/{FB}p").startswith("Вступление с жирным, курсивом")
    doc = root.find(f"{FB}description/{FB}document-info")
    assert doc.findtext(f"{FB}src-url") == "https://habr.com/ru/articles/1000001/"
    assert doc.findtext(f"{FB}id") == "habr-article-1000001"


def test_fb2_body(article, images, qapp):
    root = _fb2(article, images)
    _check_fb2_structure(root)
    body = root.find(f"{FB}body")
    titles = [s.findtext(f"{FB}title/{FB}p") for s in body.iter(f"{FB}section")]
    assert titles == [None, "Раздел первый", "Об авторе", "Комментарии (7)"]
    first, section, *_ = body.iter(f"{FB}section")
    assert first.find(f"{FB}p/{FB}strikethrough").text == "зачёркнутым"
    assert "https://habr.com/ru/articles/1/" in [a.get(HREF) for a in first.iter(f"{FB}a")]
    assert first.find(f"{FB}image") is not None  # image from <figure>

    paragraphs = ["".join(p.itertext()) for p in section.findall(f"{FB}p")]
    assert "• Первый пункт" in paragraphs
    assert " " * 3 + "◦ Вложенный пункт" in paragraphs
    assert "3.\u00a0Третий" in paragraphs
    assert "\u00a0" * 4 + "# приветствие" in paragraphs
    assert section.findtext(f"{FB}subtitle") == "Подраздел"
    assert section.find(f"{FB}cite/{FB}p").text == "Цитата из документации."
    assert section.find(f"{FB}table/{FB}tr/{FB}td[@colspan='2']").text == "объединённая ячейка"
    assert "▸ Под спойлером" in paragraphs
    assert "Старый редактор:" in paragraphs
    assert "строка после переноса" in paragraphs
    formula = section.find(f"{FB}p/{FB}image[@alt='x^2']")
    binary = root.find(f"{FB}binary[@id='{formula.get(HREF)[1:]}']")
    assert binary.get("content-type") == "image/png"  # SVG rendered to PNG
    assert base64.b64decode(binary.text).startswith(b"\x89PNG")
    assert any("🖼 битая" in p for p in paragraphs)
    assert {b.get("content-type") for b in root.iter(f"{FB}binary")} <= {"image/png", "image/jpeg"}


def test_fb2_comments(article, images):
    root = _fb2(article, images)
    comments = root.find(f"{FB}body").findall(f"{FB}section")[-1]
    heads = [_texts([c.find(f"{FB}p")]) for c in comments.findall(f"{FB}cite")]
    assert heads[0].startswith("↳ test_author · автор")
    assert "ответ @alice" in heads[0]
    assert heads[1].startswith("↳↳ alice")
    quoted = comments.findall(f"{FB}cite")[1].findall(f"{FB}p")[1]
    assert quoted.find(f"{FB}emphasis").text == "Ответ автора."  # a quote in a reply is in italics
    assert "НЛО прилетело" in _texts(comments)
    assert (
        comments.find(f"{FB}cite/{FB}p/{FB}image") is not None
    )  # an image in a reply is in a paragraph


def test_fb2_link_mode_without_comments(article):
    root = _fb2(article, None, image_mode=ImageMode.LINK, include_comments=False)
    _check_fb2_structure(root)
    assert root.find(f"{FB}binary") is None
    titles = [s.findtext(f"{FB}title/{FB}p") for s in root.iter(f"{FB}section")]
    assert "Комментарии (7)" not in titles
    assert any(a.get(HREF) == IMG_FIGURE for a in root.iter(f"{FB}a"))


def test_fb2_edge_cases():
    reply = Comment(
        id="2",
        author=User(alias="b"),
        html=(
            "<h1>Заголовок</h1><blockquote><p>цитата</p></blockquote>"
            "<img src='data:image/png;base64,AA'>"
        ),
        published=None,
        level=1,
    )
    comments = [
        Comment(id="1", author=None, html="", published=None, deleted=True, children=[reply])
    ]
    body = (
        "Старый текст<br>с переносом <b>жирный<br>дальше</b>"
        '<div class="spoiler"><b class="spoiler_title">Старый спойлер</b>'
        '<div class="spoiler_text">внутри</div></div>'
        "<dl><dt>Термин</dt><dd>Определение</dd></dl>"
        "<ul><li></li><li><p>абзац в пункте</p></li></ul>"
        '<p><a href="https://a/">картинка <img src="https://x/y.png" alt="схема"></a> '
        '<a href="#local">якорь</a></p>'
        "<h2></h2><table><tr><td><p>ячейка</p><p>вторая</p></td></tr></table><table></table>"
        "<p>\x01управляющий символ</p><pre>\tтаб</pre>"
    )
    article = _bare_article(
        body,
        comments,
        title="Т",
        author=User(alias="solo"),
        published=datetime(2026, 1, 2, tzinfo=UTC),
    )
    root = _fb2(article)
    _check_fb2_structure(root)
    author = root.find(f"{FB}description/{FB}title-info/{FB}author")
    assert [etree.QName(c).localname for c in author] == ["nickname", "home-page"]
    text = _texts(root.find(f"{FB}body"))
    assert "Старый текст" in text
    assert "с переносом" in text
    assert "жирный дальше" in text
    assert "▸ Старый спойлер" in text
    assert "Термин" in text
    assert "Определение" in text
    assert "\x01" not in text
    assert "ячейка вторая" in text
    first = root.find(f"{FB}body/{FB}section")
    # A non-embedded image in a link is text (FB2 forbids links in links); so is an anchor.
    assert [(a.get(HREF), "".join(a.itertext())) for a in first.iter(f"{FB}a")] == [
        ("https://a/", "картинка 🖼 схема: https://x/y.png")
    ]
    assert "якорь" in text
    assert root.find(f"{FB}body//{FB}p/{FB}code").text == " " * 4 + "таб"
    replies = root.find(f"{FB}body").findall(f"{FB}section")[-1].findall(f"{FB}cite")
    assert replies[0].findtext(f"{FB}subtitle") == "Заголовок"
    assert "ответ @" not in _texts(
        [replies[0]]
    )  # the parent was deleted, so there is no one to reply to
    assert root.find(f"{FB}description/{FB}title-info/{FB}date").get("value") == "2026-01-02"


def test_fb2_and_epub_files(tmp_path, article, images, qapp):
    fb2 = Fb2Exporter().export(article, tmp_path / "a.fb2", ExportOptions(ExportFormat.FB2), images)
    assert fb2.read_bytes().startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    etree.parse(str(fb2))
    epub = EpubExporter().export(
        article, tmp_path / "a.epub", ExportOptions(ExportFormat.EPUB), images
    )
    assert zipfile.is_zipfile(epub)


@pytest.fixture
def patched_client(monkeypatch, client):
    monkeypatch.setattr("habr_exporter.client.HabrClient", lambda *a, **k: client)
    return client


@pytest.mark.parametrize("fmt", ["epub", "fb2"])
def test_cli_ebook_formats(tmp_path, patched_client, capsys, qapp, fmt):
    assert cli.main(["1000001", "-f", fmt, "-o", str(tmp_path), "-q"]) == 0
    [saved] = tmp_path.glob(f"*.{fmt}")
    assert str(saved) in capsys.readouterr().out


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<h3>А</h3><p>1</p><h4>Б</h4><h3>В</h3>", ("h3",)),
        ("<h1>А</h1><h2>Б</h2><h3>В</h3>", ("h1", "h2")),
        ("<p>без заголовков</p>", ()),
    ],
)
def test_section_headings(body, expected):
    assert section_headings(lhtml.fragment_fromstring(body, create_parent="div")) == expected


def test_ebooks_split_by_top_heading_level(tmp_path):
    # The author marks sections with <h3>: they go to the table of contents, <h4> become subtitles.
    article = _bare_article(
        "<p>Вступление</p><h3>Первый</h3><p>1</p><h4>Мелкий</h4><p>2</p><h3>Второй</h3><p>3</p>"
    )
    titles = [s.findtext(f"{FB}title/{FB}p") for s in _fb2(article).iter(f"{FB}section")]
    assert titles == [None, "Первый", "Второй", "Об авторе", "Комментарии (0)"]
    assert _fb2(article).find(f".//{FB}subtitle").text == "Мелкий"
    nav = _xhtml(_epub(tmp_path, article), "nav.xhtml")
    assert [a.text for a in nav.iter(f"{XHTML}a")][1:3] == ["Первый", "Второй"]
