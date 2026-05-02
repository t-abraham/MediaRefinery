from __future__ import annotations

import pytest

from mediarefinery.config import validate_config_data
from mediarefinery.decision import DecisionEngine


def _config_for_actions(category_actions: dict[str, list[str]]):
    return validate_config_data(
        {
            "version": 1,
            "categories": [{"id": category_id} for category_id in category_actions],
            "classifier_profiles": {
                "default": {
                    "backend": "noop",
                    "model_path": None,
                    "output_mapping": {
                        f"raw_{category_id}": category_id
                        for category_id in category_actions
                    },
                }
            },
            "integration": {
                "immich": {"url": "https://immich.example.local", "api_key_env": "KEY"}
            },
            "scanner": {"media_types": ["image", "video"]},
            "classifier": {"profile": "default"},
            "actions": {
                "dry_run": True,
                "archive_enabled": any(
                    "archive" in actions for actions in category_actions.values()
                ),
            },
            "policies": {
                category_id: {"image": {"on_match": actions}}
                for category_id, actions in category_actions.items()
            },
        }
    )


@pytest.mark.parametrize(
    ("category_actions", "expected"),
    [
        (
            {
                "invoice_keep": ["no_action"],
                "receipt_tag": ["add_tag"],
                "family_review": ["add_to_review_album"],
            },
            {
                "invoice_keep": ("no_action", False),
                "receipt_tag": ("add_tag", True),
                "family_review": ("add_to_review_album", True),
            },
        ),
        (
            {
                "document_archive": ["archive"],
                "quality_check": ["manual_review"],
            },
            {
                "document_archive": ("archive", True),
                "quality_check": ("manual_review", False),
            },
        ),
    ],
)
def test_decision_engine_uses_arbitrary_user_categories_and_actions(
    category_actions: dict[str, list[str]],
    expected: dict[str, tuple[str, bool]],
) -> None:
    engine = DecisionEngine(_config_for_actions(category_actions))

    for category_id, (expected_action, expected_would_apply) in expected.items():
        plan = engine.decide(
            category_id,
            "image",
            dry_run=True,
            asset_id=f"asset-{category_id}",
        )

        assert plan.asset_id == f"asset-{category_id}"
        assert plan.category_id == category_id
        assert plan.actions == (expected_action,)
        assert plan.dry_run is True
        assert plan.policy_found is True
        assert plan.reason == "policy_match"
        assert plan.error_code is None
        assert plan.intended_actions[0].would_apply is expected_would_apply
        assert plan.as_dict()["category_id"] == category_id


def test_decision_missing_media_policy_fails_safely_to_no_action() -> None:
    engine = DecisionEngine(_config_for_actions({"lab_notes": ["add_tag"]}))

    plan = engine.decide("lab_notes", "video", dry_run=True, asset_id="asset-1")

    assert plan.actions == ("no_action",)
    assert plan.policy_found is False
    assert plan.reason == "missing_policy"
    assert plan.error_code == "missing_policy"
    assert plan.intended_actions[0].would_apply is False


def test_decision_unknown_category_fails_safely_to_no_action() -> None:
    engine = DecisionEngine(_config_for_actions({"known_bucket": ["add_tag"]}))

    plan = engine.decide("unknown_bucket", "image", dry_run=True)

    assert plan.actions == ("no_action",)
    assert plan.policy_found is False
    assert plan.reason == "unknown_category"
    assert plan.error_code == "unknown_category"
