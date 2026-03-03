#!/usr/bin/env bash
set -eo pipefail

# Interactive first-time setup for bazel-aws-maven-proxy
# Checks prerequisites, configures .env, installs tools, starts services.

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

prompt() {
    local var_name="$1" prompt_text="$2" default="$3" value
    read -rp "  $prompt_text [$default]: " value || true
    printf -v "$var_name" '%s' "${value:-$default}"
}

errors=0

echo -e "\n${BOLD}bazel-aws-maven-proxy setup${NC}\n"

# -----------------------------------------------------------------------
# 1. Check prerequisites
# -----------------------------------------------------------------------
echo -e "${BOLD}Checking prerequisites...${NC}"

if command -v mise &>/dev/null; then
    ok "mise $(mise --version 2>&1 | head -1)"
else
    fail "mise not found — install with: brew install mise"
    errors=$((errors + 1))
fi

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

if command -v podman &>/dev/null; then
    ok "podman $(podman --version 2>&1 | awk '{print $NF}')"
elif command -v docker &>/dev/null; then
    ok "docker $(docker --version 2>&1 | awk '{print $3}' | tr -d ',')"
else
    fail "No container engine — install podman (preferred) or docker"
    errors=$((errors + 1))
fi

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

WRITE_ENV=true
if [ -f .env ]; then
    echo "  .env already exists."
    read -rp "  Overwrite with fresh config? [y/N]: " overwrite || true
    if [[ "$overwrite" =~ ^[Yy]$ ]]; then
        WRITE_ENV=true
    else
        echo "  Keeping existing .env"
        WRITE_ENV=false
    fi
fi

if [ "$WRITE_ENV" = true ]; then
    echo ""

    # List available AWS profiles
    if [ -f ~/.aws/config ]; then
        profiles=$(grep '^\[profile ' ~/.aws/config 2>/dev/null | sed 's/\[profile /  - /;s/\]//' || true)
        if [ -n "${profiles:-}" ]; then
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
AWS_PROFILE="${AWS_PROFILE}"
AWS_REGION="${AWS_REGION}"

# S3 Bucket Information
S3_BUCKET_NAME="${S3_BUCKET}"

# Proxy Configuration
PROXY_PORT="${PROXY_PORT}"
REFRESH_INTERVAL=60000
LOG_LEVEL=info

# SSO Monitor Configuration
CHECK_INTERVAL=60

# SSO Watcher Configuration (macOS launchd)
SSO_COOLDOWN_SECONDS=600
SSO_POLL_SECONDS=5
SSO_LOGIN_MODE="${SSO_MODE}"
SSO_PROACTIVE_REFRESH_MINUTES=30

# Container Engine (auto-detect if unset)
# CONTAINER_ENGINE=podman
EOF

    ok "Wrote .env"
fi

echo ""

# Source .env for subsequent steps (unset vars are OK)
set +u
source .env

AWS_PROFILE="${AWS_PROFILE:-default}"
S3_BUCKET_NAME="${S3_BUCKET_NAME:-}"

# -----------------------------------------------------------------------
# 3. Install Python via mise
# -----------------------------------------------------------------------
echo -e "${BOLD}Installing tools via mise...${NC}"
if ! mise install --yes; then
    warn "mise install failed — you may need to run 'mise install' manually"
fi
ok "Python $(python3 --version 2>&1 | awk '{print $2}')"
echo ""

# -----------------------------------------------------------------------
# 4. Build webview + install launchd agent
# -----------------------------------------------------------------------
echo -e "${BOLD}Installing SSO watcher...${NC}"
if ! mise run sso-install; then
    warn "SSO watcher install failed — run 'mise run sso-install' manually"
fi
echo ""

# -----------------------------------------------------------------------
# 5. macOS permission pre-flight
# -----------------------------------------------------------------------
echo -e "${BOLD}Checking macOS permissions...${NC}"

# Skip permission checks in headless/SSH sessions (no GUI)
if [ -z "${DISPLAY:-}" ] && [ -z "${TERM_PROGRAM:-}" ] && ! pgrep -q WindowServer 2>/dev/null; then
    warn "No GUI session detected (SSH?) — skipping permission pre-flight"
