#!/bin/bash
# Test runner script for bazel-aws-maven-proxy
# This script makes it easy to run different test configurations

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}Error: pytest is not installed${NC}"
    echo "Install test dependencies with: pip install -r tests/requirements.txt"
    exit 1
fi

# Default to running unit tests if no argument provided
TEST_TYPE="${1:-unit}"

echo -e "${YELLOW}Running ${TEST_TYPE} tests...${NC}\n"

case "$TEST_TYPE" in
    "unit")
        echo "Running fast unit tests..."
        pytest -m unit -v
        ;;
    "integration")
        echo "Running integration tests (requires Docker)..."
        pytest -m integration -v --no-cov
        ;;
    "all")
        echo "Running all tests with coverage..."
        pytest -v
        ;;
    "coverage")
        echo "Running tests and generating HTML coverage report..."
        pytest --cov-report=html
        echo -e "\n${GREEN}Coverage report generated at htmlcov/index.html${NC}"
        ;;
    "watch")
        if ! command -v ptw &> /dev/null; then
            echo -e "${RED}Error: pytest-watch is not installed${NC}"
            echo "Install it with: pip install pytest-watch"
            exit 1
        fi
        echo "Running tests in watch mode (will re-run on file changes)..."
        ptw -- -m unit
        ;;
    "quick")
        echo "Running quick smoke tests..."
        pytest -m unit --maxfail=1 -x
        ;;
    *)
        echo -e "${RED}Unknown test type: $TEST_TYPE${NC}"
        echo "Usage: $0 [unit|integration|all|coverage|watch|quick]"
        echo ""
        echo "Options:"
        echo "  unit        - Run unit tests (fast, default)"
        echo "  integration - Run integration tests (requires Docker)"
        echo "  all         - Run all tests with coverage"
        echo "  coverage    - Generate HTML coverage report"
        echo "  watch       - Run tests in watch mode"
        echo "  quick       - Run quick smoke tests (stop on first failure)"
        exit 1
        ;;
esac

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo -e "\n${GREEN}✓ All tests passed!${NC}"
else
    echo -e "\n${RED}✗ Some tests failed${NC}"
fi

exit $EXIT_CODE
