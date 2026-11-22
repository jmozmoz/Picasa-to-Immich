"""Code-style checks: black, pylint, mypy and pyright.

Settings for every tool are in pyproject.toml. A check is skipped when its
tool is not installed; install them with: pip install -e ".[lint]"
Run only these checks with `pytest -m style`, or skip them with `pytest -m "not style"`.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SOURCES = ["picasa_immich", "tests", "picasa_to_immich.py"]

# pytest idioms that pylint reports in test code only.
PYLINT_TEST_DISABLES = [
    "missing-module-docstring",
    "missing-class-docstring",
    "missing-function-docstring",
    "redefined-outer-name",  # fixtures are passed as same-named arguments
    "protected-access",  # tests may check private helpers
    "too-few-public-methods",  # small fakes
    "unused-argument",  # fakes keep the signatures of the real API
]

pytestmark = pytest.mark.style


def run_tool(module: str, *args: str) -> None:
    """Run `python -m module args` in the project folder; fail with its output."""
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not installed (pip install -e '.[lint]')")
    result = subprocess.run(
        [sys.executable, "-m", module, *args], cwd=PROJECT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{module} found problems:\n{result.stdout}\n{result.stderr}"


def test_black():
    run_tool("black", "--check", "--diff", "--quiet", *SOURCES)


def test_pylint_package():
    run_tool("pylint", "picasa_immich", "picasa_to_immich.py")


def test_pylint_tests():
    run_tool("pylint", "--disable=" + ",".join(PYLINT_TEST_DISABLES), "tests")


def test_mypy():
    run_tool("mypy")


def test_pyright():
    # Use the interpreter running the tests, so pyright finds pytest and requests.
    run_tool("pyright", "--pythonpath", sys.executable)
