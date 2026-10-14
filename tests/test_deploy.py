"""Validate user-unit directives without installing units or production binaries."""

import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.mark.parametrize("name", ["serve", "watch", "tunnel"])
def test_user_units_verify(name: str, tmp_path: Path) -> None:
    body = (DEPLOY / f"kiroku-{name}.service").read_text()
    assert "WantedBy=default.target" in body
    assert "User=" not in body and "PrivateTmp=" not in body
    if name == "tunnel":
        assert (
            "ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate run "
            "--token-file %h/.config/kiroku/tunnel-token"
        ) in body
    else:
        assert "WorkingDirectory=%h/kiroku" in body
        command = "serve" if name == "serve" else "watch caedrel gorgc"
        assert f"ExecStart=%h/kiroku/.venv/bin/kiroku {command}" in body
    # Only executable existence is substituted: production binaries are not installed here.
    lines = [
        "ExecStart=/usr/bin/true" if line.startswith("ExecStart=") else line
        for line in body.splitlines()
    ]
    unit = tmp_path / f"kiroku-{name}.service"
    unit.write_text("\n".join(lines) + "\n")
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit)],
        capture_output=True,
        text=True,
        env=os.environ | {"XDG_RUNTIME_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr


def test_watch_has_one_unit_and_remotely_managed_tunnel() -> None:
    assert not (DEPLOY / "kiroku-watch@.service").exists()
    assert not (DEPLOY / "cloudflared.yml.example").exists()
    readme = " ".join((DEPLOY / "README.md").read_text().split())
    for instruction in [
        "systemctl --user",
        "loginctl enable-linger",
        "mode 600",
        "~/.config/systemd/user/",
    ]:
        assert instruction in readme
