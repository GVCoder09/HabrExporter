"""Parsing of user-provided links to Habr articles."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .i18n import tr

_HOSTS = {"habr.com", "www.habr.com", "m.habr.com", "habrahabr.ru", "geektimes.ru", "geektimes.com"}

# /ru/articles/123/, /ru/companies/x/articles/123/, /ru/news/123/, /ru/posts/123/,
# /en/post/123/, /p/123/, /company/x/blog/123/, /ru/sandbox/123/ etc.
_ID_RE = re.compile(
    r"/(?:articles|article|news|posts|post|p|blog|sandbox|amp/publications)/(\d+)(?:/|$)",
)


class InvalidArticleUrlError(ValueError):
    """The link does not look like a link to a Habr publication."""


def parse_article_id(url: str) -> str:
    """Return the numeric article id from a link.

    Accepts full links (with or without a scheme) as well as a bare number.

    >>> parse_article_id("https://habr.com/ru/companies/k2tech/articles/1094766/")
    '1094766'
    >>> parse_article_id("1094766")
    '1094766'
    """
    text = (url or "").strip()
    if not text:
        raise InvalidArticleUrlError(tr("error.url_empty"))
    if text.isdigit():
        return text

    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host not in _HOSTS:
        raise InvalidArticleUrlError(tr("error.url_not_habr", url=repr(url)))

    match = _ID_RE.search(parts.path + ("" if parts.path.endswith("/") else "/"))
    if not match:
        raise InvalidArticleUrlError(tr("error.url_no_id", url=repr(url)))
    return match.group(1)


def canonical_article_url(article_id: str, lang: str = "ru") -> str:
    """Return the canonical article link for its id."""
    return f"https://habr.com/{lang}/articles/{article_id}/"


def absolutize(url: str | None, base: str = "https://habr.com") -> str | None:
    """Make protocol-relative and root-relative links absolute."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return url