else
    echo "  If prompted, grant permissions — these are needed for SSO login dialogs."
    echo ""

    if osascript -e 'tell application "System Events" to return name of current user' &>/dev/null; then
        ok "System Events access"
    else
        warn "System Events access denied — SSO dialog notifications may not work"
        echo "       Grant in: System Settings → Privacy & Security → Accessibility"
    fi

    if osascript -e 'display dialog "Setup complete — SSO watcher permissions verified." buttons {"OK"} default button "OK" giving up after 10' &>/dev/null; then
        ok "Dialog permissions"
    else
        warn "Dialog display failed — SSO notifications may not appear"
    fi
fi

echo ""

# -----------------------------------------------------------------------
# 6. Configure AWS SSO if needed
# -----------------------------------------------------------------------
echo -e "${BOLD}Checking AWS SSO configuration...${NC}"

SSO_CONFIGURED=false

if aws configure get sso_session --profile "$AWS_PROFILE" &>/dev/null; then
    SSO_SESSION=$(aws configure get sso_session --profile "$AWS_PROFILE" 2>/dev/null)
    ok "Profile '$AWS_PROFILE' uses sso-session '$SSO_SESSION'"
    SSO_CONFIGURED=true
elif aws configure get sso_account_id --profile "$AWS_PROFILE" &>/dev/null; then
    ok "Profile '$AWS_PROFILE' has SSO configured (legacy style)"
    SSO_CONFIGURED=true
else
    warn "Profile '$AWS_PROFILE' has no SSO configured"
    echo ""
    read -rp "  Configure SSO now? [Y/n/s(kip)]: " do_sso || true
    if [[ "${do_sso:-}" =~ ^[Ss]$ ]]; then
        echo "  Skipping SSO configuration."
    elif [[ ! "${do_sso:-}" =~ ^[Nn]$ ]]; then
        echo ""
        echo "  When prompted for 'SSO registration scopes', press Enter to accept"
        echo "  the default (sso:account:access) — this enables token refresh."
        echo ""
        if aws configure sso --profile "$AWS_PROFILE"; then
            SSO_CONFIGURED=true
        else
            warn "aws configure sso failed — run 'aws configure sso' manually"
        fi
    else
        echo "  Skipping. Run 'aws configure sso' before starting services."
    fi
fi

echo ""

# -----------------------------------------------------------------------
# 7. First login via webview + validate access
# -----------------------------------------------------------------------
if [ "$SSO_CONFIGURED" = true ]; then
    if aws sts get-caller-identity --profile "$AWS_PROFILE" &>/dev/null; then
        ok "Credentials valid for profile '$AWS_PROFILE'"
    else
        echo -e "${BOLD}Initial SSO login...${NC}"
        echo "  Uses the sandboxed webview to cache IdP credentials for faster future logins."
        echo ""
        # Reuse the watcher's login function (handles --no-browser, webview, fallback)
        export REPO_PATH="$(pwd)"
        export AWS_PROFILE
        if python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_PATH'], 'sso-watcher'))
from watcher import run_aws_sso_login
sys.exit(run_aws_sso_login(os.environ.get('AWS_PROFILE', 'default')))
" 2>&1; then
            ok "SSO login successful"
        else
            warn "SSO login failed — run 'mise run sso-login' to retry"
        fi
    fi

    echo ""

    # Validate S3 bucket access
    if [ -n "$S3_BUCKET_NAME" ] && [ "$S3_BUCKET_NAME" != "your-maven-bucket" ]; then
        echo -e "${BOLD}Validating S3 access...${NC}"
        if aws s3 ls "s3://$S3_BUCKET_NAME/" --profile "$AWS_PROFILE" 2>/dev/null | head -1 &>/dev/null; then
            ok "Can access s3://$S3_BUCKET_NAME/"
        else
            warn "Cannot access s3://$S3_BUCKET_NAME/ with profile '$AWS_PROFILE'"
            echo "       Verify the profile has the correct role and permissions."
        fi
        echo ""
    fi
fi

# -----------------------------------------------------------------------
# 8. Start containers
# -----------------------------------------------------------------------
read -rp "Start containers now? [Y/n]: " start_containers || true
if [[ ! "${start_containers:-}" =~ ^[Nn]$ ]]; then
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
