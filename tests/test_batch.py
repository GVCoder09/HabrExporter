import pytest

from conftest import API, BATCH_IDS, FakeResponse, listing_page, listing_url, out_of_range
from habr_exporter import batch, cli
from habr_exporter.batch import (
    ArticleRef,
    BatchItem,
    BatchReport,
    BatchSource,
    ItemStatus,
    SourceKind,
    list_articles,
    normalize_alias,
    read_links_file,
    render_index,
    retry_item,
    run_batch,
    scan_existing,
)
from habr_exporter.client import HabrClient, HabrError
from habr_exporter.exporters import ExportFormat, ExportOptions
from habr_exporter.images import ImageMode
from habr_exporter.parser import parse_datetime

BOOKMARKS = BatchSource(SourceKind.BOOKMARKS, "alice")
HTML_LINK = ExportOptions(ExportFormat.HTML, ImageMode.LINK)


def _article_requests(session, article_id):
    return [url for url in session.requests if url == f"{API}/articles/{article_id}/"]


# --- Sources -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "alice",
        "@alice",
        " alice ",
        "https://habr.com/ru/users/alice/",
        "habr.com/en/users/alice/bookmarks/articles/",
    ],
)
def test_normalize_alias(value):
    assert normalize_alias(value) == "alice"


@pytest.mark.parametrize("value", ["", "две части", "https://habr.com/ru/articles/1/", "a" * 65])
def test_normalize_alias_rejects_garbage(value):
    with pytest.raises(ValueError, match="логин"):
        normalize_alias(value)


def test_bookmarks_are_paginated(client, batch_session):
    events = []
    refs = list_articles(BOOKMARKS, client, progress=lambda m, d, t: events.append((m, d, t)))
    assert [r.id for r in refs] == list(BATCH_IDS)
    first = refs[0]
    assert first.title == "Статья «1000001»"
    assert first.author == "test_author"
    assert first.url == "https://habr.com/ru/articles/1000001/"
    assert first.published.year == 2026
    assert batch_session.requests == [
        listing_url("alice", 1, bookmarks=True),
        listing_url("alice", 2, bookmarks=True),
    ]
    assert events == [
        ("Закладки @alice: страница 1 из 2", 0, 0),
        ("Закладки @alice: страница 2 из 2", 0, 0),
    ]


def test_limit_stops_listing_early(client, batch_session):
    refs = list_articles(BOOKMARKS, client, limit=2)
    assert [r.id for r in refs] == list(BATCH_IDS[:2])
    assert listing_url("alice", 2, bookmarks=True) not in batch_session.requests


def test_cancel_stops_listing(client, batch_session):
    assert list_articles(BOOKMARKS, client, cancelled=lambda: True) == []


def test_author_articles_include_news_without_duplicates(client, fake_session):
    fake_session.routes[listing_url("bob", 1)] = listing_page(["1", "2"], 1)
    fake_session.routes[listing_url("bob", 1, news=True)] = listing_page(["2", "3"], 1)
    refs = list_articles(BatchSource(SourceKind.AUTHOR, "@bob"), client)
    assert [r.id for r in refs] == ["1", "2", "3"]


def test_empty_list_and_unknown_user(client, fake_session):
    fake_session.routes[listing_url("empty", 1, bookmarks=True)] = out_of_range()
    assert list_articles(BatchSource(SourceKind.BOOKMARKS, "empty"), client) == []
    with pytest.raises(HabrError, match="@nobody не найден"):
        list_articles(BatchSource(SourceKind.BOOKMARKS, "nobody"), client)


def test_listing_tolerates_odd_payload(client, fake_session):
    fake_session.routes[listing_url("odd", 1, bookmarks=True)] = FakeResponse(
        payload={"pagesCount": 1, "publicationIds": ["7"], "publicationRefs": []}
    )
    [ref] = list_articles(BatchSource(SourceKind.BOOKMARKS, "odd"), client)
    assert (ref.id, ref.title, ref.author, ref.published) == ("7", "", "", None)


