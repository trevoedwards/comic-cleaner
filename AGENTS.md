# Agent notes

Claude Code: follow **`CLAUDE.md`**. Cursor: follow `.cursor/rules/` plus this file.

## Docker-only

All development happens inside Docker. Do not install project dependencies on the host. Use `docker compose run --rm dev ...` for pytest, ruff, and shells.

Real test comics are at `/comics` in the container (`CC_TEST_COMICS=/home/prime/Temp/PageMaster-Test-Content`).

## State of the work

See `docs/HANDOFF.md`.
