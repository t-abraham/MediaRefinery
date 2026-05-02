from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from mediarefinery.config import load_config
from mediarefinery.immich import (
    SYNTHETIC_IMAGE_PREVIEW_BYTES,
    AssetRef,
    HttpImmichClient,
    ImmichCapabilities,
    ImmichClient,
    ImmichClientError,
    ImmichClientConfigurationError,
    MockImmichClient,
    create_http_immich_client,
    mock_assets,
)


def test_mock_immich_lists_assets_with_pagination() -> None:
    client = MockImmichClient(
        [
            AssetRef("a", "image"),
            AssetRef("b", "image"),
            AssetRef("c", "video"),
        ]
    )

    first, token = client.list_assets(page_size=2)
    second, next_token = client.list_assets(page_token=token, page_size=2)

    assert [asset.asset_id for asset in first] == ["a", "b"]
    assert [asset.asset_id for asset in second] == ["c"]
    assert next_token is None


def test_mock_immich_filters_by_media_type() -> None:
    client = MockImmichClient(
        [
            AssetRef("a", "image"),
            AssetRef("b", "video"),
        ]
    )

    assets, token = client.list_assets(media_types={"video"})

    assert [asset.asset_id for asset in assets] == ["b"]
    assert token is None


def test_mock_immich_can_find_create_and_add_to_review_album() -> None:
    client = MockImmichClient(
        [
            AssetRef("a", "image"),
            AssetRef("b", "image"),
        ]
    )

    assert client.find_album_by_name("Review") is None
    album_id = client.create_or_get_album("Review")
    repeated_album_id = client.create_or_get_album("Review")
    client.add_to_album(album_id, ["a", "b"])

    assert repeated_album_id == album_id
    assert client.find_album_by_name("Review") == album_id
    assert client.album_assets("Review") == ("a", "b")
    assert client.album_create_requests == ["Review"]
    assert client.add_to_album_requests == [
        {"album_id": album_id, "asset_ids": ["a", "b"]}
    ]


def test_mock_immich_can_find_create_and_add_tag() -> None:
    client = MockImmichClient(
        [
            AssetRef("a", "image"),
            AssetRef("b", "image"),
        ],
        capabilities=ImmichCapabilities(tags=True),
    )

    assert client.find_tag_by_name("review") is None
    tag_id = client.create_or_get_tag("review")
    repeated_tag_id = client.create_or_get_tag("review")
    client.add_tag_to_asset("a", tag_id)

    assert repeated_tag_id == tag_id
    assert client.find_tag_by_name("review") == tag_id
    assert client.asset_tags("a") == (tag_id,)
    assert client.tag_create_requests == ["review"]
    assert client.add_tag_requests == [{"asset_id": "a", "tag_id": tag_id}]


def test_default_mock_assets_cover_sprint_003_metadata() -> None:
    assets = mock_assets()

    assert {asset.media_type for asset in assets} == {"image", "video"}
    assert any(asset.archived for asset in assets)
    assert any(asset.favorite for asset in assets)
    assert any(asset.albums for asset in assets)
    assert all(asset.created_at is not None for asset in assets)


def test_default_mock_immich_pages_are_deterministic() -> None:
    client = MockImmichClient()

    first, token = client.list_assets(page_size=2)
    second, token = client.list_assets(page_token=token, page_size=2)
    third, token = client.list_assets(page_token=token, page_size=2)

    assert [asset.asset_id for asset in first] == [
        "mock-image-001",
        "mock-image-002",
    ]
    assert [asset.asset_id for asset in second] == [
        "mock-image-003",
        "mock-video-001",
    ]
    assert [asset.asset_id for asset in third] == [
        "mock-image-archived-001",
        "mock-video-favorite-001",
    ]
    assert token is None


def test_mock_immich_returns_synthetic_preview_bytes() -> None:
    client = MockImmichClient([AssetRef("a", "image")])

    preview_bytes = client.get_preview_bytes("a")

    assert preview_bytes == SYNTHETIC_IMAGE_PREVIEW_BYTES
    assert preview_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert client.preview_requests == ["a"]


