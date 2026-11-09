#!/usr/bin/env python3
"""Repository hygiene checks. Needs only the Python standard library and no AWS credentials.

Run from anywhere:  python scripts/check_repo.py

Checks (all on git-tracked files, or on the working tree when git is unavailable):

1. No unresolved placeholders (TODO, FIXME, TBD, XXX, lorem ipsum, REPLACE_ME, ...).
2. Markdown links to AWS, Strands and Kiro sites carry the TRK tracking parameters.
3. No em dashes in the documentation files.
4. No retired default model ID (Claude Sonnet 4 reached end of life on 2026-10-14).
5. Each README keeps the Contributing, Security and License sections.
6. No executed notebooks or notebook outputs are committed.

Exit code 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve().relative_to(ROOT).as_posix()

TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".sh", ".txt", ".ini", ".ipynb", ".toml", ".cfg"}
# Files that describe the checks themselves, or record history, may mention the banned strings.
PLACEHOLDER_EXEMPT = {THIS_FILE, "docs/AUDIT.md"}
RETIRED_MODEL_EXEMPT = PLACEHOLDER_EXEMPT | {"AGENTS.md"}  # AGENTS.md names the retired model on purpose
DOC_FILES_GLOBS = ("README.md", "*/README.md", "AGENTS.md", "*/AGENTS.md")  # em dash and TRK rules
TRK_REQUIRED_HOSTS = {
    "aws.amazon.com",
    "docs.aws.amazon.com",
    "community.aws",
    "builder.aws.com",
    "strandsagents.com",
    "kiro.dev",
}
TRK_PARAMS = "trk=87c4c426-cddf-4799-a299-273337552ad8"
# Standard AWS security text that other AWS sample repositories use verbatim.
TRK_EXEMPT_URLS = {"http://aws.amazon.com/security/vulnerability-reporting/"}

PLACEHOLDER_PATTERN = re.compile(
    r"\b(?:TODO|FIXME|TBD|XXX)\b|(?i:lorem ipsum|REPLACE_ME|INSERT[_ ]HERE|<your[-_ ][a-z0-9_ -]+ here>)"
)
RETIRED_MODEL_PATTERN = re.compile(r"claude-sonnet-4-20250514|claude-sonnet-4-6-20250514")
LINK_PATTERN = re.compile(r"\]\((https?://[^)\s]+)\)|<(https?://[^>\s]+)>|(?<![(\"'<])(https?://[^\s)>\"'`]+)")
EM_DASH = "\u2014"


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        ).stdout.decode("utf-8")
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".build", "deployment_package"}
        names = [
            p.relative_to(ROOT).as_posix()
            for p in ROOT.rglob("*")
            if p.is_file() and not skip.intersection(p.relative_to(ROOT).parts)
        ]
    # A file removed in the working tree but still listed by git is skipped.
    return [ROOT / n for n in names if (ROOT / n).is_file() and not (ROOT / n).is_symlink()]


def read_text(path: Path) -> str | None:
    if path.suffix not in TEXT_SUFFIXES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def doc_files(files: list[Path]) -> list[Path]:
    wanted = {p for pattern in DOC_FILES_GLOBS for p in ROOT.glob(pattern)}
    return [f for f in files if f in wanted]


def check_placeholders(files: list[Path]) -> list[str]:
    problems = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        text = read_text(path)
        if text is None or rel in PLACEHOLDER_EXEMPT or "/tests/" in f"/{rel}":
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = PLACEHOLDER_PATTERN.search(line)
            if match:
                problems.append(f"{rel}:{number}: unresolved placeholder '{match.group(0)}'")
    return problems


def check_trk(docs: list[Path]) -> list[str]:
    problems = []
    for path in docs:
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in LINK_PATTERN.finditer(line):
                url = next(g for g in match.groups() if g)
                host = (urlparse(url).hostname or "").lower()
                if host in TRK_REQUIRED_HOSTS and url not in TRK_EXEMPT_URLS and TRK_PARAMS not in url:
                    problems.append(f"{rel}:{number}: link without TRK parameters: {url}")
    return problems


def check_em_dash(docs: list[Path]) -> list[str]:
    problems = []
    for path in docs:
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if EM_DASH in line:
                problems.append(f"{rel}:{number}: em dash")
    return problems


def check_retired_model(files: list[Path]) -> list[str]:
    problems = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        text = read_text(path)
        if text is None or rel in RETIRED_MODEL_EXEMPT or "/tests/" in f"/{rel}":
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if RETIRED_MODEL_PATTERN.search(line):
                problems.append(f"{rel}:{number}: retired model ID")
    return problems


def check_readme_sections(docs: list[Path]) -> list[str]:
    problems = []
    for path in docs:
        if path.name != "README.md":
            continue
        rel = path.relative_to(ROOT).as_posix()
        headings = {m.group(1).strip().lower() for m in re.finditer(r"^## (.+)$", path.read_text(encoding="utf-8"), re.M)}
        for required in ("contributing", "security", "license"):
            if required not in headings:
                problems.append(f"{rel}: missing '## {required.title()}' section")
    return problems


def check_notebooks(files: list[Path]) -> list[str]:
    problems = []
    for path in files:
        if path.suffix != ".ipynb":
            continue
        rel = path.relative_to(ROOT).as_posix()
        if path.stem.endswith("_executed"):
            problems.append(f"{rel}: executed notebooks must not be committed")
            continue
        notebook = json.loads(path.read_text(encoding="utf-8"))
        for index, cell in enumerate(notebook.get("cells", [])):
            if cell.get("cell_type") == "code" and (cell.get("outputs") or cell.get("execution_count")):
                problems.append(f"{rel}: cell {index} has stored outputs; strip them")
    return problems


def main() -> int:
    files = tracked_files()
    docs = doc_files(files)
    results = {
        "placeholders": check_placeholders(files),
        "trk links": check_trk(docs),
        "em dashes": check_em_dash(docs),
        "retired model id": check_retired_model(files),
        "readme sections": check_readme_sections(docs),
        "notebooks": check_notebooks(files),
    }
    failed = False
    for name, problems in results.items():
        print(f"[{'FAIL' if problems else ' ok '}] {name}")
        for problem in problems:
            print(f"        {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
