"""Conversion of Habr API responses into models, and article HTML sanitizing."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from lxml import etree
from lxml import html as lhtml

from .models import Article, Comment, Company, Contact, Hub, Statistics, User
from .url import absolutize, canonical_article_url

# Service elements Habr inserts into the article body that carry no meaning.
_JUNK_XPATH = (
    "//div[contains(concat(' ', normalize-space(@class), ' '), ' code-explainer ')] "
    "| //script | //style | //noscript"
)
_IMG_DROP_ATTRS = ("srcset", "sizes", "loading", "decode", "decoding", "data-src", "data-blurred")
_EMBED_RE = re.compile(
    r"(?:youtube\.com|youtu\.be|vimeo\.com|rutube\.ru|vk\.com/video|embedd\.srv\.habr\.com)"
)


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 date from the API. Empty and malformed values give ``None``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: Any) -> str:
    """Unescape HTML entities in the API "*Html" fields that actually contain no markup."""
    if not value:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", str(value))).strip()


def _location(data: Mapping[str, Any] | None) -> str:
    if not data:
        return ""
    parts = []
    for key in ("city", "region", "country"):
        item = data.get(key) or {}
        title = item.get("title")
        if title and title not in parts:
            parts.append(title)
    # The region is usually redundant when a city is present.
    if len(parts) == 3:
        parts.pop(1)
    return ", ".join(parts)


# --- HTML ------------------------------------------------------------------------


def _fragment_root(markup: str) -> etree._Element:
    wrapper = lhtml.fragment_fromstring(markup or "", create_parent="div")
    # The article body arrives wrapped in <div xmlns="http://www.w3.org/1999/xhtml">.
    children = list(wrapper)
    if len(children) == 1 and children[0].tag == "div" and not (wrapper.text or "").strip():
        only = children[0]
        if only.get("xmlns") or not only.attrib:
            only.attrib.pop("xmlns", None)
            return only
    return wrapper


def _replace_with_link(element: etree._Element, url: str, caption: str) -> None:
    paragraph = lhtml.Element("p", {"class": "habr-embed"})
    link = lhtml.Element("a", {"href": url})
    link.text = caption
    paragraph.append(link)
    paragraph.tail = element.tail
    parent = element.getparent()
    if parent is not None:
        parent.replace(element, paragraph)


def clean_html(markup: str) -> str:
    """Normalize the HTML of an article or comment.

    * removes service blocks (the "explain code" button, scripts);
    * makes links and image URLs absolute and resolves lazy loading;
    * replaces embedded players (iframe) with links, since they cannot be saved offline.
    """
    if not markup or not markup.strip():
        return ""
    root = _fragment_root(markup)

    for junk in root.xpath(_JUNK_XPATH):
        junk.drop_tree()

    for img in root.iter("img"):
        src = img.get("data-src") or img.get("src")
        src = absolutize(src)
        if src:
            img.set("src", src)
        for attr in _IMG_DROP_ATTRS:
            img.attrib.pop(attr, None)

    for anchor in root.iter("a"):
        href = anchor.get("href")
        if href and not href.startswith(("#", "mailto:")):
            anchor.set("href", absolutize(href) or href)
        anchor.attrib.pop("style", None)

    for frame in list(root.xpath("//iframe | //div[@data-src][contains(@class, 'iframe')]")):
        url = absolutize(frame.get("data-src") or frame.get("src")) or ""
        if not url:
            frame.drop_tree()
            continue
        caption = "Встроенное видео: " if _EMBED_RE.search(url) else "Встроенный контент: "
        _replace_with_link(frame, url, caption + url)

    for elem in root.iter():
        if isinstance(elem.tag, str):
            elem.attrib.pop("xmlns", None)

    parts = [html.escape(root.text)] if root.text else []
    parts.extend(lhtml.tostring(child, encoding="unicode") for child in root)
    return "".join(parts).strip()


def image_urls(markup: str) -> list[str]:
    """Return all image URLs in an HTML fragment (without duplicates, in order of appearance)."""
    if not markup:
        return []
    root = lhtml.fragment_fromstring(markup, create_parent="div")
    seen: dict[str, None] = {}
    for img in root.iter("img"):
        src = img.get("src")
        if src and not src.startswith("data:"):
            seen.setdefault(src, None)
    return list(seen)


# --- Models ----------------------------------------------------------------------


def parse_user(
    data: Mapping[str, Any] | None, card: Mapping[str, Any] | None = None
) -> User | None:
    """Build a user from the short description and (optionally) the full card."""
    if not data and not card:
        return None
    data = data or {}
    card = card or {}
    merged: dict[str, Any] = {**data, **{k: v for k, v in card.items() if v is not None}}

    score_stats = merged.get("scoreStats") or {}
    counters = merged.get("counterStats") or {}
    follow = merged.get("followStats") or {}
    workplace = [w.get("title", "") for w in merged.get("workplace") or [] if w.get("title")]

    return User(
        alias=merged.get("alias") or "",
        fullname=_text(merged.get("fullname")),
        avatar_url=absolutize(merged.get("avatarUrl")),
        speciality=_text(merged.get("speciality")),
        karma=score_stats.get("score"),
        karma_votes=score_stats.get("votesCount"),
        rating=merged.get("rating"),
        rating_position=merged.get("ratingPos"),
        followers=follow.get("followersCount"),
        registered=parse_datetime(merged.get("registerDateTime")),
        last_activity=parse_datetime(merged.get("lastActivityDateTime")),
        location=_location(merged.get("location")),
        workplace=workplace,
        articles_count=counters.get("postCount"),
        comments_count=counters.get("commentCount"),
        deleted=bool(merged.get("deleted")),
    )


def parse_company(card: Mapping[str, Any] | None, hub: Hub | None = None) -> Company | None:
    """Build a company from the ``/companies/<alias>/card`` card."""
    if not card and not hub:
        return None
    card = card or {}
    stats = card.get("statistics") or {}
    contacts = [
        Contact(title=_text(c.get("title")) or c.get("url", ""), url=c["url"])
        for c in card.get("contacts") or []
        if c.get("url")
    ]
    return Company(
        alias=card.get("alias") or (hub.alias if hub else ""),
        title=_text(card.get("titleHtml")) or (hub.title if hub else ""),
        description=_text(card.get("descriptionHtml")),
        logo_url=absolutize(card.get("imageUrl")),
        rating=stats.get("rating"),
        subscribers=stats.get("subscribersCount"),
        site_url=card.get("siteUrl"),
        legal_name=_text(card.get("legalName")),
        location=_location(card.get("location")),
        staff=_text(card.get("staffNumber")),
        contacts=contacts,
    )


def parse_hubs(items: list[Mapping[str, Any]] | None) -> list[Hub]:
    return [
        Hub(
            alias=item.get("alias", ""),
            title=_text(item.get("titleHtml") or item.get("title")),
            is_profiled=bool(item.get("isProfiled")),
            is_corporative=item.get("type") == "corporative",
        )
        for item in items or []
    ]


def parse_comments(data: Mapping[str, Any] | None) -> list[Comment]:
    """Build the comment tree from the flat ``comments`` dictionary.

    The order of root threads comes from ``threads`` (as on the site), the order of replies from
    ``children``. Without ``threads``, roots are sorted by time.
    """
    raw: Mapping[str, Mapping[str, Any]] = (data or {}).get("comments") or {}
    nodes: dict[str, Comment] = {}
    for cid, item in raw.items():
        author = parse_user(item.get("author")) if item.get("author") else None
        status = item.get("status") or "published"
        nodes[str(cid)] = Comment(
            id=str(cid),
            author=author,
            html=clean_html(item.get("message") or ""),
            published=parse_datetime(item.get("timePublished")),
            edited=parse_datetime(item.get("timeChanged")),
            score=int(item.get("score") or 0),
            votes=int(item.get("votesCount") or 0),
            level=int(item.get("level") or 0),
            is_post_author=bool(item.get("isPostAuthor") or item.get("isAuthor")),
            deleted=author is None or status != "published",
        )

    attached: set[str] = set()
    for cid, item in raw.items():
        for child_id in item.get("children") or []:
            child = nodes.get(str(child_id))
            if child is not None and str(child_id) not in attached:
                nodes[str(cid)].children.append(child)
                attached.add(str(child_id))
    # Replies not listed in the parent's children that still reference it.
    for cid, item in raw.items():
        parent_id = item.get("parentId")
        if parent_id and str(cid) not in attached and str(parent_id) in nodes:
            nodes[str(parent_id)].children.append(nodes[str(cid)])
            attached.add(str(cid))

    threads = [str(t) for t in (data or {}).get("threads") or [] if str(t) in nodes]
    roots = [nodes[t] for t in threads]
    orphans = [c for cid, c in nodes.items() if cid not in attached and cid not in threads]
    orphans.sort(key=lambda c: (c.published is None, c.published or datetime.min))
    roots.extend(orphans)

    def fix_levels(comment: Comment, level: int) -> None:
        comment.level = level
        for child in comment.children:
            fix_levels(child, level + 1)

    for root in roots:
        fix_levels(root, 0)
    return roots


_COMPLEXITY = {"low": "Простой", "medium": "Средний", "high": "Сложный"}


def parse_article(
    data: Mapping[str, Any],
    *,
    comments: Mapping[str, Any] | None = None,
    author_card: Mapping[str, Any] | None = None,
    company_card: Mapping[str, Any] | None = None,
    url: str | None = None,
) -> Article:
    """Build an :class:`Article` from API responses."""
    article_id = str(data.get("id") or "")
    if not article_id or "textHtml" not in data:
        raise ValueError("The API response does not look like a Habr article")

    lang = data.get("lang") or "ru"
    hubs = parse_hubs(data.get("hubs"))
    corp_hub = next((h for h in hubs if h.is_corporative), None)
    company = parse_company(company_card, corp_hub) if (company_card or corp_hub) else None

    stats = data.get("statistics") or {}
    statistics = Statistics(
        score=int(stats.get("score") or 0),
        votes=int(stats.get("votesCount") or 0),
        votes_plus=int(stats.get("votesCountPlus") or 0),
        votes_minus=int(stats.get("votesCountMinus") or 0),
        views=int(stats.get("readingCount") or 0),
        favorites=int(stats.get("favoritesCount") or 0),
        comments=int(stats.get("commentsCount") or 0),
    )

    original_author = original_url = None
    is_translation = False
    for label in data.get("postLabels") or []:
        if label.get("type") == "translation":
            is_translation = True
            extra = label.get("data") or {}
            original_author = extra.get("originalAuthorName")
            original_url = extra.get("originalUrl")

    author = parse_user(data.get("author"), author_card) or User(alias="unknown")

    return Article(
        id=article_id,
        url=url or canonical_article_url(article_id, lang),
        title=_text(data.get("titleHtml")),
        body_html=clean_html(data.get("textHtml") or ""),
        author=author,
        published=parse_datetime(data.get("timePublished")),
        lang=lang,
        post_type=data.get("postType") or "article",
        reading_time=data.get("readingTime"),
        complexity=_COMPLEXITY.get(data.get("complexity") or ""),
        hubs=hubs,
        tags=[_text(t.get("titleHtml")) for t in data.get("tags") or [] if t.get("titleHtml")],
        statistics=statistics,
        company=company,
        comments=parse_comments(comments),
        is_translation=is_translation,
        original_author=original_author,
        original_url=original_url,
    )