def test_mock_immich_can_override_preview_bytes_per_asset() -> None:
    client = MockImmichClient(
        [AssetRef("a", "image"), AssetRef("b", "image")],
        preview_bytes_by_asset_id={"a": b"", "b": b"not-image"},
    )

    assert client.get_preview_bytes("a") == b""
    assert client.get_preview_bytes("b") == b"not-image"


def test_http_immich_server_probes_use_auth_only_when_needed() -> None:
    transport = _FakeUrlOpen(
        [
            (200, {"res": "pong"}),
            (200, {"major": 2, "minor": 7, "patch": 5}),
            (200, {"version": "v2.7.5", "licensed": True, "versionUrl": ""}),
            (200, {"search": True}),
        ]
    )
    client = _http_client(transport)

    assert client.ping_server()["res"] == "pong"
    assert client.server_version()["major"] == 2
    assert client.about()["version"] == "v2.7.5"
    assert client.features()["search"] is True

    assert _path(transport.requests[0]) == "/api/server/ping"
    assert _header(transport.requests[0], "x-api-key") is None
    assert _path(transport.requests[1]) == "/api/server/version"
    assert _header(transport.requests[1], "x-api-key") is None
    assert _path(transport.requests[2]) == "/api/server/about"
    assert _header(transport.requests[2], "x-api-key") == "test-secret"
    assert _path(transport.requests[3]) == "/api/server/features"
    assert _header(transport.requests[3], "x-api-key") is None


def test_http_immich_lists_assets_with_search_metadata() -> None:
    transport = _FakeUrlOpen(
        [
            (
                200,
                {
                    "albums": {},
                    "assets": {
                        "count": 1,
                        "facets": [],
                        "items": [
                            {
                                "id": "asset-1",
                                "type": "IMAGE",
                                "checksum": "sha1-base64",
                                "isArchived": False,
                                "isFavorite": True,
                                "fileCreatedAt": "2026-04-01T10:00:00.000Z",
                                "updatedAt": "2026-04-02T11:00:00.000Z",
                                "visibility": "timeline",
                                "originalMimeType": "image/jpeg",
                                "originalPath": "/private/path/not-stored.jpg",
                            }
                        ],
                        "nextPage": 2,
                        "total": 3,
                    },
                },
            ),
        ]
    )
    client = _http_client(transport)

    assets, token = client.list_assets(page_size=50, media_types={"image"})

    request = transport.requests[0]
    body = _json_body(request)
    assert request.get_method() == "POST"
    assert _path(request) == "/api/search/metadata"
    assert _query(request) == {}
    assert _header(request, "x-api-key") == "test-secret"
    assert "test-secret" not in request.full_url
    assert body["page"] == 1
    assert body["size"] == 50
    assert body["type"] == "IMAGE"
    assert body["withDeleted"] is False
    assert len(assets) == 1
    assert assets[0].asset_id == "asset-1"
    assert assets[0].media_type == "image"
    assert assets[0].checksum == "sha1-base64"
    assert assets[0].favorite is True
    assert assets[0].archived is False
    assert assets[0].metadata == {
        "mime_type": "image/jpeg",
        "visibility": "timeline",
    }
    assert token == "2"


def test_http_immich_gets_metadata_through_search_without_private_paths() -> None:
    transport = _FakeUrlOpen(
        [
            (
                200,
                {
                    "albums": {},
                    "assets": {
                        "count": 1,
                        "facets": [],
                        "items": [
                            {
                                "id": "asset-1",
                                "type": "VIDEO",
                                "checksum": "sha1-video",
                                "duration": "0:00:03.000000",
                                "isArchived": True,
                                "isFavorite": False,
                                "createdAt": "2026-04-01T10:00:00.000Z",
                                "updatedAt": "2026-04-02T11:00:00.000Z",
                                "visibility": "archive",
                                "originalPath": "/private/path/not-stored.mp4",
                            }
                        ],
                        "nextPage": None,
                        "total": 1,
                    },
                },
            ),
        ]
    )
    client = _http_client(transport)

    metadata = client.get_metadata("asset-1")

    assert _path(transport.requests[0]) == "/api/search/metadata"
    assert _json_body(transport.requests[0])["id"] == "asset-1"
    assert metadata["asset_id"] == "asset-1"
    assert metadata["media_type"] == "video"
    assert metadata["metadata"] == {
        "duration": "0:00:03.000000",
        "visibility": "archive",
    }
    assert "/private/path" not in json.dumps(metadata)


