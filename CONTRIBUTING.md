# Contributing

Contributions welcome. This is a focused tool — keep changes simple and aligned with the core use case.

## Getting Started

```bash
# Clone and install dependencies
git clone https://github.com/your-org/bazel-aws-maven-proxy.git
cd bazel-aws-maven-proxy
pip install -r tests/requirements.txt

# Run tests
pytest
```

## Development

- Python 3.11+
- No external dependencies for watcher (stdlib only)
- S3 proxy uses Flask + boto3
- Podman preferred, Docker also supported

## Testing

All changes should include tests. Run the full suite before submitting:

```bash
pytest -v --no-cov
```

See [docs/testing.md](docs/testing.md) for test structure and coverage details.

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include tests for new behavior
- Update docs if user-facing behavior changes
- Ensure all 88 tests pass

## Code Style

- No strict linter enforced yet
- Follow existing patterns in the codebase
- Watcher uses only Python stdlib (no pip dependencies)

## Reporting Issues

Open a GitHub issue with:
- What you expected
- What happened
- Steps to reproduce
- OS and Python version
