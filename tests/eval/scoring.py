"""
Scoring — evaluate agent diagnostic accuracy.

Three scoring strategies, combined with configurable weights:

1. Tool-call verification (deterministic)
   Did the agent call the required diagnostic tools?

2. Keyword matching (deterministic)
   Does the agent's response mention the expected root cause,
   affected objects, and fix category?

3. LLM-as-judge (semantic, optional)
   A second LLM call scores whether the diagnosis is correct,
   catching cases where the agent says the right thing in
   unexpected phrasing.

Each scenario gets a composite score from 0.0 to 1.0.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from tests.eval.scenarios import Scenario, ExpectedOutcome
from tests.eval.runner import AgentTrace


# ─── Score weights ───────────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "tools_must_call": 0.30,
    "tools_should_call": 0.10,
    "root_cause": 0.35,
    "affected_objects": 0.15,
    "fix_category": 0.10,
}


# ─── Data structures ────────────────────────────────────────────

@dataclass
class CriterionResult:
    """Score for a single evaluation criterion."""

    name: str
    score: float  # 0.0 to 1.0
    weight: float
    details: str = ""

    @property
    def weighted_score(self) -> float:
        return self.score * self.weight


@dataclass
class ScenarioScore:
    """Complete evaluation score for a scenario."""

    scenario_name: str
    criteria: list[CriterionResult] = field(default_factory=list)
    llm_judge_score: Optional[float] = None
    llm_judge_reasoning: str = ""
    error: Optional[str] = None

    @property
    def composite_score(self) -> float:
        """Weighted composite score across all criteria."""
        if not self.criteria:
            return 0.0
        return sum(c.weighted_score for c in self.criteria)

    @property
    def passed(self) -> bool:
        """Whether the scenario passed (score >= threshold)."""
        return self.composite_score >= 0.70

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"\n{'─'*50}",
            f"  {self.scenario_name}: "
            f"{'PASS ✅' if self.passed else 'FAIL ❌'} "
            f"({self.composite_score:.0%})",
            f"{'─'*50}",
        ]
        for c in self.criteria:
            status = "✅" if c.score >= 0.5 else "❌"
            lines.append(
                f"  {status} {c.name}: {c.score:.0%} "
                f"(weight: {c.weight:.0%}) — {c.details}"
            )
        if self.llm_judge_score is not None:
            lines.append(
                f"  🤖 LLM Judge: {self.llm_judge_score:.0%} — "
                f"{self.llm_judge_reasoning[:100]}"
            )
        if self.error:
            lines.append(f"  ⚠️  Error: {self.error}")
        return "\n".join(lines)


# ─── Deterministic scoring functions ─────────────────────────────

def score_tools_must_call(
    trace: AgentTrace, expected: ExpectedOutcome
) -> CriterionResult:
    """
    Score: did the agent call every required tool?
    1.0 = all called, 0.0 = none called, proportional in between.
    """
    if not expected.tools_must_call:
        return CriterionResult(
            name="tools_must_call",
            score=1.0,
            weight=DEFAULT_WEIGHTS["tools_must_call"],
            details="No required tools specified",
        )

    called = trace.tool_names_called
    required = set(expected.tools_must_call)
    found = required & called
    missing = required - called

    score = len(found) / len(required)

    details = f"Called {len(found)}/{len(required)}"
    if missing:
        details += f" — missing: {', '.join(sorted(missing))}"

    return CriterionResult(
        name="tools_must_call",
        score=score,
        weight=DEFAULT_WEIGHTS["tools_must_call"],
        details=details,
    )


def score_tools_should_call(
    trace: AgentTrace, expected: ExpectedOutcome
) -> CriterionResult:
    """
    Score: did the agent call the suggested (but not required) tools?
    """
    if not expected.tools_should_call:
        return CriterionResult(
            name="tools_should_call",
            score=1.0,
            weight=DEFAULT_WEIGHTS["tools_should_call"],
            details="No suggested tools specified",
        )

    called = trace.tool_names_called
    suggested = set(expected.tools_should_call)
    found = suggested & called

    score = len(found) / len(suggested)

    details = f"Called {len(found)}/{len(suggested)}"
    if suggested - called:
        details += f" — skipped: {', '.join(sorted(suggested - called))}"

    return CriterionResult(
        name="tools_should_call",
        score=score,
        weight=DEFAULT_WEIGHTS["tools_should_call"],
        details=details,
    )


def score_root_cause(
    trace: AgentTrace, expected: ExpectedOutcome
) -> CriterionResult:
    """
    Score: does the agent's response identify the correct root cause?

    Uses keyword matching against root_cause_keywords. Each keyword
    found contributes proportionally to the score.
    """
    if not trace.final_response:
        return CriterionResult(
            name="root_cause",
            score=0.0,
            weight=DEFAULT_WEIGHTS["root_cause"],
            details="Agent produced no response",
        )

    response_lower = trace.final_response.lower()
    keywords = expected.root_cause_keywords

    if not keywords:
        # Fall back to splitting the root_cause description into words
        keywords = [
            w for w in expected.root_cause.lower().split()
            if len(w) > 3  # skip short words
        ]

    if not keywords:
        return CriterionResult(
            name="root_cause",
            score=0.5,
            weight=DEFAULT_WEIGHTS["root_cause"],
            details="No keywords to check — manual review needed",
        )

    found = []
    missing = []
    for kw in keywords:
        if kw.lower() in response_lower:
            found.append(kw)
        else:
            missing.append(kw)

    score = len(found) / len(keywords)

    details = f"Matched {len(found)}/{len(keywords)} keywords"
    if missing:
        details += f" — missing: {', '.join(missing[:5])}"

    return CriterionResult(
        name="root_cause",
        score=score,
        weight=DEFAULT_WEIGHTS["root_cause"],
        details=details,
    )


def score_affected_objects(
    trace: AgentTrace, expected: ExpectedOutcome
) -> CriterionResult:
    """
    Score: does the response mention the affected tables/indexes?
    """
    if not expected.affected_objects:
        return CriterionResult(
            name="affected_objects",
            score=1.0,
            weight=DEFAULT_WEIGHTS["affected_objects"],
            details="No affected objects specified",
        )

    if not trace.final_response:
        return CriterionResult(
            name="affected_objects",
            score=0.0,
            weight=DEFAULT_WEIGHTS["affected_objects"],
            details="Agent produced no response",
        )

    response_lower = trace.final_response.lower()
    found = []
    missing = []

    for obj in expected.affected_objects:
        if obj.lower() in response_lower:
            found.append(obj)
        else:
            missing.append(obj)

    # Primary object (first in list) counts more
    if expected.affected_objects:
        primary = expected.affected_objects[0].lower()
        primary_found = primary in response_lower
    else:
        primary_found = True

    # Score: 60% weight on primary object, 40% on others
    if len(expected.affected_objects) == 1:
        score = 1.0 if primary_found else 0.0
    else:
        others_score = (
            (len(found) - (1 if primary_found else 0))
            / (len(expected.affected_objects) - 1)
            if len(expected.affected_objects) > 1
            else 0
        )
        score = (0.6 * (1.0 if primary_found else 0.0)) + (0.4 * others_score)

    details = f"Found {len(found)}/{len(expected.affected_objects)} objects"
    if missing:
        details += f" — missing: {', '.join(missing[:3])}"

    return CriterionResult(
        name="affected_objects",
        score=score,
        weight=DEFAULT_WEIGHTS["affected_objects"],
        details=details,
    )


def score_fix_category(
    trace: AgentTrace, expected: ExpectedOutcome
) -> CriterionResult:
    """
    Score: does the recommendation match the expected fix category?
    """
    if not expected.fix_category:
        return CriterionResult(
            name="fix_category",
            score=1.0,
            weight=DEFAULT_WEIGHTS["fix_category"],
            details="No fix category specified",
        )

    if not trace.final_response:
        return CriterionResult(
            name="fix_category",
            score=0.0,
            weight=DEFAULT_WEIGHTS["fix_category"],
            details="Agent produced no response",
        )

    response_lower = trace.final_response.lower()

    # Check fix keywords first (more specific)
    if expected.fix_keywords:
        found_kw = sum(
            1 for kw in expected.fix_keywords if kw.lower() in response_lower
        )
        score = min(1.0, found_kw / max(1, len(expected.fix_keywords) * 0.5))
        details = f"Matched {found_kw}/{len(expected.fix_keywords)} fix keywords"
    else:
        # Fall back to checking the category name
        category_variants = _fix_category_variants(expected.fix_category)
        found = any(v in response_lower for v in category_variants)
        score = 1.0 if found else 0.0
        details = (
            f"Fix category '{expected.fix_category}' "
            f"{'found' if found else 'NOT found'} in response"
        )

    return CriterionResult(
        name="fix_category",
        score=score,
        weight=DEFAULT_WEIGHTS["fix_category"],
        details=details,
    )


def _fix_category_variants(category: str) -> list[str]:
    """Generate common text variants of a fix category."""
    variants = {
        "create_index": [
            "create index", "add index", "add an index", "adding index",
            "creating an index", "index on", "btree index",
        ],
        "drop_index": [
            "drop index", "remove index", "removing index",
            "unused index", "delete index",
        ],
        "vacuum": [
            "vacuum", "vacuuming", "run vacuum", "vacuum full",
            "autovacuum",
        ],
        "analyze": [
            "analyze", "run analyze", "update statistics",
        ],
        "reindex": [
            "reindex", "rebuild index", "rebuilding index",
        ],
        "config_change": [
            "change setting", "adjust parameter", "increase",
            "decrease", "set to", "work_mem", "shared_buffers",
        ],
    }
    return variants.get(category, [category.replace("_", " ")])


# ─── LLM-as-judge scoring ───────────────────────────────────────

def score_with_llm_judge(
    trace: AgentTrace,
    expected: ExpectedOutcome,
    scenario_description: str = "",
) -> tuple[float, str]:
    """
    Use a second LLM call to semantically evaluate the diagnosis.

    Returns (score, reasoning) where score is 0.0 to 1.0.

    This is optional and costs one API call per scenario. Skip it
    by not passing --llm-judge to pytest.
    """
    try:
        from agent.config import settings

        # Determine which provider to use
        provider = os.getenv(
            "EVAL_LLM_PROVIDER",
            getattr(settings, "llm_provider", "anthropic"),
        )

        judge_prompt = _build_judge_prompt(
            trace, expected, scenario_description
        )

        if provider == "anthropic":
            return _judge_with_anthropic(judge_prompt)
        elif provider == "openai":
            return _judge_with_openai(judge_prompt)
        else:
            return 0.5, f"LLM judge not available for provider: {provider}"

    except Exception as e:
        return 0.5, f"LLM judge error: {e}"


def _build_judge_prompt(
    trace: AgentTrace,
    expected: ExpectedOutcome,
    scenario_description: str,
) -> str:
    """Build the rubric prompt for the LLM judge."""
    return f"""You are evaluating an AI database agent's diagnostic output.