def test_http_immich_downloads_preview_thumbnail_bytes() -> None:
    transport = _FakeUrlOpen([(200, b"preview-bytes")])
    client = _http_client(transport)

    preview_bytes = client.get_preview_bytes("asset-1")

    request = transport.requests[0]
    assert preview_bytes == b"preview-bytes"
    assert request.get_method() == "GET"
    assert _path(request) == "/api/assets/asset-1/thumbnail"
    assert _query(request) == {"size": ["preview"]}
    assert _header(request, "x-api-key") == "test-secret"
    assert "test-secret" not in request.full_url


def test_http_immich_finds_creates_and_adds_to_review_album() -> None:
    transport = _FakeUrlOpen(
        [
            (
                200,
                [
                    {"id": "album-1", "albumName": "Review"},
                    {"id": "album-ignored", "albumName": "Other"},
                ],
            ),
            (201, {"id": "album-2", "albumName": "New Review"}),
            (
                200,
                [
                    {"id": "asset-1", "success": True},
                    {"id": "asset-2", "success": False, "error": "duplicate"},
                ],
            ),
        ]
    )
    client = _http_client(transport)

    assert client.find_album_by_name("Review") == "album-1"
    assert client.create_album("New Review") == "album-2"
    client.add_to_album("album-2", ["asset-1", "asset-2"])

    assert [request.get_method() for request in transport.requests] == [
        "GET",
        "POST",
        "PUT",
    ]
    assert [_path(request) for request in transport.requests] == [
        "/api/albums",
        "/api/albums",
        "/api/albums/album-2/assets",
    ]
    assert _json_body(transport.requests[1]) == {"albumName": "New Review"}
    assert _json_body(transport.requests[2]) == {"ids": ["asset-1", "asset-2"]}


def test_http_immich_add_to_album_failure_is_sanitized() -> None:
    transport = _FakeUrlOpen(
        [(200, [{"id": "asset-1", "success": False, "error": "no_permission"}])]
    )
    client = _http_client(transport)

    with pytest.raises(ImmichClientError) as exc_info:
        client.add_to_album("album-1", ["asset-1"])

    assert exc_info.value.error_code == "album_add_failed"
    assert "no_permission" not in str(exc_info.value)


def test_http_immich_finds_creates_and_adds_tag() -> None:
    transport = _FakeUrlOpen(
        [
            (
                200,
                [
                    {"id": "tag-1", "name": "review", "value": "review"},
                    {"id": "tag-ignored", "name": "Other", "value": "Other"},
                ],
            ),
            (201, {"id": "tag-2", "name": "New Review", "value": "New Review"}),
            (200, [{"id": "asset-1", "success": False, "error": "duplicate"}]),
        ]
    )
    client = _http_client(transport)

    assert client.find_tag_by_name("review") == "tag-1"
    assert client.create_tag("New Review") == "tag-2"
    client.add_tag_to_asset("asset-1", "tag-2")

    assert [request.get_method() for request in transport.requests] == [
        "GET",
        "POST",
        "PUT",
    ]
    assert [_path(request) for request in transport.requests] == [
        "/api/tags",
        "/api/tags",
        "/api/tags/tag-2/assets",
    ]
    assert _json_body(transport.requests[1]) == {"name": "New Review"}
    assert _json_body(transport.requests[2]) == {"ids": ["asset-1"]}
    assert all(
        _header(request, "x-api-key") == "test-secret"
        for request in transport.requests
    )
    assert all("test-secret" not in request.full_url for request in transport.requests)


