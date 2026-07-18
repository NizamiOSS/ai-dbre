"""
Report — generate evaluation summary reports.

Produces:
  - Terminal-friendly pass/fail summary
  - JSON report for CI integration
  - Detailed per-scenario breakdown
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from tests.eval.scenarios import Scenario
from tests.eval.runner import AgentTrace
from tests.eval.scoring import ScenarioScore


def print_summary(
    scores: list[ScenarioScore],
    pass_threshold: float = 0.70,
) -> None:
    """Print a terminal-friendly eval summary."""
    total = len(scores)
    passed = sum(1 for s in scores if s.composite_score >= pass_threshold)
    failed = total - passed
    avg_score = (
        sum(s.composite_score for s in scores) / total if total else 0
    )

    print("\n" + "=" * 60)
    print("  AI DBRE — EVALUATION REPORT")
    print("=" * 60)
    print(f"  Date:       {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Scenarios:  {total}")
    print(f"  Passed:     {passed} ✅")
    print(f"  Failed:     {failed} ❌")
    print(f"  Avg score:  {avg_score:.0%}")
    print(f"  Threshold:  {pass_threshold:.0%}")
    print("=" * 60)

    # Per-scenario details
    for score in sorted(scores, key=lambda s: s.composite_score):
        print(score.summary())

    # Final verdict
    print("\n" + "=" * 60)
    if failed == 0:
        print("  ✅ ALL SCENARIOS PASSED")
    else:
        print(f"  ❌ {failed}/{total} SCENARIO(S) FAILED")
    print("=" * 60 + "\n")


def generate_json_report(
    scores: list[ScenarioScore],
    traces: dict[str, AgentTrace],
    output_path: Optional[str] = None,
    pass_threshold: float = 0.70,
) -> dict:
    """
    Generate a structured JSON report suitable for CI artifacts.

    Args:
        scores: List of scenario scores.
        traces: Dict mapping scenario name to AgentTrace.
        output_path: If provided, write the JSON report to this file.
        pass_threshold: Score threshold for pass/fail.

    Returns:
        The report as a dict.
    """
    total = len(scores)
    passed = sum(1 for s in scores if s.composite_score >= pass_threshold)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_scenarios": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": f"{passed/total:.0%}" if total else "N/A",
            "average_score": round(
                sum(s.composite_score for s in scores) / total, 3
            )
            if total
            else 0,
            "pass_threshold": pass_threshold,
            "all_passed": passed == total,
        },
        "scenarios": [],
    }

    for score in scores:
        trace = traces.get(score.scenario_name)
        scenario_data = {
            "name": score.scenario_name,
            "composite_score": round(score.composite_score, 3),
            "passed": score.composite_score >= pass_threshold,
            "criteria": [
                {
                    "name": c.name,
                    "score": round(c.score, 3),
                    "weight": c.weight,
                    "weighted_score": round(c.weighted_score, 3),
                    "details": c.details,
                }
                for c in score.criteria
            ],
            "duration_seconds": round(trace.duration_seconds, 1) if trace else None,
            "tools_called": sorted(trace.tool_names_called) if trace else [],
            "error": score.error,
        }

        if score.llm_judge_score is not None:
            scenario_data["llm_judge"] = {
                "score": round(score.llm_judge_score, 3),
                "reasoning": score.llm_judge_reasoning,
            }

        report["scenarios"].append(scenario_data)

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nJSON report written to: {output_path}")

    return report


def main():
    """
    CLI entry point: run all scenarios and generate a report.

    Usage:
        python -m tests.eval.report [--llm-judge] [--output report.json]
    """
    import argparse

    from tests.eval.scenarios import load_all_scenarios
    from tests.eval.runner import run_all_scenarios
    from tests.eval.scoring import score_scenario

    parser = argparse.ArgumentParser(description="AI DBRE Evaluation Report")
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Enable LLM-as-judge scoring (costs one API call per scenario)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Path to write JSON report (default: eval_report.json)",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Only run scenarios in this category",
    )
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=0.70,
        help="Minimum score to pass (default: 0.70)",
    )
    args = parser.parse_args()

    # Load and run
    scenarios = load_all_scenarios(category=args.category)
    print(f"Loaded {len(scenarios)} scenario(s)")

    traces = run_all_scenarios(scenarios)

    # Score
    scores = []
    for scenario in scenarios:
        trace = traces[scenario.name]
        score = score_scenario(
            scenario, trace, use_llm_judge=args.llm_judge
        )
        scores.append(score)

    # Report
    print_summary(scores, pass_threshold=args.pass_threshold)

    output_path = args.output or "eval_report.json"
    generate_json_report(
        scores, traces, output_path=output_path,
        pass_threshold=args.pass_threshold,
    )

    # Exit code for CI
    all_passed = all(
        s.composite_score >= args.pass_threshold for s in scores
    )
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
