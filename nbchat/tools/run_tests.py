"""Run the repository's pytest suite and return a JSON summary.

The function returns a stringified JSON object that contains:
  * passed   – number of tests that passed
  * failed   – number of tests that failed
  * errors   – number of errored tests
  * output   – the raw stdout from pytest

If anything goes wrong, the JSON payload contains an `error` key.
"""

import json
import subprocess
import re
from pathlib import Path
from typing import Dict


def _run_tests() -> str:
    """Execute `pytest -q` in the repository root and return JSON."""
    try:
        proc = subprocess.run(
            ["pytest", "-q"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[2],  # repo root
        )

        # The final non‑empty line usually contains the summary, e.g.
        # "1 passed in 0.01s" or "1 passed, 2 failed, 1 error".
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        summary_line = lines[-1] if lines else ""

        # Extract numbers using regex.
        passed = failed = errors = 0
        passed_match = re.search(r"(?P<passed>\d+)\s+passed", summary_line)
        if passed_match:
            passed = int(passed_match.group("passed"))
        failed_match = re.search(r"(?P<failed>\d+)\s+failed", summary_line)
        if failed_match:
            failed = int(failed_match.group("failed"))
        error_match = re.search(r"(?P<errors>\d+)\s+error", summary_line)
        if error_match:
            errors = int(error_match.group("errors"))

        result: Dict = {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "output": proc.stdout,
        }
        return json.dumps(result)

    except Exception as exc:
        return json.dumps({"error": str(exc)})

# Public attributes for the discovery logic
func = _run_tests
name = "run_tests"
description = "Run the repository's pytest suite and return the results."
__all__ = ["func", "name", "description"]
