"""Tests for app.news.fetcher: ClsFetcher (with mocked HTTP)."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.news.fetcher import CLS_URL, ClsFetcher, NO_PROXY


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def empty_payload():
    return {"errno": 0, "data": {"roll_data": [], "vip": []}}


@pytest.fixture
def sample_payload():
    return {
        "errno": 0,
        "data": {
            "roll_data": [
                {"id": 1001, "title": "央行降准", "content": "降准0.5%",
                 "ctime": 1785234699, "type_name": "头条"},
            ],
            "vip": [
                {"id": 1002, "title": "GPT-5 发布", "content": "AI 大模型",
                 "ctime": 1785234700, "type_name": "电报解读"},
            ],
        },
    }


class TestClsFetcherInit:
    def test_initial_last_time_is_none(self):
        f = ClsFetcher()
        assert f._last_time is None

    def test_sign_optional(self):
        f1 = ClsFetcher()
        f2 = ClsFetcher(sign="abc123")
        assert f1._sign is None
        assert f2._sign == "abc123"


class TestClsFetcherFetch:
    def test_returns_empty_on_network_error(self):
        with patch("requests.get", side_effect=requests.RequestException("network down")):
            f = ClsFetcher()
            assert f.fetch() == []

    def test_returns_empty_on_invalid_json(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.side_effect = ValueError("not json")
        with patch("requests.get", return_value=resp):
            f = ClsFetcher()
            assert f.fetch() == []

    def test_returns_empty_on_error_errno(self):
        with patch("requests.get", return_value=_mock_response({"errno": -1, "msg": "fail"})):
            f = ClsFetcher()
            assert f.fetch() == []

    def test_returns_empty_on_empty_data(self, empty_payload):
        with patch("requests.get", return_value=_mock_response(empty_payload)):
            f = ClsFetcher()
            assert f.fetch() == []

    def test_returns_parsed_items(self, sample_payload):
        with patch("requests.get", return_value=_mock_response(sample_payload)):
            f = ClsFetcher()
            items = f.fetch()
            assert len(items) == 2
            titles = {i.title for i in items}
            assert "央行降准" in titles
            assert "GPT-5 发布" in titles

    def test_advances_last_time(self, sample_payload):
        with patch("requests.get", return_value=_mock_response(sample_payload)):
            f = ClsFetcher()
            assert f._last_time is None
            f.fetch()
            assert f._last_time == 1785234700

    def test_includes_sign_when_set(self):
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            f = ClsFetcher(sign="my-sign-value")
            f.fetch()
            params = mock_get.call_args.kwargs["params"]
            assert params["sign"] == "my-sign-value"

    def test_omits_sign_when_unset(self):
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            f = ClsFetcher()
            f.fetch()
            params = mock_get.call_args.kwargs["params"]
            assert "sign" not in params

    def test_passes_last_time_after_first_fetch(self, sample_payload):
        with patch("requests.get", return_value=_mock_response(sample_payload)):
            f = ClsFetcher()
            f.fetch()
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            f.fetch()
            params = mock_get.call_args.kwargs["params"]
            assert params["lastTime"] == "1785234700"

    def test_url_contains_required_params(self):
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            ClsFetcher().fetch()
            args = mock_get.call_args
            assert args[0][0] == CLS_URL
            params = args.kwargs["params"]
            assert params["app"] == "CailianpressWeb"
            assert params["name"] == "telegraphList"
            assert params["os"] == "web"
            assert params["sv"] == "8.7.9"

    def test_uses_no_proxy(self):
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            ClsFetcher().fetch()
            assert mock_get.call_args.kwargs["proxies"] == NO_PROXY

    def test_includes_referer_header(self):
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            ClsFetcher().fetch()
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert "cls.cn" in headers.get("referer", "")

    def test_includes_cookie_when_set(self):
        with patch("requests.get", return_value=_mock_response({"errno": 0, "data": {}})) as mock_get:
            f = ClsFetcher(cookie="session=abc")
            f.fetch()
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("cookie") == "session=abc"


class TestRawNewsHash:
    def test_hash_is_stable(self):
        payload = {"errno": 0, "data": {"roll_data": [
            {"id": 1, "title": "A", "content": "x", "ctime": 1, "type_name": ""}
        ]}}
        with patch("requests.get", return_value=_mock_response(payload)):
            item1 = ClsFetcher().fetch()[0]
        with patch("requests.get", return_value=_mock_response(payload)):
            item2 = ClsFetcher().fetch()[0]
        assert item1.hash == item2.hash

    def test_hash_different_content(self):
        payloads = [
            {"errno": 0, "data": {"roll_data": [
                {"id": 1, "title": "A", "content": "x", "ctime": 100, "type_name": ""}
            ]}},
            {"errno": 0, "data": {"roll_data": [
                {"id": 2, "title": "A", "content": "y", "ctime": 200, "type_name": ""}
            ]}},
        ]
        responses = [_mock_response(p) for p in payloads]
        with patch("requests.get", side_effect=responses):
            f = ClsFetcher()
            item1 = f.fetch()[0]
            item2 = f.fetch()[0]
        assert item1.hash != item2.hash

    def test_url_constructed_from_id(self):
        payload = {"errno": 0, "data": {"roll_data": [
            {"id": 999, "title": "x", "content": "y", "ctime": 1, "type_name": ""}
        ]}}
        with patch("requests.get", return_value=_mock_response(payload)):
            item = ClsFetcher().fetch()[0]
        assert item.url == "https://www.cls.cn/detail/999"

    def test_url_empty_when_no_id(self):
        payload = {"errno": 0, "data": {"roll_data": [
            {"id": 0, "title": "x", "content": "y", "ctime": 1, "type_name": ""}
        ]}}
        with patch("requests.get", return_value=_mock_response(payload)):
            item = ClsFetcher().fetch()[0]
        assert item.url == ""