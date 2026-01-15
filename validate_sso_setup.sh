#!/bin/bash
# validate_sso_setup.sh - Validate AWS SSO configuration for auto-refresh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
AWS_PROFILE=${1:-${AWS_PROFILE:-default}}
AWS_CONFIG_FILE="${HOME}/.aws/config"

echo "===== AWS SSO Setup Validator ====="
echo "Validating profile: ${AWS_PROFILE}"
echo ""

# Track validation status
ERRORS=0
WARNINGS=0

# Check 1: AWS CLI v2 installed
echo -n "Checking AWS CLI v2... "
if ! command -v aws &> /dev/null; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: AWS CLI not found in PATH"
    echo "  Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    ERRORS=$((ERRORS + 1))
else
    AWS_VERSION=$(aws --version 2>&1 | cut -d' ' -f1 | cut -d'/' -f2)
    AWS_MAJOR_VERSION=$(echo ${AWS_VERSION} | cut -d'.' -f1)
    if [ "${AWS_MAJOR_VERSION}" -lt 2 ]; then
        echo -e "${RED}FAIL${NC}"
        echo "  Error: AWS CLI v1 detected (${AWS_VERSION}). v2 required for SSO session support"
        echo "  Upgrade: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
        ERRORS=$((ERRORS + 1))
    else
        echo -e "${GREEN}OK${NC} (${AWS_VERSION})"
    fi
fi

# Check 2: AWS config file exists
echo -n "Checking AWS config file... "
if [ ! -f "${AWS_CONFIG_FILE}" ]; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: ${AWS_CONFIG_FILE} not found"
    echo "  Create config with: aws configure sso"
    ERRORS=$((ERRORS + 1))
    exit 1
else
    echo -e "${GREEN}OK${NC}"
fi

# Check 3: Profile exists in config
echo -n "Checking profile [${AWS_PROFILE}]... "
if ! grep -q "^\[profile ${AWS_PROFILE}\]" "${AWS_CONFIG_FILE}" && ! grep -q "^\[${AWS_PROFILE}\]" "${AWS_CONFIG_FILE}"; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: Profile '${AWS_PROFILE}' not found in ${AWS_CONFIG_FILE}"
    echo "  Create with: aws configure sso --profile ${AWS_PROFILE}"
    ERRORS=$((ERRORS + 1))
    exit 2
else
    echo -e "${GREEN}OK${NC}"
fi

# Extract profile section
PROFILE_SECTION=$(awk "/^\[profile ${AWS_PROFILE}\]/,/^\[/" "${AWS_CONFIG_FILE}" | grep -v "^\[" | grep "=" || true)
if [ -z "${PROFILE_SECTION}" ]; then
    PROFILE_SECTION=$(awk "/^\[${AWS_PROFILE}\]/,/^\[/" "${AWS_CONFIG_FILE}" | grep -v "^\[" | grep "=" || true)
fi

# Check 4: Profile has sso_session field
echo -n "Checking sso_session field... "
SSO_SESSION=$(echo "${PROFILE_SECTION}" | grep "sso_session" | cut -d'=' -f2 | tr -d ' ' || true)
if [ -z "${SSO_SESSION}" ]; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: Profile missing 'sso_session' field"
    echo "  Add to ~/.aws/config:"
    echo "    [profile ${AWS_PROFILE}]"
    echo "    sso_session = my-sso"
    echo "    sso_account_id = YOUR_ACCOUNT_ID"
    echo "    sso_role_name = YOUR_ROLE_NAME"
    echo ""
    echo "  Docs: https://docs.aws.amazon.com/cli/latest/userguide/sso-configure-profile-token.html"
    ERRORS=$((ERRORS + 1))
    exit 2
else
    echo -e "${GREEN}OK${NC} (${SSO_SESSION})"
fi

# Check 5: SSO session section exists
echo -n "Checking sso-session [${SSO_SESSION}]... "
if ! grep -q "^\[sso-session ${SSO_SESSION}\]" "${AWS_CONFIG_FILE}"; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: sso-session '${SSO_SESSION}' not found in ${AWS_CONFIG_FILE}"
    echo "  Add to ~/.aws/config:"
    echo "    [sso-session ${SSO_SESSION}]"
    echo "    sso_region = YOUR_SSO_REGION"
    echo "    sso_start_url = https://YOUR_SSO_PORTAL.awsapps.com/start"
    echo "    sso_registration_scopes = sso:account:access"
    echo ""
    echo "  Docs: https://docs.aws.amazon.com/cli/latest/userguide/sso-configure-profile-token.html"
    ERRORS=$((ERRORS + 1))
    exit 2
