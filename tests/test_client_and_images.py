import pytest
import requests

from conftest import (
    AVATAR_AUTHOR,
    IMG_BROKEN,
    IMG_FIGURE,
    IMG_FORMULA,
    IMG_LAZY,
    SVG,
    FakeResponse,
    FakeSession,
    make_png,
)
from habr_exporter.client import USER_AGENT, ArticleNotFoundError, HabrClient, HabrError
from habr_exporter.images import ImageData, ImageStore, raster_for_docx, sniff_mime


def test_client_fetches_json(client, fake_session):
    data = client.get_article("1000001")
    assert data["id"] == "1000001"
    assert client.get_comments("1000001")["threads"] == ["1", "5", "6"]
    assert client.get_user_card("test_author")["alias"] == "test_author"
    assert client.get_company_card("acme")["alias"] == "acme"
    assert fake_session.requests[0] == "https://habr.com/kek/v2/articles/1000001/"


def test_client_errors():
    session = FakeSession(
        {
            "https://habr.com/kek/v2/articles/1/": FakeResponse(500),
            "https://habr.com/kek/v2/articles/2/": FakeResponse(200, content=b"<html>"),
            "https://habr.com/kek/v2/articles/3/": FakeResponse(200, payload=["list"]),
        }
    )
    client = HabrClient(session=session)  # type: ignore[arg-type]
    with pytest.raises(HabrError, match="HTTP 500"):
        client.get_article("1")
    with pytest.raises(HabrError, match="JSON"):
        client.get_article("2")
    with pytest.raises(HabrError, match="Неожиданный"):
        client.get_article("3")
    with pytest.raises(ArticleNotFoundError):
        client.get_article("404")


def test_client_network_errors():
    class Boom(FakeSession):
        def get(self, *args, **kwargs):
            raise requests.ConnectionError("нет сети")

    client = HabrClient(session=Boom({}))  # type: ignore[arg-type]
    with pytest.raises(HabrError, match="Сетевая ошибка"):
        client.get_article("1")
    with pytest.raises(HabrError, match="Не удалось скачать"):
        client.download("https://x/y.png")


def test_client_download(client):
    resource = client.download(IMG_FIGURE)
    assert resource.content_type == "image/png"
    assert resource.content.startswith(b"\x89PNG")
    with pytest.raises(HabrError, match="HTTP 500"):
        client.download(IMG_BROKEN)


def test_default_session_has_retries_and_user_agent():
    client = HabrClient()
    assert client.session.headers["User-Agent"] == USER_AGENT
    assert client.session.get_adapter("https://habr.com").max_retries.total == 3


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (make_png(), "image/png"),
        (make_png(fmt="JPEG"), "image/jpeg"),
        (make_png(fmt="GIF"), "image/gif"),
        (make_png(fmt="WEBP"), "image/webp"),
        (make_png(fmt="BMP"), "image/bmp"),
        (SVG, "image/svg+xml"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
    ],
)
def test_sniff_mime(data, expected):
    assert sniff_mime(data) == expected


def test_sniff_mime_fallbacks():
    assert sniff_mime(b"????", "image/x-icon") == "image/x-icon"
    assert sniff_mime(b"????", "text/html") == "application/octet-stream"


def test_image_store_fetch_all(client):
    store = ImageStore(client, max_workers=2)
    calls = []
    store.fetch_all(
        [IMG_FIGURE, IMG_FIGURE, IMG_BROKEN, AVATAR_AUTHOR], lambda d, t: calls.append((d, t))
    )
    assert calls[0] == (0, 3)
    assert calls[-1] == (3, 3)
    assert len(store) == 2
    assert IMG_FIGURE in store
    assert store.failed == {IMG_BROKEN}
    figure = store.get(IMG_FIGURE)
    assert (figure.width, figure.height) == (1560, 600)
    assert figure.data_uri.startswith("data:image/png;base64,")
    # A repeated call downloads nothing again.
    calls.clear()
    store.fetch_all([IMG_FIGURE, IMG_BROKEN], lambda d, t: calls.append((d, t)))
    assert calls == [(0, 0)]


def test_image_store_requires_client():
    with pytest.raises(RuntimeError):
        ImageStore().fetch_all(["x"])


def test_raster_for_docx_native_and_converted(qapp, client):
    store = ImageStore(client)
    store.fetch_all([IMG_FIGURE, IMG_LAZY, IMG_FORMULA])
    png = raster_for_docx(store.get(IMG_FIGURE))
    assert png.data == store.get(IMG_FIGURE).data  # PNG is inserted as is
    webp = raster_for_docx(store.get(IMG_LAZY))
    assert webp.data.startswith(b"\x89PNG")
    assert (webp.width, webp.height) == (300, 200)
    svg = raster_for_docx(store.get(IMG_FORMULA))
    assert svg is not None
    assert svg.data.startswith(b"\x89PNG")
    assert (svg.width, svg.height) == (40, 12)


def test_raster_for_docx_garbage():
    assert raster_for_docx(ImageData("u", b"not an image", "application/octet-stream")) is None
    assert raster_for_docx(ImageData("u", b"<svg broken", "image/svg+xml")) is None
