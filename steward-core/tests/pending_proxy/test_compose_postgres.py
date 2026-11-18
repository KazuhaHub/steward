"""The optional PostgreSQL override keeps the database private and pinned."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SELFHOST = REPO / "deploy"
OVERRIDE = SELFHOST / "docker-compose.postgres.yml"
WORKFLOW = REPO / ".github" / "workflows" / "canvas-mcp-testing.yml"

IMAGE_RE = re.compile(r"^postgres:(\d+\.\d+)-alpine@sha256:[0-9a-f]{64}$")
























def test_ci_tests_against_the_image_the_compose_override_deploys(postgres) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["test-postgres"]
    assert job["services"]["postgres"]["image"] == postgres["image"]
    assert job["services"]["postgres"]["env"]["POSTGRES_INITDB_ARGS"].replace(" ", "") in (
        "--locale=C--encoding=UTF8",
        "--encoding=UTF8--locale=C",
    )


def test_ci_requires_both_dependency_runtimes_on_postgres() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    postgres = jobs["test-postgres"]
    assert set(postgres["strategy"]["matrix"]["dependencies"]) == {"latest", "locked"}
    assert postgres["strategy"]["fail-fast"] is False
    assert postgres["env"]["CANVAS_MCP_TEST_BACKEND"] == "postgres"
    assert "test-postgres" in jobs["test-enhancements"]["needs"]


