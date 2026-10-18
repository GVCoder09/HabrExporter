from datetime import UTC, datetime

import pytest
from lxml import html as lhtml

from habr_exporter.parser import (
    clean_html,
    image_urls,
    parse_article,
    parse_comments,
    parse_company,
    parse_datetime,
    parse_user,
)


def test_parse_article_metadata(article):
    assert article.id == "1000001"
    assert article.title == "Тестовая статья: «всё» сразу"
    assert article.url == "https://habr.com/ru/articles/1000001/"
    assert article.published == datetime(2026, 3, 5, 9, 30, tzinfo=UTC)
    assert article.reading_time == 7
    assert article.complexity == "Средний"
    assert article.tags == ["python", "тестирование"]
    assert [h.alias for h in article.hubs] == ["acme", "python"]
    assert article.hubs[0].is_corporative
    assert article.hubs[0].url == "https://habr.com/ru/companies/acme/articles/"
    assert article.hubs[1].is_profiled
    assert article.hubs[1].url == "https://habr.com/ru/hubs/python/"
    assert article.statistics.views == 15400
    assert article.statistics.score == 17
    assert article.is_translation
    assert article.original_author == "Jane Doe"
    assert article.original_url == "https://example.com/original"


def test_author_merges_card(article):
    author = article.author
    assert author.alias == "test_author"
    assert author.speciality == "Инженер-тестировщик"  # from the card, not the short description
    assert author.karma == 51
    assert author.rating == 12.5
    assert author.followers == 22
    assert author.location == "Казань, Россия"
    assert author.workplace == ["ACME"]
    assert author.avatar_url == "https://habrastorage.org/getpro/habr/avatars/author.png"
    assert author.url == "https://habr.com/ru/users/test_author/"


def test_company(article):
    company = article.company
    assert company is not None
    assert company.title == "ACME"
    assert company.rating == 467.81
    assert company.subscribers == 25905
    assert company.logo_url == "https://habrastorage.org/getpro/habr/company/logo.png"
    assert company.location == "Москва, Россия"
    assert [c.title for c in company.contacts] == ["Сайт", "Telegram"]
    assert company.url == "https://habr.com/ru/companies/acme/"


def test_company_falls_back_to_hub_without_card(article_json):
    result = parse_article(article_json)
    assert result.company is not None
    assert result.company.title == "Блог компании ACME"
    assert result.company.rating is None
    assert parse_company(None) is None


def test_no_company_for_personal_article(article_json):
    article_json["hubs"] = [h for h in article_json["hubs"] if h["type"] != "corporative"]
    assert parse_article(article_json).company is None


def test_parse_article_rejects_non_article():
    with pytest.raises(ValueError, match="does not look like"):
        parse_article({"id": "1"})


def test_body_is_cleaned(article):
    body = article.body_html
    assert "code-explainer" not in body
    assert "<script" not in body
    assert "xmlns" not in body
    assert "srcset" not in body
    assert 'href="https://habr.com/ru/articles/1/"' in body
    assert "https://habrastorage.org/webt/lazy/picture.webp" in body
    assert "data:image/gif" not in body
    assert 'class="habr-embed"' in body
    assert "https://embedd.srv.habr.com/iframe/abc123" in body
    assert "Встроенное видео" in body


def test_clean_html_edge_cases():
    assert clean_html("") == ""
    assert clean_html("   ") == ""
    assert clean_html("просто текст") == "просто текст"
    out = clean_html('<iframe src="https://example.org/widget"></iframe><iframe></iframe>')
    assert "Встроенный контент: https://example.org/widget" in out
    assert out.count("habr-embed") == 1
    assert (
        clean_html('<p><a href="#anchor" style="x">a</a></p>') == '<p><a href="#anchor">a</a></p>'
    )


def test_image_urls_unique_in_order(article):
    urls = image_urls(article.body_html)
    assert urls[0].endswith("figure.png")
    assert len(urls) == len(set(urls)) == 4
    assert image_urls("") == []
    assert image_urls('<img src="data:image/png;base64,AA">') == []


def test_comment_tree(article):
    roots = article.comments
    assert [c.id for c in roots] == ["1", "5", "6"]
    first = roots[0]
    assert [c.id for c in first.children] == ["2", "3"]
    assert [c.id for c in first.children[0].children] == ["4"]
    assert first.children[0].is_post_author
    assert first.children[0].children[0].level == 2
    # A reply referenced only by parentId still ends up in the tree.
    assert [c.id for c in roots[2].children] == ["7"]
    deleted = roots[1]
    assert deleted.deleted
    assert deleted.author is None
    assert article.comments_total == 7
    assert [c.id for c in article.iter_comments()] == ["1", "2", "4", "3", "5", "6", "7"]


def test_comments_without_threads_sorted_by_time():
    data = {
        "comments": {
            "b": {
                "id": "b",
                "parentId": None,
                "timePublished": "2026-01-02T00:00:00+00:00",
                "message": "b",
                "author": {"alias": "x"},
                "children": [],
            },
            "a": {
                "id": "a",
                "parentId": None,
                "timePublished": "2026-01-01T00:00:00+00:00",
                "message": "a",
                "author": {"alias": "y"},
                "children": [],
            },
            "c": {
                "id": "c",
                "parentId": None,
                "timePublished": None,
                "message": "c",
                "author": {"alias": "z"},
                "children": [],
            },
        }
    }
    assert [c.id for c in parse_comments(data)] == ["a", "b", "c"]
    assert parse_comments(None) == []
    assert parse_comments({}) == []


def test_parse_user_and_dates():
    assert parse_user(None) is None
    user = parse_user({"alias": "u", "fullname": "&lt;Имя&gt;", "avatarUrl": None, "deleted": True})
    assert user.fullname == "<Имя>"
    assert user.deleted
    assert user.avatar_url is None
    assert parse_datetime(None) is None
    assert parse_datetime("garbage") is None
    assert parse_datetime("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)


def test_clean_html_keeps_wrapper_with_attributes():
    out = clean_html('<div class="note"><p>a</p></div>')
    tree = lhtml.fragment_fromstring(out, create_parent="div")
    assert tree[0].get("class") == "note"
