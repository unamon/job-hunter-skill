"""Phase 1 smoke tests: package imports, version matches SKILL.md frontmatter,
linter is runnable end-to-end."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import job_hunter

REPO_ROOT = Path(__file__).resolve().parents[5]


def test_package_version_set() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", job_hunter.__version__)


def test_versions_agree_across_files() -> None:
    pkg_v = job_hunter.__version__

    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace["plugins"][0]["version"] == pkg_v

    skill_md = (REPO_ROOT / "skills" / "job-hunter" / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^version:\s*(\S+)\s*$", skill_md, re.MULTILINE)
    assert m, "SKILL.md frontmatter missing `version:`"
    assert m.group(1) == pkg_v


def test_secret_leak_linter_clean_on_assets() -> None:
    """Running the linter against shipped assets must be clean — they're empty templates."""
    script = REPO_ROOT / "skills" / "job-hunter" / "scripts" / "lint_secret_leaks.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--paths",
            str(REPO_ROOT / "skills" / "job-hunter" / "assets"),
            str(REPO_ROOT / "skills" / "job-hunter" / "references"),
            "--allow-empty",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"


def test_secret_leak_linter_flags_known_bad(tmp_path: Path) -> None:
    """A file with a CPF-shaped value must be flagged (exit 1)."""
    bad = tmp_path / "bad.txt"
    bad.write_text("user CPF is 123.456.789-09 in this log\n")  # job-hunter:allow-pii

    script = REPO_ROOT / "skills" / "job-hunter" / "scripts" / "lint_secret_leaks.py"
    result = subprocess.run(
        [sys.executable, str(script), "--paths", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, f"linter should fail; got rc={result.returncode}"
    assert "cpf" in result.stderr.lower()


def test_secret_leak_linter_allows_marker(tmp_path: Path) -> None:
    """The `# job-hunter:allow-pii` marker suppresses a line."""
    f = tmp_path / "fixture.txt"
    f.write_text("fixture CPF 123.456.789-09  # job-hunter:allow-pii\n")
    script = REPO_ROOT / "skills" / "job-hunter" / "scripts" / "lint_secret_leaks.py"
    result = subprocess.run(
        [sys.executable, str(script), "--paths", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
