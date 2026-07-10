"""
AI DBRE — Proactive Scanning Scheduler

Runs periodic health checks and feeds detected issues to the agent
for deeper analysis. This is where the agent becomes proactive —
it doesn't wait for you to ask, it tells you when something is wrong.

Usage:
    # Run a one-shot health check (no scheduling)
    python scheduler/runner.py --once

    # Run every 5 minutes
    python scheduler/runner.py --interval 300

    # Run every 5 minutes and feed critical alerts to the agent
    python scheduler/runner.py --interval 300 --agent
"""

import sys
import os
import json
import time
import signal
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.health_check import run_health_check, HealthCheckConfig


# Graceful shutdown
_running = True


def _signal_handler(sig, frame):
    global _running
    print("\n\n⏹  Stopping scheduler...")
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def format_report(report: dict) -> str:
    """Format health check report for terminal output."""
    lines = []
    status = report["overall_status"]
    summary = report["summary"]
    timestamp = report["timestamp"]

    # Status icon
    icon = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "🔴"}.get(status, "❓")

    lines.append(f"\n{'═' * 70}")
    lines.append(f"  {icon}  HEALTH CHECK — {status}")
    lines.append(f"  📅  {timestamp}")
    lines.append(f"{'═' * 70}")

    if summary["total_alerts"] == 0:
        lines.append("\n  All checks passed. Database is healthy.\n")
        return "\n".join(lines)

    lines.append(f"\n  🔴 Critical: {summary['critical']}  |  ⚠️  Warning: {summary['warning']}\n")

    # Group alerts by check name
    alerts_by_check: dict[str, list] = {}
    for alert in report["alerts"]:
        check = alert["check_name"]
        if check not in alerts_by_check:
            alerts_by_check[check] = []
        alerts_by_check[check].append(alert)

    for check_name, alerts in alerts_by_check.items():
        lines.append(f"  ── {check_name.replace('_', ' ').title()} ──")
        for alert in alerts:
            sev_icon = "🔴" if alert["severity"] == "CRITICAL" else "⚠️"
            table = f" [{alert['table_name']}]" if alert["table_name"] else ""
            lines.append(f"    {sev_icon}{table} {alert['message']}")
            lines.append(f"       → {alert['recommendation']}")
        lines.append("")

    lines.append(f"{'═' * 70}\n")
    return "\n".join(lines)


def feed_to_agent(report: dict):
    """
    Send critical alerts to the agent for deeper investigation.
    The agent receives the alert summary and autonomously investigates.
    """
    critical_alerts = [a for a in report["alerts"] if a["severity"] == "CRITICAL"]

    if not critical_alerts:
        return

    from langchain_core.messages import HumanMessage
    from agent.graph import app
    import uuid

    # Build a prompt from the alerts
    alert_summary = "\n".join(
        f"- [{a['check_name']}] {a['table_name'] or 'N/A'}: {a['message']}"
        for a in critical_alerts
    )

    prompt = (
        f"PROACTIVE HEALTH CHECK detected {len(critical_alerts)} critical issues:\n\n"
        f"{alert_summary}\n\n"
        f"Please investigate each critical issue, run the appropriate diagnostic "
        f"tools, and provide a detailed analysis with specific fix recommendations."
    )

    print("\n🤖 Feeding critical alerts to AI DBRE agent for deep analysis...\n")

    config = {"configurable": {"thread_id": f"healthcheck-{uuid.uuid4()}"}}

    try:
        events = app.stream(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
            stream_mode="values",
        )

        for event in events:
            last_msg = event["messages"][-1]
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    print(f"   🔧 Calling: {tc['name']}...", flush=True)
            elif hasattr(last_msg, "content") and isinstance(last_msg.content, str):
                if last_msg.type == "ai" and not getattr(last_msg, "tool_calls", None):
                    print(f"\n{last_msg.content}")
    except Exception as e:
        print(f"\n❌ Agent analysis failed: {e}")


def run_once(use_agent: bool = False) -> dict:
    """Run a single health check."""
    report = run_health_check()
    print(format_report(report))

    if use_agent and report["summary"]["critical"] > 0:
        feed_to_agent(report)

    return report


def run_scheduled(interval_seconds: int, use_agent: bool = False):
    """Run health checks on a fixed interval."""
    print(f"\n🔄 AI DBRE Proactive Scanner started")
    print(f"   Interval: every {interval_seconds} seconds")
    print(f"   Agent analysis: {'enabled' if use_agent else 'disabled'}")
    print(f"   Press Ctrl+C to stop\n")

    check_count = 0
    while _running:
        check_count += 1
        print(f"── Check #{check_count} at {datetime.now().strftime('%H:%M:%S')} ──")

        try:
            run_once(use_agent=use_agent)
        except Exception as e:
            print(f"❌ Health check failed: {e}")

        # Wait for next interval (check _running flag every second)
        for _ in range(interval_seconds):
            if not _running:
                break
            time.sleep(1)

    print("✅ Scanner stopped.")


def main():
    parser = argparse.ArgumentParser(description="AI DBRE Proactive Health Scanner")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single health check and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between health checks (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Feed critical alerts to the AI agent for deep analysis",
    )

    args = parser.parse_args()

    if args.once:
        run_once(use_agent=args.agent)
    else:
        run_scheduled(interval_seconds=args.interval, use_agent=args.agent)


if __name__ == "__main__":
    main()
