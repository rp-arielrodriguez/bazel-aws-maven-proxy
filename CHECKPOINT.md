# Checkpoint - 2026-03-05

## Session Topics
- Container mount issues (`:ro` vs `:rw` on `~/.aws/config`)
- ProfileNotFound handling in monitor
- AWS_CONFIG_FILE env var fixes for Docker/Podman differences

## Current State
- **Tests**: 378 passing (22 s3proxy + 211 watcher + 52 monitor + 93 setup)
- **Commits pushed to main**:
  1. `eae08ca`: proactive refresh backoff + sso/cache `:ro` removal
  2. `27c5186`: ProfileNotFound handling (no crash-loop)
  3. `0d7ec24`: AWS_CONFIG_FILE env var for containers
  4. `c0481c8`: config mounts changed to `:rw` (user)

## Active Issue
- `bazel-cache` machine (Docker): testing if `:ro` works on config mounts
- Current: `:rw` on config, `:no flag` on sso/cache

## Next Steps (on bazel-cache machine)
1. Test `~/.aws/config:ro` - revert if works
2. Rebuild: `docker compose up -d --build`
3. Verify: monitor logs show "Credentials valid"

## Working Tree
Clean - nothing uncommitted.

## Unresolved
- Why Docker needs `:rw` when Podman worked with `:ro`
