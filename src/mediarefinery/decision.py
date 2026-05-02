from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig


SIDE_EFFECT_ACTIONS = frozenset(
    {"add_to_review_album", "add_tag", "archive", "move_to_locked_folder"}
)
SAFE_NO_ACTION = ("no_action",)


@dataclass(frozen=True)
class IntendedAction:
    name: str
    would_apply: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "would_apply": self.would_apply,
        }


@dataclass(frozen=True)
class ActionPlan:
    category_id: str
    media_type: str
    actions: tuple[str, ...]
    dry_run: bool
    asset_id: str | None = None
    policy_found: bool = True
    reason: str = "policy_match"
    error_code: str | None = None

    @property
    def intended_actions(self) -> tuple[IntendedAction, ...]:
        return tuple(
            IntendedAction(
                name=action_name,
                would_apply=action_name in SIDE_EFFECT_ACTIONS,
            )
            for action_name in self.actions
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "category_id": self.category_id,
            "media_type": self.media_type,
            "actions": list(self.actions),
            "dry_run": self.dry_run,
            "policy_found": self.policy_found,
            "reason": self.reason,
            "error_code": self.error_code,
            "intended_actions": [
                action.as_dict() for action in self.intended_actions
            ],
        }


class DecisionEngine:
    def __init__(self, config: AppConfig):
        self._config = config

    def decide(
        self,
        category_id: str,
        media_type: str,
        dry_run: bool,
        *,
        asset_id: str | None = None,
    ) -> ActionPlan:
        if category_id not in self._config.category_ids:
            return self._safe_plan(
                category_id,
                media_type,
                dry_run=dry_run,
                asset_id=asset_id,
                reason="unknown_category",
                error_code="unknown_category",
            )

        category_policy = self._config.policies.get(category_id)
        if not isinstance(category_policy, dict):
            return self._safe_plan(
                category_id,
                media_type,
                dry_run=dry_run,
                asset_id=asset_id,
                reason="missing_policy",
                error_code="missing_policy",
            )

        media_policy = category_policy.get(media_type)
        if not isinstance(media_policy, dict):
            return self._safe_plan(
                category_id,
                media_type,
                dry_run=dry_run,
                asset_id=asset_id,
                reason="missing_policy",
                error_code="missing_policy",
            )

        configured_actions = media_policy.get("on_match")
        if not isinstance(configured_actions, list) or not configured_actions:
            return self._safe_plan(
                category_id,
                media_type,
                dry_run=dry_run,
                asset_id=asset_id,
                reason="invalid_policy",
                error_code="invalid_policy",
            )

        actions = tuple(str(action) for action in configured_actions)
        return ActionPlan(
            category_id=category_id,
            media_type=media_type,
            actions=actions,
            dry_run=dry_run,
            asset_id=asset_id,
        )

    def _safe_plan(
        self,
        category_id: str,
        media_type: str,
        *,
        dry_run: bool,
        asset_id: str | None,
        reason: str,
        error_code: str,
    ) -> ActionPlan:
        return ActionPlan(
            category_id=category_id,
            media_type=media_type,
            actions=SAFE_NO_ACTION,
            dry_run=dry_run,
            asset_id=asset_id,
            policy_found=False,
            reason=reason,
            error_code=error_code,
        )

