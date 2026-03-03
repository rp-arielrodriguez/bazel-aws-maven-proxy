#!/usr/bin/env bash
set -euo pipefail

# Interactive first-time setup for bazel-aws-maven-proxy
# Checks prerequisites, configures .env, installs tools, starts services.

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

errors=0

echo -e "\n${BOLD}bazel-aws-maven-proxy setup${NC}\n"

# -----------------------------------------------------------------------
# 1. Check prerequisites
# -----------------------------------------------------------------------
echo -e "${BOLD}Checking prerequisites...${NC}"

# mise (if we got here via `mise run setup`, it's present, but check anyway)
if command -v mise &>/dev/null; then
    ok "mise $(mise --version 2>&1 | head -1)"
else
    fail "mise not found — install with: brew install mise"
    errors=$((errors + 1))
fi

# AWS CLI
if command -v aws &>/dev/null; then
    AWS_VERSION=$(aws --version 2>&1 | awk '{print $1}' | cut -d/ -f2)
    AWS_MAJOR=$(echo "$AWS_VERSION" | cut -d. -f1)
    AWS_MINOR=$(echo "$AWS_VERSION" | cut -d. -f2)
    if [ "$AWS_MAJOR" -lt 2 ] || { [ "$AWS_MAJOR" -eq 2 ] && [ "$AWS_MINOR" -lt 9 ]; }; then
        fail "aws-cli $AWS_VERSION too old (need >= 2.9) — brew upgrade awscli"
        errors=$((errors + 1))
    else
        ok "aws-cli $AWS_VERSION"
    fi
else
    fail "aws CLI not found — install with: brew install awscli"
    errors=$((errors + 1))
fi

# Container engine
if command -v podman &>/dev/null; then
    ok "podman $(podman --version 2>&1 | awk '{print $NF}')"
elif command -v docker &>/dev/null; then
    ok "docker $(docker --version 2>&1 | awk '{print $3}' | tr -d ',')"
else
    fail "No container engine — install podman (preferred) or docker"
    errors=$((errors + 1))
fi

# swiftc (optional — webview)
if command -v swiftc &>/dev/null; then
    ok "swiftc (Xcode CLT)"
else
    warn "swiftc not found — SSO login will use browser instead of webview"
    echo "       Install with: xcode-select --install"
fi

if [ "$errors" -gt 0 ]; then
    echo -e "\n${RED}${errors} prerequisite(s) missing. Fix the above and re-run.${NC}"
    exit 1
fi

echo ""

# -----------------------------------------------------------------------
# 2. Configure .env
# -----------------------------------------------------------------------
echo -e "${BOLD}Configuring .env...${NC}"

prompt() {
    local var_name="$1" prompt_text="$2" default="$3" value
    read -rp "  $prompt_text [$default]: " value
    value="${value:-$default}"
    eval "$var_name=\"$value\""
}

if [ -f .env ]; then
    echo "  .env already exists."
    read -rp "  Overwrite with fresh config? [y/N]: " overwrite
    if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
        echo "  Keeping existing .env"
        source .env
        echo ""
    fi
fi

# Only run interactive config if .env doesn't exist or user chose to overwrite
if [ ! -f .env ] || [[ "${overwrite:-}" =~ ^[Yy]$ ]]; then
    echo ""

    # List available AWS profiles for convenience
    if [ -f ~/.aws/config ]; then
        profiles=$(grep '^\[profile ' ~/.aws/config 2>/dev/null | sed 's/\[profile /  - /;s/\]//' || true)
        if [ -n "$profiles" ]; then
            echo "  Available AWS profiles:"
            echo "$profiles"
            echo ""
        fi
    fi

    prompt AWS_PROFILE  "AWS CLI profile"      "default"
    prompt AWS_REGION   "AWS region"            "us-west-2"
    prompt S3_BUCKET    "S3 bucket name"        "your-maven-bucket"
    prompt PROXY_PORT   "Local proxy port"      "8888"
    echo "  SSO login modes:"
    echo "    notify     — asks before opening SSO login (default)"
    echo "    auto       — automatically opens SSO login when needed"
    echo "    silent     — background token refresh only, no UI"
    echo "    standalone — manual only (mise run sso-login)"
    prompt SSO_MODE     "SSO login mode" "notify"

    cat > .env <<EOF
# AWS Configuration
AWS_PROFILE=${AWS_PROFILE}
AWS_REGION=${AWS_REGION}

# S3 Bucket Information
S3_BUCKET_NAME=${S3_BUCKET}

# Proxy Configuration
PROXY_PORT=${PROXY_PORT}
REFRESH_INTERVAL=60000
LOG_LEVEL=info

# SSO Monitor Configuration
CHECK_INTERVAL=60

# SSO Watcher Configuration (macOS launchd)
SSO_COOLDOWN_SECONDS=600
SSO_POLL_SECONDS=5
SSO_LOGIN_MODE=${SSO_MODE}
SSO_PROACTIVE_REFRESH_MINUTES=30

# Container Engine (auto-detect if unset)
# CONTAINER_ENGINE=podman
EOF

    ok "Wrote .env"
    echo ""
fi

# Source it for subsequent steps
source .env

# -----------------------------------------------------------------------
# 3. Verify AWS SSO is configured
# -----------------------------------------------------------------------
echo -e "${BOLD}Checking AWS SSO configuration...${NC}"

AWS_PROFILE="${AWS_PROFILE:-default}"
if aws configure get sso_session --profile "$AWS_PROFILE" &>/dev/null; then
    SSO_SESSION=$(aws configure get sso_session --profile "$AWS_PROFILE" 2>/dev/null)
    ok "Profile '$AWS_PROFILE' uses sso-session '$SSO_SESSION'"
else
    warn "Profile '$AWS_PROFILE' has no sso_session configured"
    echo ""
    read -rp "  Run 'aws configure sso' now? [Y/n]: " do_sso
    if [[ ! "$do_sso" =~ ^[Nn]$ ]]; then
        echo ""
        echo "  When prompted for 'SSO registration scopes', press Enter to accept"
        echo "  the default (sso:account:access) — this enables token refresh."
        echo ""
        aws configure sso
    else
        echo "  Skipping. Run 'aws configure sso' before starting services."
    fi
fi

echo ""

# -----------------------------------------------------------------------
# 4. Install Python via mise
# -----------------------------------------------------------------------
echo -e "${BOLD}Installing tools via mise...${NC}"
mise install --yes
ok "Python $(python3 --version 2>&1 | awk '{print $2}')"
echo ""

# -----------------------------------------------------------------------
# 5. Install SSO watcher (builds webview + launchd agent)
# -----------------------------------------------------------------------
echo -e "${BOLD}Installing SSO watcher...${NC}"
mise run sso-install
echo ""

# -----------------------------------------------------------------------
# 6. Start containers
# -----------------------------------------------------------------------
read -rp "Start containers now? [Y/n]: " start_containers
if [[ ! "$start_containers" =~ ^[Nn]$ ]]; then
    mise run containers:up
    echo ""
    ok "Containers started"
fi

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}Setup complete!${NC}"
echo ""
echo "  Proxy:   http://localhost:${PROXY_PORT:-8888}/"
echo "  Logs:    mise run containers:logs"
echo "  Watcher: mise run sso-status"
echo "  Health:  curl http://localhost:${PROXY_PORT:-8888}/healthz"
echo ""
