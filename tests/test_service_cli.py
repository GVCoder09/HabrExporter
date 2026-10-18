import pytest

from conftest import API, IMG_FIGURE, FakeResponse
from habr_exporter import __version__, cli, i18n
from habr_exporter.client import ArticleNotFoundError
from habr_exporter.exporters import ExportFormat, ExportOptions
from habr_exporter.images import ImageMode
from habr_exporter.models import Article, User
from habr_exporter.service import download_images, export_article, load_article, suggest_filename
from habr_exporter.url import InvalidArticleUrlError


def test_load_article_full(client, fake_session):
    events = []
    article = load_article(
        "https://habr.com/ru/companies/acme/articles/1000001/",
        client,
        lambda m, d, t: events.append((m, d, t)),
    )
    assert article.url == "https://habr.com/ru/companies/acme/articles/1000001/"
    assert article.company.rating == 467.81
    assert article.author.followers == 22
    assert article.comments_total == 7
    assert events[-1] == ("Статья загружена", 4, 4)


def test_load_article_tolerates_missing_extras(client, fake_session):
    del fake_session.routes[f"{API}/articles/1000001/comments/"]
    fake_session.routes[f"{API}/users/test_author/card/"] = FakeResponse(500)
    del fake_session.routes[f"{API}/companies/acme/card/"]
    article = load_article("1000001", client)
    assert article.comments == []
    assert article.author.followers is None
    assert article.company.title == "Блог компании ACME"
    assert article.url == "https://habr.com/ru/articles/1000001/"


def test_load_article_errors(client):
    with pytest.raises(InvalidArticleUrlError):
        load_article("https://example.com/", client)
    with pytest.raises(ArticleNotFoundError):
        load_article("42", client)


def test_download_images_reports_progress(client, article):
    events = []
    store = download_images(article, client, lambda m, d, t: events.append(m))
    assert store.get(IMG_FIGURE) is not None
    assert events[-1].startswith("Изображения:")


def test_suggest_filename():
    article = Article(
        id="7",
        url="",
        title='Как: "кавычки" / слэши? <и> *звёзды*.',
        body_html="",
        author=User(alias="a"),
        published=None,
    )
    assert suggest_filename(article, ExportFormat.PDF) == "Как кавычки слэши и звёзды.pdf"
    article.title = "..."
    assert suggest_filename(article, ExportFormat.DOCX) == "habr-7.docx"
    article.title = "x" * 300
    assert len(suggest_filename(article, ExportFormat.HTML)) == 120 + len(".html")


@pytest.mark.parametrize("fmt", list(ExportFormat))
def test_export_article_downloads_images_and_fixes_extension(tmp_path, client, article, fmt, qapp):
    target = tmp_path / "sub" / "article"
    result = export_article(article, target, ExportOptions(fmt, ImageMode.EMBED), client=client)
    assert result == tmp_path / "sub" / f"article{fmt.extension}"
    assert result.stat().st_size > 1000


def test_export_article_link_mode_skips_downloads(tmp_path, client, fake_session, article):
    export_article(
        article,
        tmp_path / "a.html",
        ExportOptions(ExportFormat.HTML, ImageMode.LINK),
        client=client,
    )
    assert not any("habrastorage" in url for url in fake_session.requests)


# --- CLI ----------------------------------------------------------------------------


@pytest.fixture
def patched_client(monkeypatch, client):
    monkeypatch.setattr("habr_exporter.client.HabrClient", lambda *a, **k: client)
    return client


def test_cli_exports_docx(tmp_path, patched_client, capsys, qapp):
    code = cli.main(["1000001", "-f", "docx", "-i", "link", "-o", str(tmp_path), "--no-comments"])
    out, err = capsys.readouterr()
    assert code == 0
    saved = list(tmp_path.glob("*.docx"))
    assert len(saved) == 1
    assert str(saved[0]) in out
    assert "Loading the article" in err


def test_cli_exports_pdf_to_file(tmp_path, patched_client, capsys, qapp):
    target = tmp_path / "out.pdf"
    assert cli.main(["1000001", "-o", str(target), "-q"]) == 0
    assert target.read_bytes().startswith(b"%PDF")
    assert capsys.readouterr().err == ""


def test_cli_reports_errors(tmp_path, patched_client, capsys):
    assert cli.main(["https://example.com/x", "-f", "html", "-o", str(tmp_path)]) == 1
    assert "Error" in capsys.readouterr().err


def test_cli_parser_defaults():
    args = cli.build_parser().parse_args(["1"])
    assert args.format == "pdf"
    assert args.images == "embed"
    assert not args.no_comments


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_cli_restores_language(tmp_path, patched_client):
    i18n.set_language(i18n.Language.RU)
    cli.main(["1000001", "-f", "json", "-o", str(tmp_path), "-q"])
    assert i18n.language() is i18n.Language.RU
