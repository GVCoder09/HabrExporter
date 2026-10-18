"""Batch export: a user's bookmarks, all articles of an author, or links from a text file.

Articles are saved to a folder one by one through the same :func:`~.service.load_article` and
:func:`~.service.export_article` as in single-article mode. File names start with the article id
(``1094766 - Title.pdf``), so a repeated run skips what is already saved without touching the
network, which makes incremental backups possible. A failure on one article does not stop the
export; at the end an ``index.html`` listing the articles is written to the folder.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

from .client import ArticleNotFoundError, HabrClient, HabrError
from .exporters import ExportFormat, ExportOptions, PdfExporter
from .formatting import format_datetime, plural
from .i18n import tr, trn
from .parser import _text, parse_datetime
from .service import Progress, download_images, export_article, load_article, suggest_filename
from .url import InvalidArticleUrlError, canonical_article_url, parse_article_id

log = logging.getLogger(__name__)

#: Default delay between Habr API requests, in seconds.
DEFAULT_DELAY = 1.0
#: Page size of a publication list (the API accepts at most 100).
PER_PAGE = 100
INDEX_NAME = "index.html"

#: Prints a PDF in the Qt main thread: ``(html, path)``. Needed by the GUI, which runs batches in a
#: worker thread.
PdfPrint = Callable[[str, Path], object]
Cancelled = Callable[[], bool]

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _noop(_message: str, _done: int, _total: int) -> None:
    pass


def _never() -> bool:
    return False


class SourceKind(StrEnum):
    """Where articles come from."""

    BOOKMARKS = "bookmarks"
    AUTHOR = "author"
    FILE = "file"

    @property
    def label(self) -> str:
        return tr(f"batch.kind_tip.{self.value}")


@dataclass(slots=True, frozen=True)
class BatchSource:
    """Article source: a user login (for bookmarks and author articles) or a file path."""

    kind: SourceKind
    value: str

    def describe(self) -> str:
        if self.kind is SourceKind.FILE:
            return f"ссылки из файла {Path(self.value).name}"
        alias = normalize_alias(self.value)
        what = "закладки" if self.kind is SourceKind.BOOKMARKS else "статьи"
        return f"{what} @{alias}"


@dataclass(slots=True)
class ArticleRef:
    """Article from a source list. Title, author and date are known if the list provided them."""

    id: str
    url: str
    title: str = ""
    author: str = ""
    published: datetime | None = None
    #: The file line does not look like an article link, so the article will not be loaded.
    error: str = ""


class ItemStatus(StrEnum):
    SAVED = "saved"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class BatchItem:
    """Result for a single article."""

    ref: ArticleRef
    status: ItemStatus
    path: Path | None = None
    error: str = ""


@dataclass(slots=True)
class BatchReport:
    """Result of a batch export."""

    source: BatchSource
    folder: Path
    format: ExportFormat
    total: int = 0
    items: list[BatchItem] = field(default_factory=list)
    cancelled: bool = False
    index_path: Path | None = None

    def count(self, status: ItemStatus) -> int:
        return sum(1 for item in self.items if item.status is status)

    @property
    def saved(self) -> int:
        return self.count(ItemStatus.SAVED)

    @property
    def skipped(self) -> int:
        return self.count(ItemStatus.SKIPPED)

    @property
    def failed(self) -> int:
        return self.count(ItemStatus.FAILED)

    def localized_summary(self) -> str:
        """Return a summary in the current UI language, for the window and the CLI."""
        parts = [trn("batch.summary.saved", self.saved)]
        if self.skipped:
            parts.append(trn("batch.summary.skipped", self.skipped))
        if self.failed:
            parts.append(tr("batch.summary.failed", n=self.failed))
        if self.cancelled:
            parts.append(tr("batch.summary.left", n=self.total - len(self.items)))
        return ", ".join(parts)

    def summary(self) -> str:
        """Return a Russian summary for ``index.html``.

        Example: ``Сохранено: 3, пропущено (уже были в папке): 2, ошибок: 1``.
        """
        text = (
            f"Сохранено: {self.saved}, пропущено (уже были в папке): {self.skipped}, "
            f"ошибок: {self.failed}"
        )
        if self.cancelled:
            left = self.total - len(self.items)
            text += f"; выгрузка отменена, не обработано: {left}"
        return text


# --- Sources -------------------------------------------------------------------------


def normalize_alias(value: str) -> str:
    """Extract a login from ``alias``, ``@alias`` or a profile link ``https://habr.com/ru/users/alias/``."""
    text = (value or "").strip()
    if "/" in text:
        parts = [
            p for p in urlsplit(text if "://" in text else "https://" + text).path.split("/") if p
        ]
        if "users" in parts and parts.index("users") + 1 < len(parts):
            text = parts[parts.index("users") + 1]
    text = text.lstrip("@")
    if not _ALIAS_RE.match(text):
        raise ValueError(tr("error.bad_alias", value=repr(value)))
    return text


