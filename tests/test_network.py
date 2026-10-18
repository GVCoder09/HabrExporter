"""Checks against live Habr. Not run by default: ``pytest -m network``."""

import pytest

from habr_exporter.batch import BatchSource, SourceKind, list_articles, run_batch
from habr_exporter.client import HabrClient
from habr_exporter.exporters import ExportFormat, ExportOptions
from habr_exporter.images import ImageMode
from habr_exporter.service import export_article, load_article

pytestmark = pytest.mark.network

URL = "https://habr.com/ru/companies/k2tech/articles/1094766/"


@pytest.fixture(scope="module")
def live_article():
    client = HabrClient()
    return client, load_article(URL, client)


def test_live_article_parsed(live_article):
    _, article = live_article
    assert article.title
    assert article.author.alias
    assert article.company is not None
    assert article.body_html.count("<pre") > 0
    assert article.comments_total >= 0


@pytest.mark.parametrize("fmt", list(ExportFormat))
def test_live_export(tmp_path, live_article, fmt, qapp):
    client, article = live_article
    path = export_article(article, tmp_path / "live", ExportOptions(fmt), client=client)
    assert path.stat().st_size > 10_000


#: A user with many public bookmarks and their own articles and news.
LIVE_USER = "boomburum"


@pytest.mark.parametrize("kind", [SourceKind.BOOKMARKS, SourceKind.AUTHOR])
def test_live_user_lists(kind):
    refs = list_articles(BatchSource(kind, LIVE_USER), HabrClient(min_interval=1.0), limit=3)
    assert len(refs) == 3
    assert all(r.id.isdigit() and r.title and r.author for r in refs)


@pytest.mark.parametrize("fmt", [ExportFormat.PDF, ExportFormat.HTML])
def test_live_batch_bookmarks(tmp_path, fmt, qapp):
    options = ExportOptions(fmt, ImageMode.LINK)
    report = run_batch(BatchSource(SourceKind.BOOKMARKS, LIVE_USER), tmp_path, options, limit=2)
    assert (report.saved, report.failed) == (2, 0)
    again = run_batch(BatchSource(SourceKind.BOOKMARKS, LIVE_USER), tmp_path, options, limit=2)
    assert again.skipped == 2
