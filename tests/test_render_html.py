from datetime import UTC, datetime

from lxml import html as lhtml

from conftest import IMG_FIGURE
from habr_exporter.highlight import Span, highlight, language_from_classes
from habr_exporter.models import Comment, User
from habr_exporter.render_html import (
    MAX_INDENT_LEVEL,
    STUB_AVATAR,
    prepare_fragment,
    render_article,
    render_comment,
    stylesheet,
)


def test_highlight_python():
    spans = highlight("def f():\n    return 1\n", "python")
    assert "".join(s.text for s in spans) == "def f():\n    return 1\n"
    keyword = next(s for s in spans if s.text == "def")
    assert keyword.color is not None


def test_highlight_yaml_with_tokens_missing_from_style():
    # The YAML lexer emits Token.Literal.Scalar.Plain, which is missing from the xcode style table.
    code = 'services:\n  web:\n    image: nginx:1.27\n    ports: ["80:80"]\n'
    spans = highlight(code, "yaml")
    assert "".join(s.text for s in spans) == code
    assert any(s.color for s in spans)


def test_highlight_without_language_or_unknown():
    assert highlight("code", None) == [Span("code")]
    assert highlight("code", "no-such-language") == [Span("code")]
    assert highlight("code", "plaintext") == [Span("code")]
    assert highlight("", None) == []
    assert "".join(s.text for s in highlight("echo 1", "shell")) == "echo 1"


def test_language_from_classes():
    assert language_from_classes("python") == "python"
    assert language_from_classes("hljs language-go") == "go"
    assert language_from_classes("") is None
    assert language_from_classes(None) is None


def _doc(article, **kwargs):
    html = render_article(article, tz=UTC, saved_at=datetime(2026, 3, 6, tzinfo=UTC), **kwargs)
    return html, lhtml.document_fromstring(html)


def test_render_article_structure(article):
    html, doc = _doc(article)
    assert html.startswith("<!DOCTYPE html>")
    assert doc.findtext(".//title") == "Тестовая статья: «всё» сразу / Хабр"
    assert doc.xpath("string(//h1)") == "Тестовая статья: «всё» сразу"
    assert "5 мар 2026 в 09:30" in doc.xpath("string(//div[@class='article-meta-top'])")
    meta = doc.xpath("string(//div[@class='article-meta'])")
    assert "Средний" in meta
    assert "7 мин" in meta
    assert "15K" in meta
    assert doc.xpath("//div[@class='hubs']/a/text()") == ["Блог компании ACME", "Python*"]
    assert doc.xpath("//span[@class='label']/text()") == ["Перевод"]
    assert "Jane Doe" in doc.xpath("string(//div[@class='translation-note'])")
    # Block order: article -> company -> author -> comments.
    sections = [el.get("class") for el in doc.xpath("//div[@class='page']/*")]
    assert sections == ["block", "block authors", "block comments", "saved-from"]
    cards = doc.xpath("//section[contains(@class,'authors')]/div/@class")
    assert cards == ["card company-card", "card user-card"]
    assert ".article-body" in stylesheet()


def test_cards_content(article):
    _, doc = _doc(article)
    company = doc.xpath("string(//div[contains(@class,'company-card')])")
    assert "ACME" in company
    assert "467.81" in company
    assert "26K" in company
    assert "Telegram" in company
    user = doc.xpath("string(//div[contains(@class,'user-card')])")
    assert "Тест Авторов" in user
    assert "@test_author" in user
    assert "51" in user
    assert "Карма" in user
    assert "22 подписчика" in user
    assert "На Хабре с 15 янв 2020" in user


def test_code_is_highlighted(article):
    _, doc = _doc(article)
    python = doc.xpath("//pre/code[@class='python']")[0]
    assert python.xpath(".//span[@style]")
    assert 'f"Привет, {name}!"' in python.text_content()
    plain = doc.xpath("//pre/code[not(@class)]")[0]
    assert plain.text_content() == "plain text block\nsecond line"


def test_images_identity_mode(article):
    _, doc = _doc(article)
    srcs = doc.xpath("//div[@class='article-body']//img/@src")
    assert IMG_FIGURE in srcs
    assert not doc.xpath("//a[contains(@class,'image-link')]")


def test_images_link_mode(article):
    _, doc = _doc(article, image_src=lambda url: None)
    assert not doc.xpath("//div[@class='article-body']//img")
    links = doc.xpath("//a[contains(@class,'image-link')]")
    assert IMG_FIGURE in [a.get("href") for a in links]
    inline = [a for a in links if "inline" in a.get("class")]
    assert inline  # an inline formula stays in the line
    # Avatars are replaced with a placeholder, not a link.
    assert doc.xpath("//div[@class='article-meta-top']/img/@src") == [STUB_AVATAR]


def test_images_data_uri_mode(article):
    _, doc = _doc(article, image_src=lambda url: "data:image/png;base64,AAAA")
    img = doc.xpath("//figure/img")[0]
    assert img.get("src") == "data:image/png;base64,AAAA"
    assert img.get("data-original-src") == IMG_FIGURE


def test_comments_tree_markup(article):
    _, doc = _doc(article)
    assert doc.xpath("string(//h2[@class='comments-title'])") == "Комментарии7"
    first = doc.xpath("//div[@id='comment_1']")[0]
    nested_ids = first.xpath(".//div[@class='comment-children']/div/@id")
    assert nested_ids == ["comment_2", "comment_4", "comment_3"]
    assert first.xpath(".//div[@id='comment_2']//span[@class='author-badge']")
    assert "НЛО прилетело" in doc.xpath("string(//div[@id='comment_5'])")
    scores = doc.xpath(
        "//div[@id='comment_3']/div[@class='comment']//span[contains(@class,'score')]"
    )
    assert "score-negative" in scores[0].get("class")
    assert doc.xpath("//div[@id='comment_3']//pre/code[@class='bash']//span[@style]")


def test_no_comments_and_excluded_comments(article):
    article.comments = []
    _, doc = _doc(article)
    assert "Комментариев пока нет" in doc.xpath("string(//section[contains(@class,'comments')])")
    _, doc = _doc(article, include_comments=False)
    assert not doc.xpath("//section[contains(@class,'comments')]")


def test_spoilers_open_only_for_print(article):
    _, doc = _doc(article)
    assert doc.xpath("//details")[0].get("open") is None
    _, doc = _doc(article, for_print=True)
    assert doc.xpath("//details")[0].get("open") == "open"


def test_deep_threads_stop_indenting():
    leaf = Comment(id="leaf", author=User(alias="u"), html="<p>x</p>", published=None)
    node = leaf
    for level in range(MAX_INDENT_LEVEL + 2, 0, -1):
        node = Comment(
            id=str(level), author=User(alias="u"), html="<p>x</p>", published=None, children=[node]
        )
    _fix(node, 0)
    doc = lhtml.fragment_fromstring(render_comment(node, lambda u: u, UTC), create_parent="div")
    classes = doc.xpath("//div[starts-with(@class,'comment-children')]/@class")
    assert "comment-children" in classes
    assert "comment-children flat" in classes


def _fix(comment, level):
    comment.level = level
    for child in comment.children:
        _fix(child, level + 1)


def test_prepare_fragment_trivial():
    assert prepare_fragment("", lambda u: u) == ""
    assert prepare_fragment("текст", lambda u: u) == "текст"
    out = prepare_fragment('<img src="data:image/png;base64,AA">', lambda u: None)
    assert "data:image/png" in out
