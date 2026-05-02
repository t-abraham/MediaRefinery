from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

import pytest
import yaml


def test_module_help_lists_expected_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mediarefinery", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "config validate" in result.stdout
    assert "config" in result.stdout
    assert "doctor" in result.stdout
    assert "report" in result.stdout
    assert "scan" in result.stdout


@pytest.mark.parametrize(
    "config_path",
    [
        "templates/config.example.yml",
        "templates/config.preset.sensitive.example.yml",
        "tests/fixtures/config.custom-taxonomy.yml",
    ],
)
def test_config_validate_command_accepts_valid_configs(config_path: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "config",
            "validate",
            "--config",
            config_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Config valid" in result.stdout


def test_doctor_checks_ffmpeg_when_video_is_enabled(tmp_path) -> None:
    data = yaml.safe_load(Path("templates/config.example.yml").read_text())
    data["scanner"]["media_types"] = ["video"]
    data["state"]["sqlite_path"] = str(tmp_path / "state.sqlite3")
    data["runtime"]["temp_dir"] = str(tmp_path / "frames")
    data["video"]["enabled"] = True
    data["video"]["ffmpeg_path"] = "definitely-missing-mediarefinery-ffmpeg"
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "doctor",
            "--config",
            str(config_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Doctor: ffmpeg failed" in result.stderr
    assert "definitely-missing-mediarefinery-ffmpeg" not in result.stderr


def test_doctor_reports_invalid_config(tmp_path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text("categories: [", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "doctor",
            "--config",
            str(config_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Doctor: config failed" in result.stderr
    assert "invalid YAML" in result.stderr


def test_scan_command_returns_partial_failure_exit_code(tmp_path) -> None:
    data = yaml.safe_load(Path("templates/config.example.yml").read_text())
    data["state"]["sqlite_path"] = str(tmp_path / "state.sqlite3")
    data["runtime"]["temp_dir"] = str(tmp_path / "tmp")
    del data["policies"]["needs_review"]["image"]
    config_path = tmp_path / "partial.yml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "scan",
            "--config",
            str(config_path),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 4
    assert "Errors: 1" in result.stdout
    assert "event=asset.error" in result.stderr
    assert "error_code=missing_policy" in result.stderr


def test_scan_immich_http_requires_api_key_env_without_reporting_value(tmp_path) -> None:
    data = yaml.safe_load(Path("templates/config.example.yml").read_text())
    data["state"]["sqlite_path"] = str(tmp_path / "state.sqlite3")
    data["runtime"]["temp_dir"] = str(tmp_path / "tmp")
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    env = os.environ.copy()
    env.pop("IMMICH_API_KEY", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediarefinery",
            "scan",
            "--config",
            str(config_path),
            "--immich-http",
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "IMMICH_API_KEY" in result.stderr
    assert "replace_me" not in result.stderr
