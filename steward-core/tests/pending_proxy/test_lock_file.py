"""The locked versions of the authorization dependencies satisfy the declared ranges."""

import pathlib

import pytest


@pytest.mark.parametrize("name", ["fastmcp", "mcp", "joserfc"])
def test_locked_versions_satisfy_the_declared_ranges(name):
    import tomllib

    from packaging.requirements import Requirement
    from packaging.version import Version

    root = pathlib.Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    req = next(
        Requirement(line)
        for line in project["project"]["dependencies"]
        if Requirement(line).name == name
    )
    packages = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]
    assert Version(next(p["version"] for p in packages if p["name"] == name)) in req.specifier