def test_read_links_file(tmp_path):
    path = tmp_path / "links.txt"
    path.write_text(
        "﻿# мои статьи\n\nhttps://habr.com/ru/articles/1000001/\n  1000002  \nне ссылка\n1000001\n",
        encoding="utf-8",
    )
    refs = read_links_file(path)
    assert [(r.id, bool(r.error)) for r in refs] == [
        ("1000001", False),
        ("1000002", False),
        ("", True),
        ("1000001", False),
    ]
    assert refs[0].url == "https://habr.com/ru/articles/1000001/"
    deduped = list_articles(BatchSource(SourceKind.FILE, str(path)), HabrClient())
    assert [r.id for r in deduped] == ["1000001", "1000002", ""]
    with pytest.raises(HabrError, match="Не удалось прочитать"):
        read_links_file(tmp_path / "missing.txt")


def test_source_describe(tmp_path):
    assert BOOKMARKS.describe() == "закладки @alice"
    assert (
        BatchSource(SourceKind.AUTHOR, "https://habr.com/ru/users/bob/").describe() == "статьи @bob"
    )
    assert (
        BatchSource(SourceKind.FILE, str(tmp_path / "links.txt")).describe()
        == "ссылки из файла links.txt"
    )
    assert [k.label for k in SourceKind] == [
        "Закладки пользователя",
        "Все статьи автора",
        "Ссылки из файла",
    ]


# --- Request throttling --------------------------------------------------------------


