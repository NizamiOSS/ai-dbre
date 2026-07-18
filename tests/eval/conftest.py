"""
Pytest configuration for the eval harness.

Provides:
  - --llm-judge flag to enable LLM-as-judge scoring
  - --pass-threshold flag to set minimum passing score
  - --eval-category flag to filter scenarios by category
  - Shared database connection fixture
  - Scenario parametrization
"""

import pytest

from tests.eval.scenarios import load_all_scenarios
from tests.eval.runner import get_db_connection


# ─── Custom CLI options ──────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--llm-judge",
        action="store_true",
        default=False,
        help="Enable LLM-as-judge scoring (costs one API call per scenario)",
    )
    parser.addoption(
        "--pass-threshold",
        type=float,
        default=0.70,
        help="Minimum composite score to pass a scenario (default: 0.70)",
    )
    parser.addoption(
        "--eval-category",
        type=str,
        default=None,
        help="Only run scenarios in this category (e.g. 'index', 'bloat')",
    )


# ─── Fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def db_connection():
    """Shared database connection for setup/teardown SQL."""
    conn = get_db_connection()
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def use_llm_judge(request):
    """Whether to run LLM-as-judge scoring."""
    return request.config.getoption("--llm-judge")


@pytest.fixture(scope="session")
def pass_threshold(request):
    """Minimum score to pass."""
    return request.config.getoption("--pass-threshold")


@pytest.fixture(scope="session")
def eval_category(request):
    """Category filter for scenarios."""
    return request.config.getoption("--eval-category")


def pytest_generate_tests(metafunc):
    """
    Dynamically parametrize test functions that request a 'scenario' fixture.

    This loads all YAML scenarios and creates one test per scenario,
    with the test ID set to the scenario name for readable output:

        tests/eval/test_eval.py::test_scenario[missing_index]
        tests/eval/test_eval.py::test_scenario[table_bloat]
        ...
    """
    if "scenario" in metafunc.fixturenames:
        category = metafunc.config.getoption("--eval-category", default=None)
        try:
            scenarios = load_all_scenarios(category=category)
        except FileNotFoundError:
            scenarios = []

        metafunc.parametrize(
            "scenario",
            scenarios,
            ids=[s.name for s in scenarios],
        )
