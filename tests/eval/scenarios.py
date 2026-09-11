"""
Scenario loader — reads YAML eval scenario files into typed dataclasses.

Each scenario defines:
  - setup SQL to induce a known problem
  - a prompt to give the agent
  - expected outcomes (root cause, tools, affected objects, fix)
  - teardown SQL to clean up
"""

import os
import glob
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class ExpectedOutcome:
    """What the agent should find."""

    root_cause: str
    root_cause_keywords: list[str] = field(default_factory=list)
    affected_objects: list[str] = field(default_factory=list)
    tools_must_call: list[str] = field(default_factory=list)
    tools_must_call_any: list[list[str]] = field(default_factory=list)
    tools_should_call: list[str] = field(default_factory=list)
    fix_category: str = ""
    fix_keywords: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    """A single eval test case."""

    name: str
    description: str
    category: str
    setup: list[str]
    prompt: str
    expected: ExpectedOutcome
    teardown: list[str]
    file_path: str = ""

    def __str__(self) -> str:
        return f"Scenario({self.name}, category={self.category})"


def load_scenario(path: str) -> Scenario:
    """Load a single scenario from a YAML file."""
    with open(path, "r") as f:
        data = yaml.safe_load(f)

    _validate_scenario_data(data, path)

    expected_data = data["expected"]
    expected = ExpectedOutcome(
        root_cause=expected_data["root_cause"],
        root_cause_keywords=expected_data.get("root_cause_keywords", []),
        affected_objects=expected_data.get("affected_objects", []),
        tools_must_call=expected_data.get("tools_must_call", []),
        tools_should_call=expected_data.get("tools_should_call", []),
        fix_category=expected_data.get("fix_category", ""),
        fix_keywords=expected_data.get("fix_keywords", []),
    )

    return Scenario(
        name=data["name"],
        description=data.get("description", ""),
        category=data.get("category", "unknown"),
        setup=data["setup"],
        prompt=data["prompt"],
        expected=expected,
        teardown=data.get("teardown", []),
        file_path=path,
    )


def load_all_scenarios(
    scenarios_dir: Optional[str] = None,
    category: Optional[str] = None,
) -> list[Scenario]:
    """
    Load all YAML scenarios from the scenarios directory.

    Args:
        scenarios_dir: Path to scenarios folder. Defaults to ./scenarios
                       relative to this file.
        category: If set, only load scenarios matching this category.

    Returns:
        List of Scenario objects, sorted by name.
    """
    if scenarios_dir is None:
        scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")

    pattern = os.path.join(scenarios_dir, "*.yaml")
    files = sorted(glob.glob(pattern))

    if not files:
        raise FileNotFoundError(
            f"No scenario YAML files found in {scenarios_dir}"
        )

    scenarios = []
    for path in files:
        try:
            scenario = load_scenario(path)
            if category is None or scenario.category == category:
                scenarios.append(scenario)
        except (yaml.YAMLError, ValueError) as e:
            print(f"WARNING: Skipping {path}: {e}")

    return scenarios


def _validate_scenario_data(data: dict, path: str) -> None:
    """Validate required fields exist in the YAML data."""
    required_top = ["name", "setup", "prompt", "expected"]
    for key in required_top:
        if key not in data:
            raise ValueError(f"Missing required field '{key}' in {path}")

    expected = data["expected"]
    if "root_cause" not in expected:
        raise ValueError(f"Missing 'expected.root_cause' in {path}")

    if not isinstance(data["setup"], list):
        raise ValueError(f"'setup' must be a list of SQL strings in {path}")

    if "tools_must_call" not in expected and "tools_must_call_any" not in expected:
        raise ValueError(
            f"Missing 'expected.tools_must_call' or 'expected.tools_must_call_any' in {path} — "
            f"at least one required tool must be specified"
        )
