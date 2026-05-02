from __future__ import annotations

import copy
import json
import sqlite3
from urllib.parse import urlparse

from mediarefinery.actions import ActionExecutor
from mediarefinery.config import load_config, validate_config_data
from mediarefinery.decision import ActionPlan
from mediarefinery.immich import (
    SYNTHETIC_IMAGE_PREVIEW_BYTES,
    HttpImmichClient,
    ImmichCapabilities,
    MockImmichClient,
)
from mediarefinery.pipeline import run_scan


def _example_data() -> dict:
    return copy.deepcopy(load_config("templates/config.example.yml").raw)


def _config_for_policy(
    actions: list[str],
    *,
    dry_run: bool,
    archive_enabled: bool | None = None,
):
    data = _example_data()
    data["actions"]["dry_run"] = dry_run
    if archive_enabled is not None:
        data["actions"]["archive_enabled"] = archive_enabled
    elif "archive" in actions:
        data["actions"]["archive_enabled"] = True
    data["policies"]["needs_review"]["image"]["on_match"] = actions
    return validate_config_data(data)


def test_action_executor_dry_run_blocks_album_tag_and_archive_mutations() -> None:
    config = _config_for_policy(
        ["add_to_review_album", "add_tag", "archive"],
        dry_run=True,
        archive_enabled=True,
    )

    class MutatingTrapClient(MockImmichClient):
        def create_or_get_album(self, name: str) -> str:  # pragma: no cover
            raise AssertionError("dry-run must not create albums")

        def add_to_album(
            self,
            album_id: str,
            asset_ids: list[str],
        ) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not add assets to albums")

        def create_or_get_tag(self, name: str) -> str:  # pragma: no cover
            raise AssertionError("dry-run must not create tags")

        def add_tag_to_asset(
            self,
            asset_id: str,
            tag_id: str,
        ) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not add tags")

        def archive_asset(self, asset_id: str) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not archive assets")

    client = MutatingTrapClient(
        capabilities=ImmichCapabilities(tags=True, archive=True)
    )
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_to_review_album", "add_tag", "archive"),
        True,
        asset_id="mock-image-002",
    )

    results = ActionExecutor(config, client).execute(plan)

    assert [(result.action_name, result.success) for result in results] == [
        ("add_to_review_album", True),
        ("add_tag", True),
        ("archive", True),
    ]
    assert all(result.dry_run is True for result in results)
    assert all(result.would_apply is True for result in results)
    assert client.album_find_requests == []
    assert client.album_create_requests == []
    assert client.add_to_album_requests == []
    assert client.tag_find_requests == []
    assert client.tag_create_requests == []
    assert client.add_tag_requests == []
    assert client.archive_requests == []


def test_live_actions_require_config_dry_run_false_or_explicit_override() -> None:
    config = _config_for_policy(["add_to_review_album"], dry_run=True)
    client = MockImmichClient()
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_to_review_album",),
        False,
        asset_id="mock-image-002",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is False
    assert result.error_code == "live_actions_not_enabled"
    assert client.album_find_requests == []
    assert client.album_create_requests == []
    assert client.add_to_album_requests == []


def test_explicit_live_override_allows_album_action() -> None:
    config = _config_for_policy(["add_to_review_album"], dry_run=True)
    client = MockImmichClient()
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_to_review_album",),
        False,
        asset_id="mock-image-002",
    )

    result = ActionExecutor(
        config,
        client,
        dry_run_override=False,
    ).execute(plan)[0]

    assert result.success is True
    assert client.album_assets("MediaRefinery Review") == ("mock-image-002",)


