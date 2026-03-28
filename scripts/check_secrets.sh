#!/usr/bin/env bash
# scripts/check_secrets.sh
# Run manually or in CI to catch accidental secret commits.
set -euo pipefail

echo "🔍  Scanning for potential hardcoded secrets..."

PATTERNS=(
    "API_KEY\s*=\s*['\"][^'\"]{8,}"
    "API_SECRET\s*=\s*['\"][^'\"]{8,}"
    "PRIVATE_KEY\s*=\s*['\"][^'\"]{8,}"
    "password\s*=\s*['\"][^'\"]{6,}"
    "-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----"
)

FOUND=0
for pattern in "${PATTERNS[@]}"; do
    if grep -rEn "$pattern" src/ tests/ configs/ --include="*.py" --include="*.yaml" --include="*.json" 2>/dev/null; then
        FOUND=1
    fi
done

if [ "$FOUND" -eq 1 ]; then
    echo ""
    echo "❌  Potential hardcoded secret detected. Move it to .env"
    exit 1
else
    echo "✅  No hardcoded secrets found."
fi