else
    echo -e "${GREEN}OK${NC}"
fi

# Extract sso-session section
SSO_SESSION_SECTION=$(awk "/^\[sso-session ${SSO_SESSION}\]/,/^\[/" "${AWS_CONFIG_FILE}" | grep -v "^\[" | grep "=" || true)

# Check 6: sso_region field
echo -n "Checking sso_region... "
SSO_REGION=$(echo "${SSO_SESSION_SECTION}" | grep "sso_region" | cut -d'=' -f2 | tr -d ' ' || true)
if [ -z "${SSO_REGION}" ]; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: sso-session missing 'sso_region' field"
    echo "  Add to [sso-session ${SSO_SESSION}] section: sso_region = YOUR_REGION"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}OK${NC} (${SSO_REGION})"
fi

# Check 7: sso_start_url field
echo -n "Checking sso_start_url... "
SSO_START_URL=$(echo "${SSO_SESSION_SECTION}" | grep "sso_start_url" | cut -d'=' -f2 | tr -d ' ' || true)
if [ -z "${SSO_START_URL}" ]; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: sso-session missing 'sso_start_url' field"
    echo "  Add to [sso-session ${SSO_SESSION}] section: sso_start_url = https://YOUR_SSO_PORTAL.awsapps.com/start"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}OK${NC} (${SSO_START_URL})"
fi

# Check 8: sso_registration_scopes field
echo -n "Checking sso_registration_scopes... "
SSO_SCOPES=$(echo "${SSO_SESSION_SECTION}" | grep "sso_registration_scopes" | cut -d'=' -f2 | tr -d ' ' || true)
if [ -z "${SSO_SCOPES}" ]; then
    echo -e "${YELLOW}WARNING${NC}"
    echo "  Missing 'sso_registration_scopes' field (will use default)"
    echo "  Recommended: Add to [sso-session ${SSO_SESSION}] section: sso_registration_scopes = sso:account:access"
    WARNINGS=$((WARNINGS + 1))
elif [[ ! "${SSO_SCOPES}" =~ "sso:account:access" ]]; then
    echo -e "${YELLOW}WARNING${NC}"
    echo "  sso_registration_scopes doesn't include 'sso:account:access' (${SSO_SCOPES})"
    echo "  Auto-refresh may not work. Recommended: sso_registration_scopes = sso:account:access"
    WARNINGS=$((WARNINGS + 1))
else
    echo -e "${GREEN}OK${NC} (${SSO_SCOPES})"
fi

# Check 9: SSO account and role in profile
echo -n "Checking sso_account_id... "
SSO_ACCOUNT=$(echo "${PROFILE_SECTION}" | grep "sso_account_id" | cut -d'=' -f2 | tr -d ' ' || true)
if [ -z "${SSO_ACCOUNT}" ]; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: Profile missing 'sso_account_id' field"
    echo "  Add to [profile ${AWS_PROFILE}] section: sso_account_id = YOUR_ACCOUNT_ID"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}OK${NC} (${SSO_ACCOUNT})"
fi

echo -n "Checking sso_role_name... "
SSO_ROLE=$(echo "${PROFILE_SECTION}" | grep "sso_role_name" | cut -d'=' -f2 | tr -d ' ' || true)
if [ -z "${SSO_ROLE}" ]; then
    echo -e "${RED}FAIL${NC}"
    echo "  Error: Profile missing 'sso_role_name' field"
    echo "  Add to [profile ${AWS_PROFILE}] section: sso_role_name = YOUR_ROLE_NAME"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}OK${NC} (${SSO_ROLE})"
fi

# Summary
echo ""
echo "===== Validation Summary ====="
if [ ${ERRORS} -eq 0 ] && [ ${WARNINGS} -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed!${NC}"
    echo ""
    echo "Your AWS SSO configuration is ready for auto-refresh."
    echo "Next steps:"
    echo "  1. Run: aws sso login --profile ${AWS_PROFILE}"
    echo "  2. Start services: docker-compose up -d"
    echo "  3. Check logs: docker-compose logs credential-renewer"
    exit 0
elif [ ${ERRORS} -eq 0 ]; then
    echo -e "${YELLOW}✓ Configuration valid with ${WARNINGS} warning(s)${NC}"
    echo ""
    echo "Your configuration will work but may not be optimal."
    echo "Review warnings above for recommendations."
    exit 0
else
    echo -e "${RED}✗ Configuration invalid: ${ERRORS} error(s), ${WARNINGS} warning(s)${NC}"
    echo ""
    echo "Fix errors above before using auto-refresh feature."
    echo "See: examples/aws_config_example for reference configuration"
    exit 2
fi
