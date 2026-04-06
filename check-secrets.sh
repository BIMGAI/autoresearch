#!/bin/bash
# Pre-commit secret leak scanner.
# Catches real API keys before they get committed.
#
# Install as git hook:
#   cp check-secrets.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or run manually:
#   bash check-secrets.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
FAILED=0

echo "Scanning staged files for secrets..."

# Get list of staged files (only added/modified, skip deleted)
STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)
if [ -z "$STAGED" ]; then
    # Not in a git commit context — scan all tracked files
    STAGED=$(git ls-files 2>/dev/null)
fi

# 1. Block .env from being committed
if echo "$STAGED" | grep -qE '^\.env$'; then
    echo -e "${RED}BLOCKED: .env is staged for commit! Remove it:${NC}"
    echo "  git reset HEAD .env"
    FAILED=1
fi

# 2. Scan for high-entropy strings that look like real API keys
# Real keys are long base64/hex strings. Placeholders like "..." or "your_key_here" are fine.
for file in $STAGED; do
    [ -f "$file" ] || continue

    # Skip binary files and images
    case "$file" in
        *.png|*.jpg|*.jpeg|*.gif|*.ico|*.woff*|*.ttf|*.lock|*.pyc) continue ;;
    esac

    # Twitter/X API keys — catch any value 20+ chars that isn't a placeholder
    # Covers: KEY=abc123..., KEY="abc123...", KEY: "abc123..."
    if grep -Pn '(CONSUMER_KEY|CONSUMER_SECRET|ACCESS_TOKEN|ACCESS_TOKEN_SECRET|BEARER)\s*[=:]\s*["\x27]?[A-Za-z0-9_-]{20,}["\x27]?' "$file" 2>/dev/null | grep -vP '(your_|_here|\.\.\.|\.\.\.|example|placeholder|CHANGEME)' 2>/dev/null; then
        echo -e "${RED}BLOCKED: Possible real X/Twitter API key in $file${NC}"
        FAILED=1
    fi

    # Generic long secrets (40+ char strings assigned to secret-like variable names)
    if grep -Pn '(?i)(secret|token|key|password|credential|auth)\s*[=:]\s*["\x27][A-Za-z0-9+/=_-]{40,}["\x27]' "$file" 2>/dev/null | grep -vP '(your_|_here|\.\.\.|\.\.\.|example|placeholder|CHANGEME|\$\{\{)' 2>/dev/null; then
        echo -e "${RED}BLOCKED: Possible secret value in $file${NC}"
        FAILED=1
    fi

    # OpenAI / Anthropic API keys
    if grep -Pn '(sk-[a-zA-Z0-9]{20,}|sk-ant-[a-zA-Z0-9]{20,})' "$file" 2>/dev/null; then
        echo -e "${RED}BLOCKED: Possible OpenAI/Anthropic API key in $file${NC}"
        FAILED=1
    fi

    # AWS keys
    if grep -Pn 'AKIA[0-9A-Z]{16}' "$file" 2>/dev/null; then
        echo -e "${RED}BLOCKED: Possible AWS access key in $file${NC}"
        FAILED=1
    fi
done

if [ $FAILED -eq 1 ]; then
    echo ""
    echo -e "${RED}COMMIT BLOCKED — secrets detected.${NC}"
    echo "Fix the issues above, then try again."
    echo "If these are false positives, review carefully before using: git commit --no-verify"
    exit 1
else
    echo -e "${GREEN}No secrets found. Safe to commit.${NC}"
    exit 0
fi
