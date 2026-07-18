"""
AI DBRE — Evaluation Tests

Each YAML scenario in tests/eval/scenarios/ becomes a parametrized test case.

Run:
    pytest tests/eval/test_eval.py -v
    pytest tests/eval/test_eval.py -v --llm-judge
    pytest tests/eval/test_eval.py -v -k missing_index
    pytest tests/eval/test_eval.py -v --eval-category=bloat
"""

import json
import os

import pytest

from tests.eval.scenarios import Scenario
from tests.eval.runner import run_scenario
from tests.eval.scoring import score_scenario


class TestAgentEval:
    """
    Parametrized eval tests — one test per YAML scenario.

    The 'scenario' fixture is automatically parametrized by conftest.py
    using pytest_generate_tests, which loads all YAML files from the
    scenarios/ directory.
    """

    def test_scenario(
        self,
        scenario: Scenario,
        db_connection,
        use_llm_judge: bool,
        pass_threshold: float,
    ):
        """
        Run a single eval scenario and assert it passes.

        Steps:
            1. Setup: execute SQL to induce the problem
            2. Run: give the agent the scenario prompt
            3. Score: check tool calls, root cause, fix recommendation
            4. Teardown: clean up the induced problem
            5. Assert: composite score >= pass_threshold
        """
        # Run the scenario
        trace = run_scenario(scenario, conn=db_connection)

        # Score it
        score = score_scenario(
            scenario, trace, use_llm_judge=use_llm_judge
        )

        # Print detailed results for debugging
        print(score.summary())

        # Optionally save trace for post-mortem analysis
        _save_trace_if_failed(scenario, trace, score, pass_threshold)

        # Assert
        assert score.composite_score >= pass_threshold, (
            f"Scenario '{scenario.name}' scored {score.composite_score:.0%} "
            f"(threshold: {pass_threshold:.0%})\n"
            f"Root cause expected: {scenario.expected.root_cause}\n"
            f"Tools called: {sorted(trace.tool_names_called)}\n"
            f"Response preview: {trace.final_response[:300]}"
        )


def _save_trace_if_failed(
    scenario: Scenario,
    trace,
    score,
    threshold: float,
) -> None:
    """
    Save the full agent trace to a JSON file when a scenario fails.
    Useful for debugging why the agent missed the diagnosis.
    """
    if score.composite_score >= threshold:
        return

    traces_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "eval_traces"
    )
    os.makedirs(traces_dir, exist_ok=True)

    trace_data = {
        "scenario": scenario.name,
        "composite_score": round(score.composite_score, 3),
        "threshold": threshold,
        "tools_called": sorted(trace.tool_names_called),
        "final_response": trace.final_response,
        "duration_seconds": round(trace.duration_seconds, 1),
        "error": trace.error,
        "criteria": [
            {
                "name": c.name,
                "score": round(c.score, 3),
                "details": c.details,
            }
            for c in score.criteria
        ],
        "raw_messages": trace.raw_messages,
    }

    path = os.path.join(traces_dir, f"failed_{scenario.name}.json")
    with open(path, "w") as f:
        json.dump(trace_data, f, indent=2)
    print(f"  Trace saved to: {path}")
