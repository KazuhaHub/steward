"""Keep Canvas out of steward-core, except where it is still being moved out.

Fails when:

* any file under ``steward-core/src/steward`` imports ``canvas_mcp``;
* a file outside the Canvas connector names Canvas and is not listed in
  ``scripts/canvas-allowlist.txt`` (a new file must not add Canvas vocabulary);
* a listed file no longer names Canvas (remove it from the list, so the list
  only ever shrinks).

Some listed files must keep their Canvas names for good: persisted names such
as the migration version table, keyring salts, AAD labels and cookies keep
existing databases and grants valid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "steward-core" / "src" / "steward"
CONNECTOR = SRC / "connectors" / "canvas"
ALLOWLIST = ROOT / "scripts" / "canvas-allowlist.txt"

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+canvas_mcp\b", re.MULTILINE)
CANVAS_RE = re.compile(r"canvas", re.IGNORECASE)


def listed() -> set[str]:
    lines = ALLOWLIST.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def main() -> int:
    allowed = listed()
    problems: list[str] = []
    naming: set[str] = set()
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sql", ".json", ".md"}:
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py" and IMPORT_RE.search(text):
            problems.append(f"{rel}: imports canvas_mcp")
        if CONNECTOR in path.parents:
            continue
        if CANVAS_RE.search(text):
            naming.add(rel)
    for rel in sorted(naming - allowed):
        problems.append(f"{rel}: names Canvas outside the connector and is not allowlisted")
    for rel in sorted(allowed - naming):
        problems.append(f"{rel}: no longer names Canvas; remove it from {ALLOWLIST.name}")
    for problem in problems:
        print(problem)
    if not problems:
        print(f"ok: {len(naming)} allowlisted files still name Canvas")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