def test_executor_refuses_archive_when_action_config_disabled() -> None:
    config = _config_for_policy(["no_action"], dry_run=False, archive_enabled=False)
    client = MockImmichClient(
        capabilities=ImmichCapabilities(archive=True)
    )
    plan = ActionPlan(
        "needs_review",
        "image",
        ("archive",),
        False,
        asset_id="mock-image-002",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is False
    assert result.error_code == "archive_disabled"
    assert client.archive_requests == []


def test_executor_rejects_destructive_actions_even_if_plan_is_constructed() -> None:
    config = _config_for_policy(["no_action"], dry_run=False)
    client = MockImmichClient()
    plan = ActionPlan(
        "needs_review",
        "image",
        ("delete", "trash"),
        False,
        asset_id="mock-image-002",
    )

    results = ActionExecutor(config, client).execute(plan)

    assert [result.error_code for result in results] == [
        "destructive_action_unsupported",
        "destructive_action_unsupported",
    ]
    assert all(result.success is False for result in results)


def test_live_mock_album_action_records_success_in_state(tmp_path) -> None:
    config = _config_for_policy(["add_to_review_album"], dry_run=False)
    client = MockImmichClient()
    state_path = tmp_path / "state.sqlite3"

    summary = run_scan(config, state_path=state_path, client=client)

    assert summary.processed == 3
    assert summary.errors == 0
    assert client.album_assets("MediaRefinery Review") == ("mock-image-002",)
    action_rows = _sqlite_rows(
        state_path,
        """
        SELECT action_name, dry_run, would_apply, success, error_code
        FROM action_runs
        ORDER BY action_name, id
        """,
    )
    assert action_rows == [
        ("add_to_review_album", 0, 1, 1, None),
        ("no_action", 0, 0, 1, None),
        ("no_action", 0, 0, 1, None),
    ]


def test_unsupported_tag_action_records_failure_and_continues(tmp_path) -> None:
    config = _config_for_policy(["add_tag"], dry_run=False)
    state_path = tmp_path / "state.sqlite3"

    summary = run_scan(config, state_path=state_path, client=MockImmichClient())

    assert summary.processed == 3
    assert summary.errors == 1
    action_rows = _sqlite_rows(
        state_path,
        """
        SELECT asset_id, action_name, dry_run, would_apply, success, error_code
        FROM action_runs
        ORDER BY id
        """,
    )
    assert action_rows == [
        ("mock-image-001", "no_action", 0, 0, 1, None),
        ("mock-image-002", "add_tag", 0, 1, 0, "tag_unsupported"),
        ("mock-image-003", "no_action", 0, 0, 1, None),
    ]
    error_rows = _sqlite_rows(
        state_path,
        "SELECT stage, message_code FROM errors ORDER BY id",
    )
    assert error_rows == [("action", "tag_unsupported")]


def test_unsupported_archive_action_records_failure_and_continues(tmp_path) -> None:
    config = _config_for_policy(
        ["archive"],
        dry_run=False,
        archive_enabled=True,
    )
    state_path = tmp_path / "state.sqlite3"

    summary = run_scan(config, state_path=state_path, client=MockImmichClient())

    assert summary.processed == 3
    assert summary.errors == 1
    action_rows = _sqlite_rows(
        state_path,
        """
        SELECT asset_id, action_name, dry_run, would_apply, success, error_code
        FROM action_runs
        ORDER BY id
        """,
    )
    assert action_rows == [
        ("mock-image-001", "no_action", 0, 0, 1, None),
        ("mock-image-002", "archive", 0, 1, 0, "archive_unsupported"),
        ("mock-image-003", "no_action", 0, 0, 1, None),
    ]


def test_real_http_tag_action_creates_missing_tag_when_enabled() -> None:
    config = _config_for_policy(["add_tag"], dry_run=False)
    transport = _FakeUrlOpen(
        [
            (200, []),
            (201, {"id": "tag-1", "name": "mediarefinery-review"}),
            (200, [{"id": "asset-1", "success": True}]),
        ]
    )
    client = _http_client(transport)
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_tag",),
        False,
        asset_id="asset-1",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is True
    assert result.error_code is None
    assert [request.get_method() for request in transport.requests] == [
        "GET",
        "POST",
        "PUT",
    ]
    assert [_path(request) for request in transport.requests] == [
        "/api/tags",
        "/api/tags",
        "/api/tags/tag-1/assets",
    ]
    assert _json_body(transport.requests[1]) == {"name": "mediarefinery-review"}
    assert _json_body(transport.requests[2]) == {"ids": ["asset-1"]}


def test_real_http_tag_action_respects_create_disabled_when_tag_exists() -> None:
    data = _example_data()
    data["actions"]["dry_run"] = False
    data["actions"]["create_tag_if_missing"] = False
    data["policies"]["needs_review"]["image"]["on_match"] = ["add_tag"]
    config = validate_config_data(data)
    transport = _FakeUrlOpen(
        [
            (
                200,
                [
                    {
                        "id": "tag-1",
                        "name": "mediarefinery-review",
                        "value": "mediarefinery-review",
                    }
                ],
            ),
            (200, [{"id": "asset-1", "success": True}]),
        ]
    )
    client = _http_client(transport)
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_tag",),
        False,
        asset_id="asset-1",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is True
    assert [request.get_method() for request in transport.requests] == ["GET", "PUT"]
    assert [_path(request) for request in transport.requests] == [
        "/api/tags",
        "/api/tags/tag-1/assets",
    ]


def test_real_http_tag_action_respects_create_disabled_when_tag_is_missing() -> None:
    data = _example_data()
    data["actions"]["dry_run"] = False
    data["actions"]["create_tag_if_missing"] = False
    data["policies"]["needs_review"]["image"]["on_match"] = ["add_tag"]
    config = validate_config_data(data)
    transport = _FakeUrlOpen([(200, [])])
    client = _http_client(transport)
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_tag",),
        False,
        asset_id="asset-1",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is False
    assert result.error_code == "tag_missing"
    assert len(transport.requests) == 1
    assert _path(transport.requests[0]) == "/api/tags"


def test_real_http_tag_action_failure_is_sanitized() -> None:
    config = _config_for_policy(["add_tag"], dry_run=False)
    transport = _FakeUrlOpen(
        [
            (
                200,
                [
                    {
                        "id": "tag-1",
                        "name": "mediarefinery-review",
                        "value": "mediarefinery-review",
                    }
                ],
            ),
            (
                200,
                [
                    {
                        "id": "asset-1",
                        "success": False,
                        "error": "no_permission",
                        "errorMessage": "token=leak-marker-value",
                    }
                ],
            ),
        ]
    )
    client = _http_client(transport)
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_tag",),
        False,
        asset_id="asset-1",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is False
    assert result.error_code == "tag_action_failed"
    assert result.message == "tag action failed"
    assert "no_permission" not in result.message
    assert "leak-marker-value" not in result.message


def test_real_http_tag_partial_failure_records_state_and_continues(tmp_path) -> None:
    data = _example_data()
    data["actions"]["dry_run"] = False
    data["classifier_profiles"]["default"]["output_mapping"]["raw_safety"] = (
        "needs_review"
    )
    data["policies"]["needs_review"]["image"]["on_match"] = ["add_tag"]
    config = validate_config_data(data)
    transport = _FakeUrlOpen(
        [
            (
                200,
                {
                    "albums": {},
                    "assets": {
                        "count": 2,
                        "facets": [],
                        "items": [
                            {
                                "id": "asset-1",
                                "type": "IMAGE",
                                "checksum": "sha256:001",
                                "visibility": "timeline",
                            },
                            {
                                "id": "asset-2",
                                "type": "IMAGE",
                                "checksum": "sha256:002",
                                "visibility": "timeline",
                            },
                        ],
                        "nextPage": None,
                        "total": 2,
                    },
                },
            ),
            (200, SYNTHETIC_IMAGE_PREVIEW_BYTES),
            (
                200,
                [
                    {
                        "id": "tag-1",
                        "name": "mediarefinery-review",
                        "value": "mediarefinery-review",
                    }
                ],
            ),
            (200, [{"id": "asset-1", "success": True}]),
            (200, SYNTHETIC_IMAGE_PREVIEW_BYTES),
            (
                200,
                [
                    {
                        "id": "tag-1",
                        "name": "mediarefinery-review",
                        "value": "mediarefinery-review",
                    }
                ],
            ),
            (200, [{"id": "asset-2", "success": False, "error": "unknown"}]),
        ]
    )
    client = _http_client(transport)
    state_path = tmp_path / "state.sqlite3"

    summary = run_scan(config, state_path=state_path, client=client)

    assert summary.processed == 2
    assert summary.errors == 1
    action_rows = _sqlite_rows(
        state_path,
        """
        SELECT asset_id, action_name, dry_run, would_apply, success, error_code
        FROM action_runs
        ORDER BY id
        """,
    )
    assert action_rows == [
        ("asset-1", "add_tag", 0, 1, 1, None),
        ("asset-2", "add_tag", 0, 1, 0, "tag_action_failed"),
    ]
    error_rows = _sqlite_rows(
        state_path,
        "SELECT stage, message_code FROM errors ORDER BY id",
    )
    assert error_rows == [("action", "tag_action_failed")]


def test_real_http_tag_action_dry_run_makes_no_requests() -> None:
    config = _config_for_policy(["add_tag"], dry_run=True)

    def fail_on_network(*args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("dry-run must not call Immich HTTP")

    client = HttpImmichClient(
        base_url="https://immich.example.local",
        api_key="test-secret",
        urlopen_func=fail_on_network,
    )
    plan = ActionPlan(
        "needs_review",
        "image",
        ("add_tag",),
        True,
        asset_id="asset-1",
    )

    result = ActionExecutor(config, client).execute(plan)[0]

    assert result.success is True
    assert result.dry_run is True
    assert result.would_apply is True


def test_real_http_adapter_keeps_archive_closed_without_requests() -> None:
    config = _config_for_policy(
        ["no_action"],
        dry_run=False,
        archive_enabled=True,
    )

    def fail_on_network(*args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("unsupported actions must not call Immich HTTP")

    client = HttpImmichClient(
        base_url="https://immich.example.local",
        api_key="test-secret",
        urlopen_func=fail_on_network,
    )
    archive_plan = ActionPlan(
        "needs_review",
        "image",
        ("archive",),
        False,
        asset_id="asset-1",
    )

    archive_result = ActionExecutor(config, client).execute(archive_plan)[0]

    assert archive_result.success is False
    assert archive_result.error_code == "archive_unsupported"


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


def _json_body(request) -> dict:
    return json.loads((request.data or b"{}").decode("utf-8"))


def _sqlite_rows(path, query: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute(query))
