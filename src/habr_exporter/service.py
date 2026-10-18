"""High-level operations: load an article by link and save it to a file."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from .client import HabrClient, HabrError
from .exporters import ExportFormat, ExportOptions, collect_image_urls, get_exporter
from .i18n import tr
from .images import ImageStore
from .models import Article
from .parser import parse_article
from .url import parse_article_id

log = logging.getLogger(__name__)

#: ``(message, done, total)``; ``total == 0`` means indeterminate progress.
Progress = Callable[[str, int, int], None]


def _noop(_message: str, _done: int, _total: int) -> None:
    pass


def load_article(url: str, client: HabrClient | None = None, progress: Progress = _noop) -> Article:
    """Download the article, its comments, and the author and company cards.

    The cards are optional: if they cannot be fetched, the article is still saved, just with less
    detailed author and company blocks.
    """
    client = client or HabrClient()
    article_id = parse_article_id(url)

    progress(tr("progress.article"), 0, 4)
    data = client.get_article(article_id)

    progress(tr("progress.comments"), 1, 4)
    try:
        comments = client.get_comments(article_id)
    except HabrError as exc:
        log.warning("Comments were not loaded: %s", exc)
        comments = None

    progress(tr("progress.author"), 2, 4)
    author_card = None
    alias = (data.get("author") or {}).get("alias")
    if alias:
        try:
            author_card = client.get_user_card(alias)
        except HabrError as exc:
            log.warning("Author profile was not loaded: %s", exc)

    progress(tr("progress.company"), 3, 4)
    company_card = None
    corp = next((h for h in data.get("hubs") or [] if h.get("type") == "corporative"), None)
    if corp and corp.get("alias"):
        try:
            company_card = client.get_company_card(corp["alias"])
        except HabrError as exc:
            log.warning("Company profile was not loaded: %s", exc)

    progress(tr("progress.loaded"), 4, 4)
    page_url = url.strip() if url.strip().startswith("http") else None
    return parse_article(
        data, comments=comments, author_card=author_card, company_card=company_card, url=page_url
    )


def download_images(
    article: Article, client: HabrClient | None = None, progress: Progress = _noop
) -> ImageStore:
    """Download all images of the article and comments, and the avatars."""
    store = ImageStore(client or HabrClient())
    urls = collect_image_urls(article)
    store.fetch_all(
        urls,
        lambda done, total: progress(tr("progress.images", done=done, total=total), done, total),
    )
    return store


_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def suggest_filename(article: Article, fmt: ExportFormat) -> str:
    """Return a safe file name built from the article title."""
    title = _FORBIDDEN.sub(" ", article.title)
    title = re.sub(r"\s+", " ", title).strip(" .")[:120].rstrip(" .")
    return f"{title or 'habr-' + article.id}{fmt.extension}"


def export_article(
    article: Article,
    path: str | Path,
    options: ExportOptions,
    *,
    client: HabrClient | None = None,
    images: ImageStore | None = None,
    progress: Progress = _noop,
) -> Path:
    """Save a loaded article. PDF requires a ``QApplication`` in the main thread."""
    path = Path(path)
    if path.suffix.lower() != options.format.extension:
        path = path.with_name(path.name + options.format.extension)
    path.parent.mkdir(parents=True, exist_ok=True)

    if options.downloads_images and images is None:
        images = download_images(article, client, progress)

    progress(tr("progress.saving", fmt=options.format.value.upper()), 0, 0)
    result = get_exporter(options.format).export(article, path, options, images)
    progress(tr("status.saved", path=result), 1, 1)
    return result
