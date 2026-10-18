"""Command-line interface.

The CLI always speaks English: its messages, progress and errors use the English UI dictionary.
Saved files stay in Russian, as they are Habr content.

* single article: ``habr-exporter-cli URL -f pdf -o article.pdf``;
* batch: ``habr-exporter-cli --bookmarks USER -f html -o ./backup/`` (also ``--author``,
  ``--from-file``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, i18n
from .batch import DEFAULT_DELAY, BatchSource, SourceKind
from .exporters import ExportFormat, ExportOptions
from .images import ImageMode

if TYPE_CHECKING:
    from .client import HabrClient
    from .service import Progress


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="habr-exporter-cli",
        description="Save a Habr article as PDF, HTML, DOCX, EPUB, FB2, Markdown or JSON. "
        "With --bookmarks, --author or --from-file, save every article of the source "
        "into a folder.",
    )
    parser.add_argument("url", nargs="?", help="article link or number")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    beta = ", ".join(f.value for f in ExportFormat if f.beta)
    parser.add_argument(
        "-f",
        "--format",
        choices=[f.value for f in ExportFormat],
        default=ExportFormat.PDF.value,
        help=f"output format (default: pdf; in beta: {beta})",
    )
    parser.add_argument(
        "-i",
        "--images",
        choices=[m.value for m in ImageMode],
        default=ImageMode.EMBED.value,
        help="embed: put images into the file, link: refer to the originals (default: embed)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="file or folder path; a folder in batch mode (default: the current folder)",
    )
    parser.add_argument("--no-comments", action="store_true", help="do not save comments")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not print progress")

    batch = parser.add_argument_group("batch export")
    batch.add_argument(
        "--bookmarks",
        metavar="USER",
        help="all public bookmarks of a user (username or profile link)",
    )
    batch.add_argument(
        "--author",
        metavar="USER",
        help="all articles and news of an author (username or profile link)",
    )
    batch.add_argument(
        "--from-file", metavar="PATH", type=Path, help="links from a text file, one per line"
    )
    batch.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        metavar="SEC",
        help=f"pause between requests to Habr, in seconds (default: {DEFAULT_DELAY:g})",
    )
    batch.add_argument(
        "--limit", type=int, metavar="N", help="save at most the first N articles of the source"
    )
    return parser


def _batch_source(args: argparse.Namespace) -> BatchSource | None:
    if args.bookmarks:
        return BatchSource(SourceKind.BOOKMARKS, args.bookmarks)
    if args.author:
        return BatchSource(SourceKind.AUTHOR, args.author)
    if args.from_file:
        return BatchSource(SourceKind.FILE, str(args.from_file))
    return None


@contextmanager
def _english() -> Iterator[None]:
    """Switch the process to English for the duration of a CLI run.

    Tests call :func:`main` in-process.
    """
    previous = i18n.language()
    i18n.set_language(i18n.Language.EN)
    try:
        yield
    finally:
        i18n.set_language(previous)


def main(argv: list[str] | None = None) -> int:
    with _english():
        return _main(argv)


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    chosen = [args.url, args.bookmarks, args.author, args.from_file]
    if sum(value is not None for value in chosen) != 1:
        parser.error(
            "specify exactly one of: an article link, --bookmarks, --author or --from-file"
        )
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    from .client import HabrClient
    from .service import export_article, load_article, suggest_filename

    options = ExportOptions(
        format=ExportFormat(args.format),
        image_mode=ImageMode(args.images),
        include_comments=not args.no_comments,
    )

    def progress(message: str, done: int, total: int) -> None:
        if not args.quiet:
            suffix = f" [{done}/{total}]" if total else ""
            print(f"{message}{suffix}", file=sys.stderr)

    app = None
    if options.format.needs_qt:
        from .app import create_application

        app = create_application(["habr-exporter-cli"])

    client = HabrClient()
    source = _batch_source(args)
    if source is not None:
        try:
            return _run_batch(source, args, options, client, progress)
        finally:
            del app

    try:
        article = load_article(args.url, client, progress)
        output: Path = args.output or Path.cwd()
        if output.is_dir() or not output.suffix:
            output = output / suggest_filename(article, options.format)
        path = export_article(article, output, options, client=client, progress=progress)
    except Exception as exc:  # noqa: BLE001 - the CLI reports the error and exits with code 1
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        del app
    print(path)
    return 0


def _run_batch(
    source: BatchSource,
    args: argparse.Namespace,
    options: ExportOptions,
    client: HabrClient,
    progress: Progress,
) -> int:
    """Run a batch export and return the exit code.

    Exit codes: 0 - no errors, 2 - some articles were not saved, 1 - failed to get the list,
    130 - interrupted (Ctrl+C).
    """
    from .batch import ItemStatus, run_batch

    try:
        report = run_batch(
            source,
            args.output or Path.cwd(),
            options,
            client=client,
            delay=args.delay,
            limit=args.limit,
            # The message text already contains "N of M", so no counter in brackets is needed.
            progress=lambda message, _done, _total: progress(message, 0, 0),
        )
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI reports the error and exits with code 1
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for item in report.items:
        if item.status is ItemStatus.FAILED:
            print(f"Error: {item.ref.url}: {item.error}", file=sys.stderr)
    print(report.localized_summary(), file=sys.stderr)
    print(report.index_path)
    if report.cancelled:
        return 130
    return 2 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
