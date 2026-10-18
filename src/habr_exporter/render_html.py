"""Assembly of a standalone HTML page of an article in Habr style.

This page is the basis for HTML (saved as is) and PDF (printed by the Chromium engine). The caller
decides how to substitute images via ``image_src``: the function receives the source URL and
returns the value for ``src``, or ``None`` if a link to the image should be shown instead.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, tzinfo
from functools import lru_cache
from html import escape
from importlib import resources

from lxml import etree
from lxml import html as lhtml

from .formatting import format_count, format_datetime, format_number, format_score, plural
from .highlight import Span, highlight, language_from_classes
from .models import Article, Comment, Company, User

ImageSrc = Callable[[str], str | None]

#: From this depth on, comment threads stop shifting to the right,
#: otherwise long discussions turn into a narrow column in the PDF.
MAX_INDENT_LEVEL = 8

_STUB_AVATAR_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="3" fill="#e3eaef"/>'
    '<circle cx="12" cy="9.5" r="4" fill="#b7c6d0"/>'
    '<path d="M4.5 21c.8-4 3.8-6 7.5-6s6.7 2 7.5 6z" fill="#b7c6d0"/></svg>'
)
STUB_AVATAR = "data:image/svg+xml;base64," + base64.b64encode(_STUB_AVATAR_SVG.encode()).decode()


@lru_cache(maxsize=1)
def stylesheet() -> str:
    """Return CSS replicating the Habr styling."""
    return resources.files("habr_exporter").joinpath("assets/habr.css").read_text(encoding="utf-8")


def identity_src(url: str) -> str | None:
    """Keep the original image URL."""
    return url


# --- Article body -----------------------------------------------------------------


def _spans_to_html(spans: list[Span]) -> str:
    out = []
    for span in spans:
        style = []
        if span.color:
            style.append(f"color:#{span.color}")
        if span.bold:
            style.append("font-weight:bold")
        if span.italic:
            style.append("font-style:italic")
        text = escape(span.text, quote=False)
        out.append(f'<span style="{";".join(style)}">{text}</span>' if style else text)
    return "".join(out)


def _is_inline_image(img: etree._Element) -> bool:
    if "formula" in (img.get("class") or "") and "inline" in (img.get("class") or ""):
        return True
    parent = img.getparent()
    if parent is None or parent.tag not in {"p", "span", "a", "li", "td", "strong", "em", "b", "i"}:
        return False
    siblings_text = (parent.text or "") + "".join((c.tail or "") for c in parent)
    return bool(siblings_text.strip())


def _image_placeholder(img: etree._Element, url: str) -> etree._Element:
    inline = _is_inline_image(img)
    link = lhtml.Element(
        "a", {"href": url, "class": "image-link inline" if inline else "image-link"}
    )
    alt = (img.get("alt") or "").strip()
    link.text = f"🖼 {alt or 'Изображение'}: {url}"
    link.tail = img.tail
    return link


def prepare_fragment(markup: str, image_src: ImageSrc, *, expand_spoilers: bool = False) -> str:
    """Substitute images and highlight code in an HTML fragment of an article or comment.

    ``expand_spoilers`` expands ``<details>``, which is needed for printing, where they cannot be
    opened.
    """
    if not markup:
        return ""
    root = lhtml.fragment_fromstring(markup, create_parent="div")

    if expand_spoilers:
        for details in root.iter("details"):
            details.set("open", "open")

    for img in list(root.iter("img")):
        url = img.get("src") or ""
        if not url or url.startswith("data:"):
            continue
        resolved = image_src(url)
        if resolved is None:
            parent = img.getparent()
            if parent is not None:
                parent.replace(img, _image_placeholder(img, url))
        else:
            img.set("src", resolved)
            img.set("data-original-src", url)

    for code in root.xpath(".//pre/code"):
        language = language_from_classes(code.get("class"))
        if not language:
            continue
        source = code.text_content()
        new_code = lhtml.fragment_fromstring(
            f'<code class="{escape(language)}">{_spans_to_html(highlight(source, language))}</code>'
        )
        new_code.tail = code.tail
        code.getparent().replace(code, new_code)

    parts = [escape(root.text, quote=False)] if root.text else []
    parts.extend(lhtml.tostring(child, encoding="unicode") for child in root)
    return "".join(parts)


# --- Page blocks --------------------------------------------------------------


def _avatar(user: User | None, image_src: ImageSrc, css: str = "userpic") -> str:
    src = None
    if user and user.avatar_url:
        src = image_src(user.avatar_url)
    return f'<img class="{css}" src="{escape(src or STUB_AVATAR)}" alt="">'


def _user_link(user: User | None) -> str:
    if user is None:
        return '<span class="username">НЛО</span>'
    return f'<a class="username" href="{escape(user.url)}">{escape(user.alias)}</a>'


def _score_class(value: float | int | None) -> str:
    if not value:
        return ""
    return "score-positive" if value > 0 else "score-negative"


def render_header(article: Article, image_src: ImageSrc, tz: tzinfo | None) -> str:
    published = article.published
    parts = [
        '<div class="article-meta-top">',
        _avatar(article.author, image_src),
        _user_link(article.author),
        f'<time datetime="{published.isoformat() if published else ""}">'
        f"{escape(format_datetime(published, tz))}</time>",
        "</div>",
        f'<h1 class="title">{escape(article.title)}</h1>',
    ]

    meta = []
    if article.complexity:
        meta.append(f"<span>Уровень сложности <b>{escape(article.complexity)}</b></span>")
    if article.reading_time:
        meta.append(f"<span>Время на прочтение <b>{article.reading_time} мин</b></span>")
    if article.statistics.views:
        meta.append(
            f"<span>Количество просмотров <b>{format_count(article.statistics.views)}</b></span>"
        )
    if meta:
        parts.append(f'<div class="article-meta">{"".join(meta)}</div>')

    if article.hubs:
        hubs = ", ".join(
            f'<a href="{escape(h.url)}">{escape(h.title)}{"*" if h.is_profiled else ""}</a>'
            for h in article.hubs
        )
        parts.append(f'<div class="hubs">{hubs}</div>')

    labels = []
    if article.post_type == "news":
        labels.append("Новость")
    if article.is_translation:
        labels.append("Перевод")
    if labels:
        parts.append(
            '<div class="labels">'
            + "".join(f'<span class="label">{x}</span>' for x in labels)
            + "</div>"
        )
    if article.is_translation and (article.original_author or article.original_url):
        note = "Автор оригинала: " + escape(article.original_author or "")
        if article.original_url:
            note += f' · <a href="{escape(article.original_url)}">Оригинал</a>'
        parts.append(f'<div class="translation-note">{note}</div>')
    return "\n".join(parts)


def render_footer(article: Article) -> str:
    parts = []
    if article.tags:
        tags = ", ".join(
            f'<a href="https://habr.com/ru/search/?target_type=posts&amp;order=relevance&amp;q=%5B{escape(t)}%5D">'
            f"{escape(t)}</a>"
            for t in article.tags
        )
        parts.append(f'<div class="separated-list"><span class="caption">Теги:</span>{tags}</div>')
    if article.hubs:
        hubs = ", ".join(f'<a href="{escape(h.url)}">{escape(h.title)}</a>' for h in article.hubs)
        parts.append(f'<div class="separated-list"><span class="caption">Хабы:</span>{hubs}</div>')

    stats = article.statistics
    parts.append(
        '<div class="data-icons">'
        f'<span>Рейтинг <b class="{_score_class(stats.score)}">'
        f"{format_score(stats.score)}</b></span>"
        f"<span>Просмотры <b>{format_count(stats.views)}</b></span>"
        f"<span>В закладках <b>{stats.favorites}</b></span>"
        f"<span>Комментарии <b>{stats.comments}</b></span>"
        "</div>"
    )
    return "\n".join(parts)


def render_company(company: Company, image_src: ImageSrc) -> str:
    logo = image_src(company.logo_url) if company.logo_url else None
    logo_html = f'<img class="card-logo" src="{escape(logo or STUB_AVATAR)}" alt="">'
    counters = []
    if company.rating is not None:
        counters.append(
            '<div class="counter-rating"><div class="counter-value">'
            f'{format_number(company.rating)}</div><div class="counter-label">Рейтинг</div></div>'
        )
    if company.subscribers is not None:
        counters.append(
            f'<div><div class="counter-value">{format_count(company.subscribers)}</div>'
            '<div class="counter-label">Подписчики</div></div>'
        )
    sub = []
    if company.site_url:
        sub.append(f'<a href="{escape(company.site_url)}">Сайт</a>')
    for contact in company.contacts:
        if contact.url != company.site_url:
            sub.append(f'<a href="{escape(contact.url)}">{escape(contact.title)}</a>')
    details = " · ".join(x for x in (company.location, company.staff) if x)
    return (
        '<div class="card company-card">'
        f"{logo_html}"
        '<div class="card-main">'
        f'<a class="card-title" href="{escape(company.url)}">{escape(company.title)}</a>'
        + (
            f'<div class="card-desc">{escape(company.description)}</div>'
            if company.description
            else ""
        )
        + (f'<div class="card-sub">{" · ".join(sub)}</div>' if sub else "")
        + (f'<div class="card-sub">{escape(details)}</div>' if details else "")
        + "</div>"
        + (f'<div class="card-counters">{"".join(counters)}</div>' if counters else "")
        + "</div>"
    )


def render_user_card(user: User, image_src: ImageSrc, tz: tzinfo | None) -> str:
    counters = [
        '<div class="counter-karma"><div class="counter-value '
        f'{_score_class(user.karma)}">{format_number(user.karma)}</div>'
        '<div class="counter-label">Карма</div></div>',
        '<div class="counter-rating"><div class="counter-value">'
        f'{format_number(user.rating)}</div><div class="counter-label">Рейтинг</div></div>',
    ]
    name = escape(user.fullname or user.alias)
    sub = []
    if user.followers is not None:
        sub.append(
            f"{user.followers} {plural(user.followers, 'подписчик', 'подписчика', 'подписчиков')}"
        )
    if user.location:
        sub.append(escape(user.location))
    if user.registered:
        sub.append("На Хабре с " + escape(format_datetime(user.registered, tz).split(" в ")[0]))
    return (
        '<div class="card user-card">'
        + _avatar(user, image_src, "card-logo")
        + '<div class="card-main">'
        f'<a class="card-title" href="{escape(user.url)}">{name}</a>'
        f'<span class="card-alias">@{escape(user.alias)}</span>'
        + (f'<div class="card-desc">{escape(user.speciality)}</div>' if user.speciality else "")
        + (f'<div class="card-sub">{" · ".join(sub)}</div>' if sub else "")
        + "</div>"
        f'<div class="card-counters">{"".join(counters)}</div>'
        "</div>"
    )


def render_comment(
    comment: Comment, image_src: ImageSrc, tz: tzinfo | None, *, expand: bool = False
) -> str:
    head = [_avatar(comment.author, image_src), _user_link(comment.author)]
    if comment.is_post_author:
        head.append('<span class="author-badge">автор</span>')
    if comment.published:
        head.append(f"<time>{escape(format_datetime(comment.published, tz))}</time>")

    if comment.deleted and not comment.html:
        body = (
            '<div class="comment-body comment-deleted">'
            "НЛО прилетело и опубликовало эту надпись здесь</div>"
        )
    else:
        body = (
            '<div class="comment-body">'
            f"{prepare_fragment(comment.html, image_src, expand_spoilers=expand)}</div>"
        )

    score_cls = _score_class(comment.score)
    score = f'<span class="score {score_cls}">{format_score(comment.score)}</span>'
    foot = f'<div class="comment-foot">Рейтинг{score}</div>'
    html = [
        f'<div class="comment-thread" id="comment_{escape(comment.id)}">',
        f'<div class="comment"><div class="comment-head">{"".join(head)}</div>{body}{foot}</div>',
    ]
    if comment.children:
        flat = " flat" if comment.level + 1 >= MAX_INDENT_LEVEL else ""
        html.append(f'<div class="comment-children{flat}">')
        html.extend(
            render_comment(child, image_src, tz, expand=expand) for child in comment.children
        )
        html.append("</div>")
    html.append("</div>")
    return "".join(html)


def render_comments(
    article: Article, image_src: ImageSrc, tz: tzinfo | None, *, expand: bool = False
) -> str:
    total = article.comments_total
    title = f'<h2 class="comments-title">Комментарии<span class="count">{total}</span></h2>'
    if not article.comments:
        return title + '<div class="comment-deleted">Комментариев пока нет</div>'
    return title + "".join(
        render_comment(c, image_src, tz, expand=expand) for c in article.comments
    )


def render_article(
    article: Article,
    image_src: ImageSrc = identity_src,
    *,
    tz: tzinfo | None = None,
    saved_at: datetime | None = None,
    include_comments: bool = True,
    for_print: bool = False,
) -> str:
    """Return the full HTML page of the article with embedded styles.

    ``for_print`` prepares the page for printing to PDF: spoilers are expanded.
    """
    saved_at = saved_at or datetime.now().astimezone()
    blocks = [
        '<article class="block">',
        render_header(article, image_src, tz),
        f'<div class="article-body">'
        f"{prepare_fragment(article.body_html, image_src, expand_spoilers=for_print)}</div>",
        render_footer(article),
        "</article>",
    ]
    cards = []
    if article.company:
        cards.append(render_company(article.company, image_src))
    cards.append(render_user_card(article.author, image_src, tz))
    blocks.append(f'<section class="block authors">{"".join(cards)}</section>')
    if include_comments:
        comments = render_comments(article, image_src, tz, expand=for_print)
        blocks.append(f'<section class="block comments">{comments}</section>')
    blocks.append(
        f'<div class="saved-from">Сохранено с <a href="{escape(article.url)}">'
        f"{escape(article.url)}</a> · "
        f"{escape(format_datetime(saved_at, tz))}</div>"
    )
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{escape(article.lang)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(article.title)} / Хабр</title>\n"
        f'<link rel="canonical" href="{escape(article.url)}">\n'
        f"<style>\n{stylesheet()}\n</style>\n</head>\n<body>\n"
        f'<div class="page">\n{"".join(blocks)}\n</div>\n</body>\n</html>\n'
    )