SCENARIO: {scenario_description}

KNOWN ROOT CAUSE (ground truth):
{expected.root_cause}

AGENT'S DIAGNOSIS:
{trace.final_response[:3000]}

TOOLS THE AGENT CALLED:
{', '.join(sorted(trace.tool_names_called)) or 'None'}

EVALUATION RUBRIC:
Score the diagnosis on a scale of 0 to 3:
- 3: Correct root cause identified with evidence. Fix recommendation is appropriate.
- 2: Root cause partially identified, or correct but vague. Some evidence cited.
- 1: Related issues mentioned but wrong root cause, or correct root cause with no evidence.
- 0: Completely wrong diagnosis, or no meaningful output.

Respond with EXACTLY this format (no other text):
SCORE: <number 0-3>
REASONING: <one sentence explaining the score>"""


def _judge_with_anthropic(prompt: str) -> tuple[float, str]:
    """Call Anthropic API for LLM judge scoring."""
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    return _parse_judge_response(response.content[0].text)


def _judge_with_openai(prompt: str) -> tuple[float, str]:
    """Call OpenAI API for LLM judge scoring."""
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    return _parse_judge_response(response.choices[0].message.content)


def _parse_judge_response(text: str) -> tuple[float, str]:
    """Parse the structured judge response into (score, reasoning)."""
    score_match = re.search(r"SCORE:\s*(\d)", text)
    reasoning_match = re.search(r"REASONING:\s*(.+)", text, re.DOTALL)

    if score_match:
        raw_score = int(score_match.group(1))
        normalized = raw_score / 3.0  # Convert 0-3 to 0.0-1.0
    else:
        normalized = 0.5  # Can't parse, neutral score

    reasoning = (
        reasoning_match.group(1).strip()[:200]
        if reasoning_match
        else "Could not parse judge reasoning"
    )

    return normalized, reasoning


# ─── Main scoring function ──────────────────────────────────────

def score_scenario(
    scenario: Scenario,
    trace: AgentTrace,
    use_llm_judge: bool = False,
) -> ScenarioScore:
    """
    Score an agent trace against a scenario's expected outcomes.

    Args:
        scenario: The scenario definition with expected outcomes.
        trace: The captured agent execution trace.
        use_llm_judge: Whether to run the LLM-as-judge (costs one API call).

    Returns:
        ScenarioScore with detailed criterion-level results.
    """
    result = ScenarioScore(scenario_name=scenario.name)

    # Check for agent errors
    if trace.error:
        result.error = trace.error
        # Still try to score what we got — the agent may have
        # partially completed before erroring
        if not trace.final_response and not trace.tool_calls:
            result.criteria = [
                CriterionResult(name=name, score=0.0, weight=weight, details="Agent error")
                for name, weight in DEFAULT_WEIGHTS.items()
            ]
            return result

    expected = scenario.expected

    # Deterministic scoring
    result.criteria = [
        score_tools_must_call(trace, expected),
        score_tools_should_call(trace, expected),
        score_root_cause(trace, expected),
        score_affected_objects(trace, expected),
        score_fix_category(trace, expected),
    ]

    # Optional LLM-as-judge
    if use_llm_judge:
        llm_score, reasoning = score_with_llm_judge(
            trace, expected, scenario.description
        )
        result.llm_judge_score = llm_score
        result.llm_judge_reasoning = reasoning

    return result