def test_http_immich_create_or_get_tag_reuses_existing_value() -> None:
    transport = _FakeUrlOpen(
        [
            (
                200,
                [
                    {
                        "id": "tag-1",
                        "name": "review",
                        "value": "parent/review",
                    }
                ],
            ),
        ]
    )
    client = _http_client(transport)

    assert client.create_or_get_tag("parent/review") == "tag-1"

    assert len(transport.requests) == 1
    assert transport.requests[0].get_method() == "GET"
    assert _path(transport.requests[0]) == "/api/tags"


def test_http_immich_add_tag_failure_is_sanitized() -> None:
    transport = _FakeUrlOpen(
        [
            (
                200,
                [
                    {
                        "id": "asset-1",
                        "success": False,
                        "error": "no_permission",
                        "errorMessage": "api_key=leak-marker-value",
                    }
                ],
            )
        ]
    )
    client = _http_client(transport)

    with pytest.raises(ImmichClientError) as exc_info:
        client.add_tag_to_asset("asset-1", "tag-1")

    assert exc_info.value.error_code == "tag_add_failed"
    assert "no_permission" not in str(exc_info.value)
    assert "leak-marker-value" not in str(exc_info.value)


def test_http_immich_tag_invalid_responses_fail_closed() -> None:
    find_transport = _FakeUrlOpen([(200, {"tags": []})])
    create_transport = _FakeUrlOpen([(201, {"name": "review"})])
    add_transport = _FakeUrlOpen([(200, {"id": "asset-1", "success": True})])

    with pytest.raises(ImmichClientError) as find_exc:
        _http_client(find_transport).find_tag_by_name("review")
    with pytest.raises(ImmichClientError) as create_exc:
        _http_client(create_transport).create_tag("review")
    with pytest.raises(ImmichClientError) as add_exc:
        _http_client(add_transport).add_tag_to_asset("asset-1", "tag-1")

    assert find_exc.value.error_code == "invalid_tag_response"
    assert create_exc.value.error_code == "invalid_tag_response"
    assert add_exc.value.error_code == "invalid_tag_response"


def test_http_immich_real_adapter_supports_tags_and_keeps_archive_unsupported() -> None:
    transport = _FakeUrlOpen([])
    client = _http_client(transport)

    assert client.capabilities.albums is True
    assert client.capabilities.tags is True
    assert client.capabilities.archive is False
    with pytest.raises(NotImplementedError):
        client.archive_asset("asset-1")
    assert transport.requests == []


def test_create_http_immich_client_uses_env_var_name_without_reporting_value() -> None:
    config = load_config("templates/config.example.yml")

    client = create_http_immich_client(
        config,
        environ={"IMMICH_API_KEY": "test-secret"},
    )

    assert isinstance(client, HttpImmichClient)
    with pytest.raises(ImmichClientConfigurationError) as exc_info:
        create_http_immich_client(config, environ={})
    assert "IMMICH_API_KEY" in str(exc_info.value)
    assert "test-secret" not in str(exc_info.value)


def test_immich_client_surface_has_no_delete_or_trash_methods() -> None:
    method_names = (
        set(dir(ImmichClient))
        | set(dir(MockImmichClient))
        | set(dir(HttpImmichClient))
    )

    assert not {
        name
        for name in method_names
        if "delete" in name.lower() or "trash" in name.lower()
    }


class _FakeResponse:
    def __init__(self, status: int, body: object):
        self.status = status
        self._body = _response_body(body)

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakeUrlOpen:
    def __init__(self, responses: list[tuple[int, object]]):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        status, body = self._responses.pop(0)
        return _FakeResponse(status, body)


def _http_client(transport: _FakeUrlOpen) -> HttpImmichClient:
    return HttpImmichClient(
        base_url="https://immich.example.local",
        api_key="test-secret",
        urlopen_func=transport,
        max_retries=0,
        retry_backoff_seconds=0,
    )


def _response_body(body: object) -> bytes:
    if isinstance(body, bytes):
        return body
    return json.dumps(body).encode("utf-8")


def _path(request) -> str:
    return urlparse(request.full_url).path


def _query(request) -> dict[str, list[str]]:
    return parse_qs(urlparse(request.full_url).query)


def _json_body(request) -> dict:
    return json.loads((request.data or b"{}").decode("utf-8"))


def _header(request, name: str) -> str | None:
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None
