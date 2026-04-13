#!/bin/bash
# check_secrets.sh
# Scans staged files for patterns that look like real secrets.
# Run before every git commit: bash check_secrets.sh
# Returns exit code 1 if any secrets are found.

echo "🔍 Scanning staged files for secrets..."

FAILED=0

# Patterns that indicate a real secret is present
PATTERNS=(
  ":[0-9]{10}:AA"                          # Telegram bot token format
  "api-key=[a-f0-9-]{36}"                  # Helius API key format (UUID)
  "api_key=[a-f0-9-]{36}"                  # variant
  "TELEGRAM_BOT_TOKEN:.*[0-9]{9,10}:AA"    # filled-in bot token in YAML
  "TELEGRAM_CHANNEL_ID:.*-100[0-9]{10}"    # filled-in channel ID in YAML
)

# Files to check — all staged files
STAGED=$(git diff --cached --name-only 2>/dev/null)

if [ -z "$STAGED" ]; then
  echo "  No staged files to check."
  exit 0
fi

for file in $STAGED; do
  [ -f "$file" ] || continue
  for pattern in "${PATTERNS[@]}"; do
    if grep -qEi "$pattern" "$file" 2>/dev/null; then
      echo "  ❌ POSSIBLE SECRET in: $file (pattern: $pattern)"
      FAILED=1
    fi
  done
done

# Also check for any REPLACE_WITH_* that was replaced with something real
for file in $STAGED; do
  [ -f "$file" ] || continue
  # If k8s.yaml or postgres.yaml no longer contains REPLACE_WITH_, warn
  if [[ "$file" == "k8s.yaml" || "$file" == "postgres.yaml" ]]; then
    if ! grep -q "REPLACE_WITH_" "$file" 2>/dev/null; then
      echo "  ❌ WARNING: $file no longer contains REPLACE_WITH_* placeholders."
      echo "     This may mean real secrets were written into the committed version."
      FAILED=1
    fi
  fi
done

if [ "$FAILED" -eq 1 ]; then
  echo ""
  echo "  ⛔ Commit blocked. Remove secrets before committing."
  echo "  If you filled in k8s.yaml or postgres.yaml with real values,"
  echo "  rename them to k8s.local.yaml / postgres.local.yaml first."
  exit 1
else
  echo "  ✅ No secrets detected. Safe to commit."
  exit 0
fi