def test_client_throttles_requests(monkeypatch, fake_session):
    clock = iter([100.0, 100.2, 101.0, 105.0, 105.0])
    sleeps = []
    monkeypatch.setattr("habr_exporter.client.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("habr_exporter.client.time.sleep", sleeps.append)
    client = HabrClient(session=fake_session)  # type: ignore[arg-type]
    client.get_user_card("test_author")  # no delay: min_interval = 0
    with client.throttled(1.0):
        assert client.min_interval == 1.0
        client.get_user_card("test_author")  # first request: nothing to wait for
        client.get_user_card("test_author")  # after 0.2 s, wait another 0.8 s
        client.get_user_card("test_author")  # after 4 s, no wait
    assert client.min_interval == 0
    assert sleeps == [pytest.approx(0.8)]


def test_run_batch_uses_delay(monkeypatch, client, batch_session, tmp_path):
    sleeps = []
    monkeypatch.setattr("habr_exporter.client.time.sleep", sleeps.append)
    run_batch(BOOKMARKS, tmp_path, HTML_LINK, client=client, delay=5.0, limit=1)
    assert sleeps
    assert all(0 < s <= 5.0 for s in sleeps)
    assert client.min_interval == 0


# --- Export --------------------------------------------------------------------------


def test_run_batch_saves_skips_and_continues_after_error(client, batch_session, tmp_path):
    batch_session.routes[f"{API}/articles/1000002/"] = FakeResponse(500)
    events, items = [], []
    report = run_batch(
        BOOKMARKS,
        tmp_path / "backup",
        HTML_LINK,
        client=client,
        delay=0,
        progress=lambda m, d, t: events.append((m, d, t)),
        on_item=lambda item, n, total: items.append((item.status, n, total)),
    )
    assert (report.total, report.saved, report.skipped, report.failed) == (3, 2, 0, 1)
    assert items == [(ItemStatus.SAVED, 1, 3), (ItemStatus.FAILED, 2, 3), (ItemStatus.SAVED, 3, 3)]
    assert report.summary() == "Сохранено: 2, пропущено (уже были в папке): 0, ошибок: 1"
    assert "HTTP 500" in report.items[1].error
    assert ("3 из 3 · сохранено: Статья номер 1000003", 3, 3) in events
    assert not any("~" in m for m, _, _ in events)

    names = sorted(p.name for p in (tmp_path / "backup").iterdir())
    assert names == [
        "1000001 - Тестовая статья «всё» сразу.html",
        "1000003 - Статья номер 1000003.html",
        "index.html",
    ]
    index = report.index_path.read_text(encoding="utf-8")
    assert "закладки @alice" in index
    assert 'href="1000001%20-%20' in index
    assert "Не удалось сохранить" in index
    assert "HTTP 500" in index

    # Repeated run: saved articles are skipped without requests, the failed one is retried.
    batch_session.requests.clear()
    batch_session.routes[f"{API}/articles/1000002/"] = FakeResponse(
        payload={**_article(batch_session), "id": "1000002"}
    )
    again = run_batch(BOOKMARKS, tmp_path / "backup", HTML_LINK, client=client, delay=0)
    assert (again.saved, again.skipped, again.failed) == (1, 2, 0)
    assert not _article_requests(batch_session, "1000001")
    assert _article_requests(batch_session, "1000002")
    assert (
        again.items[0].ref.title == "Статья «1000001»"
    )  # the skipped article's title comes from the bookmark list


def _article(session):
    return session.routes[f"{API}/articles/1000001/"]._payload


def test_other_format_is_not_skipped(client, batch_session, tmp_path):
    run_batch(BOOKMARKS, tmp_path, HTML_LINK, client=client, delay=0, limit=1)
    report = run_batch(
        BOOKMARKS,
        tmp_path,
        ExportOptions(ExportFormat.DOCX, ImageMode.LINK),
        client=client,
        delay=0,
        limit=1,
    )
    assert report.saved == 1
    assert set(scan_existing(tmp_path, ExportFormat.DOCX)) == {"1000001"}
    assert set(scan_existing(tmp_path, None)) == {"1000001"}
    assert scan_existing(tmp_path / "nope", None) == {}
    index = report.index_path.read_text(encoding="utf-8")
    assert "Другие статьи в папке" in index  # HTML version from the previous run
    assert ".html</a>" in index


def test_file_source_with_bad_line_and_skipped_title(client, batch_session, tmp_path):
    links = tmp_path / "links.txt"
    links.write_text(
        "https://habr.com/ru/articles/1000001/\nhttps://example.com/x\n1000003\n", encoding="utf-8"
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "1000003 - Старое имя.html").write_text("<html></html>", encoding="utf-8")
    report = run_batch(
        BatchSource(SourceKind.FILE, str(links)), out, HTML_LINK, client=client, delay=0
    )
    assert [i.status for i in report.items] == [
        ItemStatus.SAVED,
        ItemStatus.FAILED,
        ItemStatus.SKIPPED,
    ]
    assert "Это не ссылка на Хабр" in report.items[1].error
    assert report.items[2].ref.title == "Старое имя"
    assert report.items[0].ref.url == "https://habr.com/ru/articles/1000001/"


def test_cancel_keeps_saved_and_writes_index(client, batch_session, tmp_path):
    state = {"stop": False}

    def on_item(item, number, total):
        state["stop"] = True

    report = run_batch(
        BOOKMARKS,
        tmp_path,
        HTML_LINK,
        client=client,
        delay=0,
        cancelled=lambda: state["stop"],
        on_item=on_item,
    )
    assert report.cancelled
    assert report.saved == 1
    assert report.summary().endswith("выгрузка отменена, не обработано: 2")
    assert report.index_path.exists()


def test_keyboard_interrupt_cancels_and_cleans_partial_file(
    monkeypatch, client, batch_session, tmp_path
):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(batch, "export_article", interrupted)
    report = run_batch(BOOKMARKS, tmp_path, HTML_LINK, client=client, delay=0)
    assert report.cancelled
    assert report.items == []
    assert [p.name for p in tmp_path.iterdir()] == ["index.html"]


def test_custom_pdf_printer_and_partial_cleanup(client, batch_session, tmp_path):
    printed = []

    def print_pdf(html, path):
        printed.append(path.name)
        if len(printed) == 2:
            path.write_bytes(b"%PDF-broken")
            raise RuntimeError("принтер сломался")
        path.write_bytes(b"%PDF-1.4 fake")

    options = ExportOptions(ExportFormat.PDF, ImageMode.EMBED)
    report = run_batch(
        BOOKMARKS, tmp_path, options, client=client, delay=0, limit=2, print_pdf=print_pdf
    )
    assert printed[0].startswith("~1000001 - ")
    assert [i.status for i in report.items] == [ItemStatus.SAVED, ItemStatus.FAILED]
    assert report.items[1].error == "принтер сломался"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "1000001 - Тестовая статья «всё» сразу.pdf",
        "index.html",
    ]


def test_render_index_escapes_and_handles_bare_refs(tmp_path):
    report = BatchReport(BatchSource(SourceKind.FILE, "x.txt"), tmp_path, ExportFormat.PDF, total=2)
    report.items = [
        batch.BatchItem(
            ArticleRef("5", "u", title="<b>5</b>"),
            ItemStatus.SKIPPED,
            path=tmp_path / "5 - a&b.pdf",
        ),
        batch.BatchItem(
            ArticleRef("", "плохая строка", error="нет"), ItemStatus.FAILED, error="нет"
        ),
    ]
    html = render_index(report)
    assert "&lt;b&gt;5&lt;/b&gt;" in html
    assert 'href="5%20-%20a%26b.pdf"' in html
    assert "<li>плохая строка<br>" in html
    assert "1 статья в формате PDF" in html


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture
def patched_client(monkeypatch, client):
    monkeypatch.setattr("habr_exporter.client.HabrClient", lambda *a, **k: client)
    return client


