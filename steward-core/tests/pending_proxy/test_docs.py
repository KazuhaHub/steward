"""Validate deployable v1 configuration and operator/security documentation."""

from __future__ import annotations

from pathlib import Path



ROOT = Path(__file__).resolve().parents[2]
SELFHOST = ROOT / "deploy"






















def test_independent_fork_notice_and_unclaimed_client_table():
    content = text("README.md")
    opening = content.split("## ")[0]
    assert "independent fork" in opening and "not endorsed" in opening
    assert "vishalsachdev/canvas-mcp" in opening and "small" in opening
    assert (
        "independent fork"
        in (ROOT / "README.md").read_text(encoding="utf-8").split("## ")[0]
    )
    table = content.split("### Tested clients", 1)[1].split(
        "### Staging acceptance", 1
    )[0]
    assert "Only `Passed` rows" in table
    results = {"Not tested", "Passed", "Partial", "Failed"}
    rows = [
        line
        for line in table.splitlines()
        if line.startswith("| claude.ai web") or line.startswith("| Claude ")
    ]
    assert len(rows) == 4
    for row in rows:
        columns = [value.strip() for value in row.strip("|").split("|")]
        assert len(columns) == 8 and columns[5] in results
        if columns[5] != "Not tested":
            assert all(columns[index] not in {"", "-"} for index in (1, 2, 3, 4, 6))
    assert "OPERATOR.md" in content
    assert "db/migrations/README.md" in text("OPERATOR.md")














def test_custody_documentation_explains_the_operator_decryption_boundary():
    content = text("README.md")
    assert "## Custody and privacy boundary" in content
    assert "decrypt every" in content and "compromised" in content
    assert "plaintext SQL" in content and "audit history" in content
    assert "ACCOUNT_SESSION_SECRET" in content and "AES-256-GCM" in content
    root = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "README.md#custody-and-privacy-boundary" in root