def _ref_from_listing(article_id: str, data: dict) -> ArticleRef:
    return ArticleRef(
        id=article_id,
        url=canonical_article_url(article_id, data.get("lang") or "ru"),
        title=_text(data.get("titleHtml")),
        author=(data.get("author") or {}).get("alias") or "",
        published=parse_datetime(data.get("timePublished")),
    )


def _iter_user_list(
    client: HabrClient, alias: str, *, bookmarks: bool, news: bool, progress: Progress
) -> Iterator[ArticleRef]:
    """Yield the publications of one user list, page by page."""
    label = tr(
        "progress.list.bookmarks"
        if bookmarks
        else ("progress.list.news" if news else "progress.list.articles")
    )
    page, pages = 1, 1
    while page <= pages:
        try:
            data = client.get_user_articles(
                alias, page, bookmarks=bookmarks, news=news, per_page=PER_PAGE
            )
        except ArticleNotFoundError as exc:
            # The API returns an empty list as 404 "Page is out of range" and an unknown login as
            # 404 "not found".
            if "out of range" in exc.api_message.lower():
                return
            raise HabrError(tr("error.user_not_found", alias=alias)) from exc
        pages = int(data.get("pagesCount") or 0)
        refs = data.get("publicationRefs")
        refs = refs if isinstance(refs, dict) else {}
        # The overall "N of M" counter counts articles, not list pages, so progress is indeterminate
        # here.
        progress(tr("progress.list.page", label=label, alias=alias, page=page, pages=pages), 0, 0)
        for article_id in data.get("publicationIds") or []:
            yield _ref_from_listing(str(article_id), refs.get(str(article_id)) or {})
        page += 1