def test_cli_bookmarks_html(tmp_path, patched_client, batch_session, capsys):
    code = cli.main(
        ["--bookmarks", "alice", "-f", "html", "-i", "link", "-o", str(tmp_path), "--delay", "0"]
    )
    out, err = capsys.readouterr()
    assert code == 0
    assert out.strip() == str(tmp_path / "index.html")
    assert "1 of 3 · " in err
    assert "Saved 3 articles" in err
    assert "[" not in err  # the "N of M" counter is already in the text
    assert len(list(tmp_path.glob("*.html"))) == 4


def test_cli_author_pdf_limit(tmp_path, patched_client, fake_session, capsys, qapp):
    fake_session.routes[listing_url("test_author", 1)] = listing_page(["1000001", "1000002"], 1)
    code = cli.main(
        ["--author", "test_author", "-o", str(tmp_path), "--limit", "1", "--delay", "0", "-q"]
    )
    err = capsys.readouterr().err
    assert code == 0
    [pdf] = tmp_path.glob("*.pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert err.strip() == "Saved 1 article"


def test_cli_from_file_with_errors_returns_2(tmp_path, patched_client, batch_session, capsys):
    links = tmp_path / "links.txt"
    links.write_text("1000001\nhttps://example.com/\n", encoding="utf-8")
    code = cli.main(
        [
            "--from-file",
            str(links),
            "-f",
            "html",
            "-i",
            "link",
            "-o",
            str(tmp_path / "o"),
            "--delay",
            "0",
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "Error: https://example.com/:" in err


def test_cli_batch_fatal_error_and_interrupt(monkeypatch, tmp_path, patched_client, capsys):
    assert (
        cli.main(["--bookmarks", "nobody", "-f", "html", "-o", str(tmp_path), "--delay", "0"]) == 1
    )
    assert "Error: User @nobody was not found on Habr" in capsys.readouterr().err

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(batch, "run_batch", interrupted)
    assert cli.main(["--bookmarks", "alice", "-f", "html", "-o", str(tmp_path)]) == 130
    assert "Interrupted" in capsys.readouterr().err


def test_cli_batch_cancelled_report_returns_130(monkeypatch, tmp_path, patched_client, capsys):
    def cancelled(source, folder, options, **kwargs):
        report = BatchReport(source, tmp_path, options.format, total=3, cancelled=True)
        report.index_path = tmp_path / "index.html"
        return report

    monkeypatch.setattr(batch, "run_batch", cancelled)
    assert cli.main(["--bookmarks", "alice", "-f", "html", "-o", str(tmp_path)]) == 130
    assert "not processed: 3" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "exactly one"),
        (["1000001", "--bookmarks", "alice"], "exactly one"),
        (["--bookmarks", "a", "--author", "b"], "exactly one"),
        (["--bookmarks", "a", "--delay", "-1"], "--delay"),
        (["--bookmarks", "a", "--limit", "0"], "--limit"),
    ],
)
def test_cli_batch_argument_errors(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_batch_defaults():
    args = cli.build_parser().parse_args(["--bookmarks", "alice"])
    assert args.url is None
    assert args.delay == batch.DEFAULT_DELAY == 1.0
    assert args.limit is None


# --- Order by date --------------------------------------------------------------------


def test_bookmarks_are_sorted_newest_first(client, fake_session):
    # Habr returns bookmarks in the order they were added, not published.
    dates = {
        "2": "2026-01-01T10:00:00+00:00",
        "1": "2026-05-01T10:00:00+00:00",
        "3": None,
        "4": "2025-12-31T23:00:00Z",
    }
    fake_session.routes[listing_url("carol", 1, bookmarks=True)] = listing_page(
        ["2", "1", "3", "4"], 1, dates
    )
    refs = list_articles(BatchSource(SourceKind.BOOKMARKS, "carol"), client)
    assert [r.id for r in refs] == ["1", "2", "4", "3"]  # undated ones go last
    # limit takes the most recently added bookmarks and only then sorts them.
    limited = list_articles(BatchSource(SourceKind.BOOKMARKS, "carol"), client, limit=2)
    assert [r.id for r in limited] == ["1", "2"]


def test_author_articles_and_news_are_merged_by_date(client, fake_session):
    fake_session.routes[listing_url("dave", 1)] = listing_page(
        ["10", "11"], 1, {"10": "2026-02-01T00:00:00Z", "11": "2025-02-01T00:00:00Z"}
    )
    fake_session.routes[listing_url("dave", 1, news=True)] = listing_page(
        ["12"], 1, {"12": "2025-06-01T00:00:00Z"}
    )
    refs = list_articles(BatchSource(SourceKind.AUTHOR, "dave"), client)
    assert [r.id for r in refs] == ["10", "12", "11"]


def test_index_is_sorted_by_date(tmp_path):
    def item(article_id, published):
        ref = ArticleRef(
            article_id, "u", title=f"Статья {article_id}", published=parse_datetime(published)
        )
        return batch.BatchItem(
            ref, ItemStatus.SAVED, path=tmp_path / f"{article_id} - Статья {article_id}.html"
        )

    for name in ("5 - старая.html", "900 - новая.pdf", "77 - средняя.docx"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    report = BatchReport(
        BatchSource(SourceKind.FILE, "x.txt"), tmp_path, ExportFormat.HTML, total=3
    )
    report.items = [
        item("1", "2024-01-01T00:00:00Z"),
        item("2", None),
        item("3", "2026-01-01T00:00:00Z"),
    ]
    html = render_index(report)
    positions = [html.index(f"Статья {i}</a>") for i in ("3", "1", "2")]
    assert positions == sorted(positions)
    others = [
        html.index(name) for name in ("900 - новая.pdf", "77 - средняя.docx", "5 - старая.html")
    ]
    assert others == sorted(others)  # earlier files, by descending article id


def test_on_start_called_only_for_downloads(client, batch_session, tmp_path):
    started = []
    run_batch(BOOKMARKS, tmp_path, HTML_LINK, client=client, delay=0, limit=1)
    on_start = lambda ref, n, total: started.append((ref.id, n, total))  # noqa: E731
    run_batch(BOOKMARKS, tmp_path, HTML_LINK, client=client, delay=0, on_start=on_start)
    assert started == [("1000002", 2, 3), ("1000003", 3, 3)]  # 1000001 was already in the folder


def test_retry_item_updates_report_and_index(client, batch_session, tmp_path):
    route = batch_session.routes[f"{API}/articles/1000002/"]
    batch_session.routes[f"{API}/articles/1000002/"] = FakeResponse(
        404, payload={"httpCode": 404, "message": "x"}
    )
    report = run_batch(BOOKMARKS, tmp_path, HTML_LINK, client=client, delay=0)
    failed = next(i for i in report.items if i.status is ItemStatus.FAILED)
    assert "Не удалось сохранить" in report.index_path.read_text(encoding="utf-8")

    again = retry_item(report, failed, HTML_LINK, client=client)
    assert again.status is ItemStatus.FAILED  # still 404
    assert any(i is again for i in report.items)
    assert all(i is not failed for i in report.items)  # replaced, not appended
    assert len(report.items) == 3

    batch_session.routes[f"{API}/articles/1000002/"] = route
    saved = retry_item(report, again, HTML_LINK, client=client)
    assert saved.status is ItemStatus.SAVED
    assert saved.path.exists()
    assert (report.saved, report.failed) == (3, 0)
    assert "Не удалось сохранить" not in report.index_path.read_text(encoding="utf-8")


def test_retry_item_bad_line_and_unknown_item(client, tmp_path):
    report = BatchReport(BatchSource(SourceKind.FILE, "x.txt"), tmp_path, ExportFormat.HTML)
    bad = BatchItem(
        ArticleRef(id="", url="мусор", error="Не похоже на ссылку"), ItemStatus.FAILED, error="x"
    )
    result = retry_item(report, bad, HTML_LINK, client=client)
    assert result.status is ItemStatus.FAILED
    assert result.error == "Не похоже на ссылку"
    assert report.items == [result]  # the item was not in the report, so it is appended
    assert report.index_path.exists()
