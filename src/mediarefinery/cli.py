from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .classifier import ClassifierError
from .config import AppConfig, ConfigError, load_config
from .doctor import DoctorResult, run_doctor_checks
from .immich import ImmichClientError, create_http_immich_client
from .observability import configure_logging
from .pipeline import run_scan
from .reporter import PARTIAL_FAILURE_EXIT_CODE, Reporter
from .state import StateStore


MARKDOWN_FORMAT = "markdown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediarefinery",
        description="Local-first media triage companion for Immich.",
        epilog=(
            "Common commands: mediarefinery config validate, "
            "mediarefinery doctor, mediarefinery scan --dry-run, "
            "mediarefinery report"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    config_parser = subparsers.add_parser("config", help="Configuration commands")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    validate_parser = config_subparsers.add_parser(
        "validate", help="Validate a MediaRefinery YAML config"
    )
    validate_parser.add_argument(
        "--config",
        help="Path to config YAML. Defaults to MEDIAREFINERY_CONFIG or templates/config.example.yml.",
    )
    validate_parser.set_defaults(func=_cmd_config_validate)

    doctor_parser = subparsers.add_parser("doctor", help="Run basic readiness checks")
    doctor_parser.add_argument("--config", help="Optional config YAML to validate")
    doctor_parser.set_defaults(func=_cmd_doctor)

    scan_parser = subparsers.add_parser(
        "scan",
        help="Run a scan; uses mock Immich unless --immich-http is set",
    )
    scan_parser.add_argument(
        "--config",
        help="Path to config YAML. Defaults to MEDIAREFINERY_CONFIG or templates/config.example.yml.",
    )
    scan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run behavior; no mutating Immich calls are made.",
    )
    scan_parser.add_argument(
        "--state-path",
        help="Override state.sqlite_path, useful for tests and local smoke runs.",
    )
    scan_parser.add_argument(
        "--immich-http",
        action="store_true",
        help="Use the real Immich HTTP adapter for reads, previews, album writes, and tag writes.",
    )
    scan_parser.set_defaults(func=_cmd_scan)

    report_parser = subparsers.add_parser(
        "report",
        help="Generate a safe markdown report from SQLite state",
    )
    report_parser.add_argument(
        "--config",
        help="Path to config YAML for state and report output settings.",
    )
    report_parser.add_argument(
        "--state-path",
        help="Override state.sqlite_path. When used without --config, output defaults to stdout.",
    )
    report_parser.add_argument(
        "--run-id",
        type=int,
        help="Run id to report. Defaults to the latest run in state.",
    )
    report_parser.add_argument(
        "--format",
        help="Report format. Only markdown is implemented in Sprint 011.",
    )
    report_parser.add_argument(
        "--output",
        help="Write to this path, or '-' for stdout. Defaults to reports.output_dir when a config is used.",
    )
    report_parser.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))


def _cmd_config_validate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("Config invalid:", file=sys.stderr)
        for error in exc.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    source = config.source or "<memory>"
    print(f"Config valid: {source}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    result = run_doctor_checks(args.config)
    _print_doctor_result(result)
    return result.exit_code


def _cmd_scan(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("Scan aborted: config invalid", file=sys.stderr)
        for error in exc.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    try:
        configure_logging(config.runtime.get("log_level"))
        client = create_http_immich_client(config) if args.immich_http else None
        summary = run_scan(
            config,
            state_path=args.state_path,
            dry_run_override=True if args.dry_run else None,
            client=client,
        )
    except ClassifierError as exc:
        print(f"Scan aborted: classifier failed: {exc}", file=sys.stderr)
        return 1
    except ImmichClientError as exc:
        print(f"Scan aborted: Immich HTTP failed: {exc}", file=sys.stderr)
        return 1 if exc.error_code == "missing_api_key" else 2
    return PARTIAL_FAILURE_EXIT_CODE if summary.errors else 0


def _cmd_report(args: argparse.Namespace) -> int:
    config = _load_report_config(args)
    if isinstance(config, int):
        return config

    report_format = _report_format(args, config)
    if report_format != MARKDOWN_FORMAT:
        print(
            "Report aborted: only markdown format is implemented in Sprint 011",
            file=sys.stderr,
        )
        return 1

    sqlite_path = _report_state_path(args, config)
    try:
        with StateStore(sqlite_path) as store:
            report = store.get_run_report(args.run_id)
    except (OSError, RuntimeError, sqlite3.Error):
        print("Report aborted: unable to read state database", file=sys.stderr)
        return 1

    if report is None:
        print("Report aborted: no matching run found in state", file=sys.stderr)
        return 1

    reporter = Reporter()
    output_path = _report_output_path(args, config, report.run_id)
    if output_path is None:
        reporter.write_run_report(report, sys.stdout)
    else:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                reporter.render_run_report(report) + "\n",
                encoding="utf-8",
            )
        except OSError:
            print("Report aborted: unable to write report output", file=sys.stderr)
            return 1
        print(f"Report written: {output_path.name}")

    return PARTIAL_FAILURE_EXIT_CODE if report.partial_failure else 0


def _print_doctor_result(result: DoctorResult) -> None:
    for check in result.checks:
        stream = sys.stderr if check.failed else sys.stdout
        print(f"Doctor: {check.name} {check.status}", file=stream)
        print(f"- {check.message}", file=stream)
        for detail in check.details:
            print(f"- {detail}", file=stream)

    if result.exit_code == 0:
        print("Doctor: OK")
    else:
        print("Doctor: FAILED", file=sys.stderr)


def _load_report_config(args: argparse.Namespace) -> AppConfig | None | int:
    should_load_config = bool(args.config) or not args.state_path
    if not should_load_config:
        return None
    try:
        return load_config(args.config)
    except ConfigError as exc:
        print("Report aborted: config invalid", file=sys.stderr)
        for error in exc.errors:
            print(f"- {error}", file=sys.stderr)
        return 1


def _report_format(args: argparse.Namespace, config: AppConfig | None) -> str:
    configured_format = None
    if config is not None:
        configured_format = config.reports.get("format")
    requested_format = args.format or configured_format or MARKDOWN_FORMAT
    return str(requested_format).strip().lower()


def _report_state_path(args: argparse.Namespace, config: AppConfig | None) -> str:
    if args.state_path:
        return str(args.state_path)
    if config is not None:
        return str(config.state.get("sqlite_path") or "state.sqlite3")
    return "state.sqlite3"


def _report_output_path(
    args: argparse.Namespace,
    config: AppConfig | None,
    run_id: int,
) -> Path | None:
    if args.output == "-":
        return None
    if args.output:
        return Path(args.output)
    if config is None:
        return None
    reports = config.reports
    if reports.get("enabled") is False:
        return None
    output_dir = reports.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        return None
    return Path(output_dir) / f"mediarefinery-run-{run_id}.md"
