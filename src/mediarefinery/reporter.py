from __future__ import annotations

from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass
from typing import TextIO

from .decision import ActionPlan
from .state import RunReport


SUCCESS_EXIT_CODE = 0
PARTIAL_FAILURE_EXIT_CODE = 4


@dataclass(frozen=True)
class ScanSummary:
    processed: int
    skipped: int
    errors: int
    by_category: dict[str, int]
    intended_actions: dict[str, int]
    dry_run: bool


class Reporter:
    def render_scan_summary(self, summary: ScanSummary) -> str:
        category_text = _format_counts(summary.by_category)
        intended_action_text = _format_counts(summary.intended_actions)
        action_label = (
            "Intended actions (not applied)"
            if summary.dry_run
            else "Intended actions"
        )
        dry_run_text = "true" if summary.dry_run else "false"
        return (
            f"Processed: {summary.processed}\n"
            f"Skipped: {summary.skipped}\n"
            f"Errors: {summary.errors}\n"
            f"by_category: {category_text}\n"
            f"{action_label}: {intended_action_text}\n"
            f"dry_run={dry_run_text}"
        )

    def write_scan_summary(self, summary: ScanSummary, stream: TextIO) -> None:
        stream.write(self.render_scan_summary(summary))
        stream.write("\n")

    def render_run_report(self, report: RunReport) -> str:
        partial_failure_text = "yes" if report.partial_failure else "no"
        operational_exit_code = (
            PARTIAL_FAILURE_EXIT_CODE if report.partial_failure else SUCCESS_EXIT_CODE
        )
        lines = [
            "# MediaRefinery Run Report",
            "",
            "## Run Metadata",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Run ID | {report.run_id} |",
            f"| Command | {_table_value(report.command)} |",
            f"| Status | {_table_value(report.status)} |",
            f"| Mode | {report.mode} |",
            f"| Started | {_table_value(report.started_at)} |",
            f"| Ended | {_table_value(report.ended_at)} |",
            f"| Config source | {_table_value(report.config_source_name)} |",
            f"| Config hash | {_short_hash(report.config_hash)} |",
            f"| Model backend | {_table_value(report.model_backend)} |",
            f"| Model profile | {_table_value(report.model_profile_name)} |",
            f"| Model version | {_table_value(report.model_version)} |",
            f"| Partial failures | {partial_failure_text} |",
            f"| Operational exit code | {operational_exit_code} |",
            "",
            "## Summary",
            "",
            f"- Processed: {report.processed}",
            f"- Skipped: {report.skipped}",
            f"- Errors: {report.errors}",
            f"- Dry run: {_bool_text(report.dry_run)}",
            "",
            "## Category Counts",
            "",
            *_count_table("Category", report.by_category),
            "",
            "## Action Counts",
            "",
            *_action_table(report),
            "",
            "## Partial Failure Summary",
            "",
            *_error_table(report),
            "",
        ]
        return "\n".join(lines)

    def write_run_report(self, report: RunReport, stream: TextIO) -> None:
        stream.write(self.render_run_report(report))
        stream.write("\n")


def summarize_categories(category_ids: list[str], dry_run: bool) -> ScanSummary:
    counts = Counter(category_ids)
    return ScanSummary(
        processed=len(category_ids),
        skipped=0,
        errors=0,
        by_category=dict(counts),
        intended_actions={},
        dry_run=dry_run,
    )


def summarize_scan(
    action_plans: Iterable[ActionPlan],
    *,
    skipped: int,
    errors: int,
    dry_run: bool,
) -> ScanSummary:
    category_counts: Counter[str] = Counter()
    intended_action_counts: Counter[str] = Counter()
    processed = 0

    for action_plan in action_plans:
        processed += 1
        category_counts[action_plan.category_id] += 1
        intended_action_counts.update(action_plan.actions)

    return ScanSummary(
        processed=processed,
        skipped=skipped,
        errors=errors,
        by_category=dict(category_counts),
        intended_actions=dict(intended_action_counts),
        dry_run=dry_run,
    )


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none=0"
    return " ".join(f"{name}={count}" for name, count in sorted(counts.items()))


def _count_table(label: str, counts: dict[str, int]) -> list[str]:
    if not counts:
        return [f"No {label.lower()} counts recorded."]

    lines = [f"| {label} | Count |", "| --- | ---: |"]
    lines.extend(
        f"| {_table_value(name)} | {count} |"
        for name, count in sorted(counts.items())
    )
    return lines


def _action_table(report: RunReport) -> list[str]:
    if not report.action_counts:
        return ["No action rows recorded."]

    lines = [
        "| Action | Error code | Total | Would apply | Succeeded | Failed |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for count in report.action_counts:
        lines.append(
            "| "
            f"{_table_value(count.action_name)} | "
            f"{_table_value(count.error_code)} | "
            f"{count.total} | "
            f"{count.would_apply} | "
            f"{count.succeeded} | "
            f"{count.failed} |"
        )
    return lines


def _error_table(report: RunReport) -> list[str]:
    if not report.partial_failure:
        return ["No partial failures recorded."]
    if not report.error_counts:
        return ["Partial failure status was recorded, but no error rows were found."]

    lines = [
        "| Stage | Error code | Count | Affected assets |",
        "| --- | --- | ---: | ---: |",
    ]
    for count in report.error_counts:
        lines.append(
            "| "
            f"{_table_value(count.stage)} | "
            f"{_table_value(count.message_code)} | "
            f"{count.total} | "
            f"{count.affected_assets} |"
        )
    return lines


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _short_hash(value: str | None) -> str:
    if value is None:
        return "unknown"
    return _table_value(value[:12])


def _table_value(value: object | None) -> str:
    if value is None or value == "":
        return "unknown"
    return str(value).replace("|", "\\|")

