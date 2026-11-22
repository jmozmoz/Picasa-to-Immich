"""ImmichAPI response parsing and pagination, without any network access."""
import pytest

import picasa_to_immich as p2i


class ScriptedAPI(p2i.ImmichAPI):
    """ImmichAPI whose request() returns prepared responses and records the calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs.get("json"), kwargs.get("params")))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def page(ids, **extra):
    return {"assets": {"items": [{"id": i} for i in ids], **extra}}


@pytest.mark.parametrize(("payload", "expected"), [
    (page(["a"], nextPage="2"), (["a"], True)),
    (page(["a"], nextPage=None), (["a"], False)),
    ({"items": [{"id": "a"}], "hasNextPage": True}, (["a"], True)),
    ({"items": [{"id": "a"}], "hasMore": True}, (["a"], True)),
    ({"assets": [{"id": "a"}]}, (["a"], False)),
    ({"data": {"items": []}}, ([], False)),
])
def test_extract_items(payload, expected):
    items, has_more = p2i.ImmichAPI._extract_items(payload)
    assert ([i["id"] for i in items], has_more) == expected


@pytest.mark.parametrize("payload", [[], {"unexpected": 1}])
def test_extract_items_rejects_unknown_shapes(payload):
    with pytest.raises(RuntimeError):
        p2i.ImmichAPI._extract_items(payload)


def test_list_assets_follows_pages_and_removes_duplicates():
    api = ScriptedAPI([page(["a", "b"], nextPage="2"), page(["b", "c"], nextPage=None)])
    assert [a["id"] for a in api.list_assets(page_size=2)] == ["a", "b", "c"]
    assert [call[2] for call in api.calls] == [
        {"size": 2, "withExif": True, "page": 1},
        {"size": 2, "withExif": True, "page": 2},
    ]


def test_list_assets_follows_cursor():
    api = ScriptedAPI([{**page(["a"]), "nextCursor": "c1"}, page(["b"])])
    assert [a["id"] for a in api.list_assets(page_size=1)] == ["a", "b"]
    assert api.calls[1][2] == {"size": 1, "withExif": True, "cursor": "c1"}


def test_list_assets_follows_nested_cursor():
    api = ScriptedAPI([page(["a"], nextCursor="c1"), page(["b"])])
    assert [a["id"] for a in api.list_assets()] == ["a", "b"]


def test_empty_page_claiming_more_results_is_an_error():
    api = ScriptedAPI([page([], nextPage="2")])
    with pytest.raises(RuntimeError, match="incomplete"):
        api.list_assets()


def test_album_asset_ids():
    api = ScriptedAPI([page(["a", "b"], nextPage="2"), page(["c"])])
    assert api.album_asset_ids("alb", page_size=2) == {"a", "b", "c"}
    assert api.calls[0][2] == {"size": 2, "albumIds": ["alb"], "page": 1}


@pytest.mark.parametrize(("response", "expected"), [
    ([{"id": "x"}], [{"id": "x"}]),
    ({"items": [{"id": "y"}]}, [{"id": "y"}]),
])
def test_list_albums(response, expected):
    api = ScriptedAPI([response])
    assert api.list_albums() == expected
    assert api.calls == [("GET", "albums", None, {"isOwned": "true"})]


def test_list_albums_rejects_unknown_shape():
    with pytest.raises(RuntimeError):
        ScriptedAPI(["text"]).list_albums()


def test_server_version_falls_back_to_second_endpoint():
    api = ScriptedAPI([RuntimeError("404"), {"version": "1.2.3"}])
    assert api.server_version() == "1.2.3"


def test_server_version_unavailable():
    assert ScriptedAPI([RuntimeError("a"), RuntimeError("b")]).server_version() is None


def test_album_write_requests():
    api = ScriptedAPI([{"id": "new"}, None])
    api.create_album("Trip", ["a"])
    api.add_album_assets("alb", ["b"])
    assert api.calls == [
        ("POST", "albums", {"albumName": "Trip", "assetIds": ["a"]}, None),
        ("PUT", "albums/alb/assets", {"ids": ["b"]}, None),
    ]


class FakeResponse:
    def __init__(self, status, body=b"", json_value=None, headers=None):
        self.status_code = status
        self.ok = status < 400
        self.content = body
        self.text = body.decode()
        self.headers = headers or {}
        self._json = json_value

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def api_with(response, debug=False):
    pytest.importorskip("requests")
    api = p2i.ImmichAPI("http://server:2283/", "secret-key", timeout=5, debug=debug)
    api.session = FakeSession(response)
    return api


def test_request_builds_url_and_returns_json():
    api = api_with(FakeResponse(200, b"{}", {"ok": True}))
    assert api.request("GET", "/server/version") == {"ok": True}
    assert api.session.calls == [("GET", "http://server:2283/api/server/version", {"timeout": 5})]


def test_request_returns_none_for_empty_body():
    assert api_with(FakeResponse(204)).request("PUT", "x") is None


def test_request_raises_with_details_on_http_error():
    api = api_with(FakeResponse(400, b"bad", {"message": "bad"}, {"X-Correlation-ID": "c-1"}))
    with pytest.raises(RuntimeError, match=r"HTTP 400.*bad.*c-1"):
        api.request("POST", "albums")


def test_debug_logging_never_contains_api_key(caplog):
    api = api_with(FakeResponse(200, b"{}", {}), debug=True)
    with caplog.at_level("INFO", logger="picasa_to_immich"):
        api.request("POST", "search/metadata", json={"size": 1})
    assert "API -> POST http://server:2283/api/search/metadata" in caplog.text
    assert "secret-key" not in caplog.text


def test_top_level_list_keeps_pagination_flag():
    # Regression: a bare top-level list used to drop "hasNextPage" and truncate the inventory.
    api = ScriptedAPI([{"assets": [{"id": "a"}], "hasNextPage": True}, {"assets": [{"id": "b"}]}])
    assert [a["id"] for a in api.list_assets()] == ["a", "b"]
