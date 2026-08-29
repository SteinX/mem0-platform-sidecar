import tomllib
from pathlib import Path

import mem0_sidecar


def test_package_has_version() -> None:
    assert mem0_sidecar.__version__ == "0.3.9"


def test_runtime_version_matches_package_metadata() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )

    assert mem0_sidecar.__version__ == pyproject["project"]["version"]
