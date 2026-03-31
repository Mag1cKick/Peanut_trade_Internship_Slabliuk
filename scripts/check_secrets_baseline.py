"""
scripts/check_secrets_baseline.py
Runs detect-secrets scan and fails if new secrets are found vs the baseline.
Used as a pre-commit hook on Windows where detect-secrets-hook.exe has path issues.
"""

import json
import subprocess
import sys
from pathlib import Path

BASELINE_FILE = Path(".secrets.baseline")


def main() -> int:
    if not BASELINE_FILE.exists():
        print("ERROR: .secrets.baseline not found.")
        print(
            "Run: python -m detect_secrets scan | Out-File -FilePath .secrets.baseline -Encoding utf8"
        )
        return 1

    with open(
        BASELINE_FILE, encoding="utf-8-sig"
    ) as f:  # utf-8-sig handles BOM from PowerShell Out-File
        baseline = json.load(f)

    baseline_secrets: dict = baseline.get("results", {})

    # Run a fresh scan
    result = subprocess.run(
        [sys.executable, "-m", "detect_secrets", "scan"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("detect-secrets scan failed:", result.stderr)
        return 1

    current = json.loads(result.stdout)
    current_secrets: dict = current.get("results", {})

    # Find secrets in current scan that are NOT in the baseline
    new_secrets = {}
    for filepath, findings in current_secrets.items():
        baseline_findings = {
            (f["type"], f["line_number"]) for f in baseline_secrets.get(filepath, [])
        }
        new_findings = [
            f for f in findings if (f["type"], f["line_number"]) not in baseline_findings
        ]
        if new_findings:
            new_secrets[filepath] = new_findings

    if new_secrets:
        print("BLOCKED: New secrets detected!\n")
        for filepath, findings in new_secrets.items():
            for f in findings:
                print(f"  {filepath}:{f['line_number']}  [{f['type']}]")
        print("\nIf this is a false positive, add  # pragma: allowlist secret  to the line.")
        print("If it's real, remove the secret and rotate it immediately.")
        return 1

    print("OK: No new secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
