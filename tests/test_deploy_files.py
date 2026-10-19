"""Deployment hardening that is easy to undo by accident (security review #9-#11)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _service_block(name):
    text = (ROOT / "docker-compose.yml").read_text()
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \w|^\S)", text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_logger_gets_no_grafana_secrets_and_no_env_file():
    block = "\n".join(line for line in _service_block("fordlogger").splitlines()
                      if not line.strip().startswith("#"))
    assert "env_file" not in block
    assert "GRAFANA" not in block
    for var in ("ZEEKR_USERNAME", "ZEEKR_PASSWORD", "ZEEKR_VIN_IV", "FORDLOGGER_DB_PASSWORD"):
        assert var in block


def test_container_runs_as_non_root():
    text = (ROOT / "Dockerfile").read_text()
    users = re.findall(r"^USER\s+(\S+)", text, re.M)
    assert users and users[-1] not in ("root", "0")


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
@pytest.mark.parametrize("path,ignored", [
    (".env", True), (".env.local", True), (".env.bak", True), (".env.example", False),
    ("zeekr_session.json", True), ("sub/zeekr_session.json", True),
    ("zeekrmate_1.dump", True), ("backups/x.dump", True), ("captures-sanitised/a.json", True),
])
def test_gitignore(path, ignored):
    r = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT)
    assert (r.returncode == 0) == ignored, path


def test_dockerignore_covers_secrets():
    lines = set((ROOT / ".dockerignore").read_text().split())
    for pattern in (".env", ".env.*", "**/zeekr_session.json", "**/*.dump", "backups/", "data/"):
        assert pattern in lines, pattern