def read_links_file(path: str | Path) -> list[ArticleRef]:
    """Read links from a text file: one per line, skipping blank lines and ``#`` comments."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise HabrError(tr("error.read_file", path=path, reason=exc.strerror or exc)) from exc
    refs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            refs.append(ArticleRef(id=parse_article_id(line), url=line))
        except InvalidArticleUrlError as exc:
            refs.append(ArticleRef(id="", url=line, error=str(exc)))
    return refs


def iter_source(
    source: BatchSource, client: HabrClient, progress: Progress = _noop
) -> Iterator[ArticleRef]:
    """Yield source articles in the order Habr shows them (for a file, in line order)."""
    if source.kind is SourceKind.FILE:
        yield from read_links_file(source.value)
        return
    alias = normalize_alias(source.value)
    if source.kind is SourceKind.BOOKMARKS:
        yield from _iter_user_list(client, alias, bookmarks=True, news=False, progress=progress)
    else:
        yield from _iter_user_list(client, alias, bookmarks=False, news=False, progress=progress)
        yield from _iter_user_list(client, alias, bookmarks=False, news=True, progress=progress)


def list_articles(
    source: BatchSource,
    client: HabrClient | None = None,
    *,
    limit: int | None = None,
    progress: Progress = _noop,
    cancelled: Cancelled = _never,
) -> list[ArticleRef]:
    """Return the full list of source articles without duplicates, newest first.

    Habr returns bookmarks in the order they were added, and an author's articles and news as two
    separate lists, so the list is re-sorted by publication date. ``limit`` takes the first N in
    Habr's order (the most recently added bookmarks) and only then sorts them; otherwise fetching
    ten articles would require downloading every page of the list. Links from a file keep their
    line order because their dates are unknown until the article is loaded.
    """
    client = client or HabrClient()
    refs: list[ArticleRef] = []
    seen: set[str] = set()
    if limit is not None and limit <= 0:
        return refs
    for ref in iter_source(source, client, progress):
        if cancelled():
            break
        key = ref.id or ref.url
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
        if limit is not None and len(refs) >= limit:
            break
    return refs if source.kind is SourceKind.FILE else newest_first(refs)


def _newest_key(ref: ArticleRef) -> tuple[bool, float]:
    return ref.published is None, -ref.published.timestamp() if ref.published else 0.0


def newest_first(refs: list[ArticleRef]) -> list[ArticleRef]:
    """Sort by publication date, newest first; undated articles go last in their original order."""
    return sorted(refs, key=_newest_key)


# --- Files ---------------------------------------------------------------------------


def article_filename(article_id: str, title_filename: str) -> str:
    """Build a file name like ``1094766 - Title.pdf``.

    The leading id prevents collisions and lets already saved articles be skipped.
    """
    return f"{article_id} - {title_filename}"


def _existing_re(fmt: ExportFormat | None) -> re.Pattern[str]:
    ext = (
        re.escape(fmt.extension)
        if fmt
        else r"\.(?:" + "|".join(f.value for f in ExportFormat) + ")"
    )
    return re.compile(rf"^(\d+) - (.+?){ext}$", re.IGNORECASE)


def scan_existing(folder: Path, fmt: ExportFormat | None) -> dict[str, Path]:
    """Map ids of articles already saved in ``folder`` to their files.

    ``fmt=None`` means any format.
    """
    if not folder.is_dir():
        return {}
    pattern = _existing_re(fmt)
    found: dict[str, Path] = {}
    for path in sorted(folder.iterdir()):
        match = pattern.match(path.name)
        if match and path.is_file():
            found.setdefault(match.group(1), path)
    return found


def _title_from_filename(path: Path) -> str:
    match = _existing_re(None).match(path.name)
    return match.group(2) if match else path.stem


# --- Export --------------------------------------------------------------------------


def _replace_with_retry(source: Path, target: Path, attempts: int = 20) -> None:
    """``source.replace(target)``, tolerating a short-lived lock on Windows.

    Qt WebEngine may still hold the freshly printed PDF for a moment (and antivirus scanners
    may do the same), which makes the rename fail with ``PermissionError`` (WinError 32).
    """
    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def _save_one(
    ref: ArticleRef,
    folder: Path,
    options: ExportOptions,
    *,
    client: HabrClient,
    progress: Progress,
    print_pdf: PdfPrint | None,
) -> Path:
    article = load_article(ref.url, client, progress)
    ref.title = article.title
    ref.author = article.author.alias
    ref.published = article.published
    target = folder / article_filename(ref.id, suggest_filename(article, options.format))
    # Write to a temporary file so an interrupted export never leaves a broken "saved" file.
    partial = target.with_name("~" + target.name)
    try:
        if options.format is ExportFormat.PDF and print_pdf is not None:
            images = (
                download_images(article, client, progress) if options.downloads_images else None
            )
            progress(tr("progress.pdf_layout"), 0, 0)
            html = PdfExporter().render(article, options, images)
            progress(tr("progress.pdf_print"), 0, 0)
            print_pdf(html, partial)
        else:
            saved_prefix = tr("status.saved", path="")

            def quiet(message: str, done: int, total: int) -> None:
                # The final "Saved: <path>" from export_article would show the temporary file name.
                if not message.startswith(saved_prefix):
                    progress(message, done, total)

            export_article(article, partial, options, client=client, progress=quiet)
        _replace_with_retry(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def run_batch(
    source: BatchSource,
    folder: str | Path,
    options: ExportOptions,
    *,
    client: HabrClient | None = None,
    delay: float = DEFAULT_DELAY,
    limit: int | None = None,
    progress: Progress = _noop,
    cancelled: Cancelled = _never,
    print_pdf: PdfPrint | None = None,
    on_item: Callable[[BatchItem, int, int], None] | None = None,
    on_start: Callable[[ArticleRef, int, int], None] | None = None,
) -> BatchReport:
    """Save every source article to ``folder`` and write ``index.html`` there.

    * ``delay`` is the pause between Habr API requests, to avoid overloading the site;
    * ``progress(message, done, total)`` reports overall "N of M" progress;
    * ``cancelled()`` is checked before each article; whatever is saved stays in the folder;
    * ``print_pdf`` is a custom PDF printer (the GUI prints in the main thread). Without it PDFs are
      printed synchronously, so the function must be called from the main thread with a
      ``QApplication`` created;
    * ``on_item(item, n, total)`` is called after each article;
    * ``on_start(ref, n, total)`` is called before an article is loaded (not for already saved
      ones).

    A failure to get the list (no such user, no file) raises :class:`HabrError`. A failure on one
    article goes into the report and the export continues.
    """
    client = client or HabrClient()
    folder = Path(folder)
    report = BatchReport(source=source, folder=folder, format=options.format)

    with client.throttled(delay):
        progress(tr("progress.listing", what=_describe_ui(source)), 0, 0)
        refs = list_articles(source, client, limit=limit, progress=progress, cancelled=cancelled)
        report.total = len(refs)
        report.cancelled = cancelled()  # cancelled while the list was being collected
        folder.mkdir(parents=True, exist_ok=True)
        existing = scan_existing(folder, options.format)

        for number, ref in enumerate(refs if not report.cancelled else [], 1):
            if cancelled():
                report.cancelled = True
                break
            prefix = tr("progress.n_of", number=number, total=report.total)

            def sub(
                message: str, _done: int, _total: int, prefix: str = prefix, number: int = number
            ) -> None:
                progress(f"{prefix} · {message}", number - 1, report.total)

            if ref.error:
                item = BatchItem(ref, ItemStatus.FAILED, error=ref.error)
            elif ref.id in existing:
                path = existing[ref.id]
                ref.title = ref.title or _title_from_filename(path)
                item = BatchItem(ref, ItemStatus.SKIPPED, path=path)
            else:
                progress(f"{prefix} · {ref.title or ref.url}", number - 1, report.total)
                if on_start is not None:
                    on_start(ref, number, report.total)
                try:
                    path = _save_one(
                        ref, folder, options, client=client, progress=sub, print_pdf=print_pdf
                    )
                    item = BatchItem(ref, ItemStatus.SAVED, path=path)
                except KeyboardInterrupt:
                    report.cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001 - one failed article does not stop the export
                    log.warning("Article %s was not saved: %s", ref.url, exc)
                    item = BatchItem(
                        ref, ItemStatus.FAILED, error=str(exc) or exc.__class__.__name__
                    )
            report.items.append(item)
            progress(
                f"{prefix} · {tr(_STATUS_KEYS[item.status])}: {ref.title or ref.url}",
                number,
                report.total,
            )
            if on_item is not None:
                on_item(item, number, report.total)

    report.index_path = write_index(report)
    return report


def retry_item(
    report: BatchReport,
    item: BatchItem,
    options: ExportOptions,
    *,
    client: HabrClient | None = None,
    progress: Progress = _noop,
    print_pdf: PdfPrint | None = None,
) -> BatchItem:
    """Save an article from the report again (usually a failed one) and update ``index.html``.

    The new result replaces ``item`` in ``report.items``; a failure goes into the result again
    instead of raising.
    """
    client = client or HabrClient()
    ref = item.ref
    if ref.error:
        result = BatchItem(ref, ItemStatus.FAILED, error=ref.error)
    else:
        try:
            path = _save_one(
                ref, report.folder, options, client=client, progress=progress, print_pdf=print_pdf
            )
            result = BatchItem(ref, ItemStatus.SAVED, path=path)
        except Exception as exc:  # noqa: BLE001 - the error is shown in the log row
            log.warning("Article %s was not saved: %s", ref.url, exc)
            result = BatchItem(ref, ItemStatus.FAILED, error=str(exc) or exc.__class__.__name__)
    position = next((i for i, existing in enumerate(report.items) if existing is item), None)
    if position is None:
        report.items.append(result)
    else:
        report.items[position] = result
    report.index_path = write_index(report)
    return result


_STATUS_KEYS = {
    ItemStatus.SAVED: "progress.status.saved",
    ItemStatus.SKIPPED: "progress.status.skipped",
    ItemStatus.FAILED: "progress.status.failed",
}


def _describe_ui(source: BatchSource) -> str:
    """Describe the source for progress messages in the UI language.

    ``describe`` is for index.html.
    """
    if source.kind is SourceKind.FILE:
        return tr("progress.what.file", name=Path(source.value).name)
    key = (
        "progress.what.bookmarks" if source.kind is SourceKind.BOOKMARKS else "progress.what.author"
    )
    return tr(key, alias=normalize_alias(source.value))


# --- index.html ----------------------------------------------------------------------

_INDEX_CSS = """
body{font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#333;
background:#f0f0f0;margin:0}
main{max-width:980px;margin:24px auto;background:#fff;padding:24px 32px;border-radius:8px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px}
.meta{color:#929ca5;font-size:13px;margin-bottom:16px}
ol{padding-left:2.2em}li{margin:6px 0}
a{color:#548eaa;text-decoration:none}a:hover{color:#3d6f87;text-decoration:underline}
.sub{color:#929ca5;font-size:13px}.err{color:#d04e4e;font-size:13px}
"""


def _index_entry(ref: ArticleRef, path: Path | None) -> str:
    title = escape(ref.title or (path.name if path else ref.url))
    link = f'<a href="{escape(quote(path.name))}">{title}</a>' if path else title
    details = [f"@{escape(ref.author)}"] if ref.author else []
    if ref.published:
        details.append(escape(format_datetime(ref.published)))
    if ref.id:
        details.append(f'<a href="{escape(canonical_article_url(ref.id))}">на Хабре</a>')
    return f'<li>{link}<br><span class="sub">{" · ".join(details)}</span></li>'


def render_index(report: BatchReport) -> str:
    """Render the folder index: articles from this run, previously saved files and errors."""
    done = [i for i in report.items if i.status is not ItemStatus.FAILED and i.path is not None]
    done.sort(key=lambda i: _newest_key(i.ref))
    failed = [i for i in report.items if i.status is ItemStatus.FAILED]
    listed = {i.path.name for i in done if i.path}
    # Files from earlier runs with a different source, format or --limit stay in the index too.
    pattern = _existing_re(None)
    earlier = [p for p in report.folder.iterdir() if pattern.match(p.name) and p.name not in listed]
    # These files have no dates, but Habr article ids grow over time: a larger id is newer.
    earlier.sort(key=lambda p: (-int(pattern.match(p.name).group(1)), p.name))

    title = f"Habr Exporter — {report.source.describe()}"
    count = len(done)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title><style>{_INDEX_CSS}</style></head><body><main>",
        f"<h1>{escape(title)}</h1>",
        f'<div class="meta">{count} {plural(count, "статья", "статьи", "статей")} в формате '
        f"{report.format.value.upper()} · {escape(report.summary())} · "
        f"обновлено {escape(format_datetime(datetime.now().astimezone()))}</div>",
        "<ol>",
        *(_index_entry(item.ref, item.path) for item in done),
        "</ol>",
    ]
    if failed:
        parts.append("<h2>Не удалось сохранить</h2><ol>")
        for item in failed:
            ref = item.ref
            name = escape(ref.title or ref.url)
            link = f'<a href="{escape(ref.url)}">{name}</a>' if ref.id else name
            parts.append(f'<li>{link}<br><span class="err">{escape(item.error)}</span></li>')
        parts.append("</ol>")
    if earlier:
        parts.append("<h2>Другие статьи в папке</h2><ol>")
        parts.extend(
            f'<li><a href="{escape(quote(p.name))}">{escape(p.name)}</a></li>' for p in earlier
        )
        parts.append("</ol>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


def write_index(report: BatchReport) -> Path:
    path = report.folder / INDEX_NAME
    report.folder.mkdir(parents=True, exist_ok=True)
    path.write_text(render_index(report), encoding="utf-8")
    return path
