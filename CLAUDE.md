# Comic Cleaner — project rules

## Docker-only (non-negotiable)

All development happens inside Docker. Do **not** install project dependencies on the host. No `pip install`, no host `.venv`, no host `pytest`/`ruff`.

```bash
docker compose build
docker compose run --rm dev bash
docker compose run --rm dev python -m pytest
docker compose run --rm dev python -m ruff check .
```

Real test comics are bind-mounted **read-only** at `/comics` from `CC_TEST_COMICS` in `.env` (see `.env.example`). On this machine that is `/home/prime/Temp/PageMaster-Test-Content`.

The host needs Docker. Rebuild after dependency changes: `docker compose build --no-cache`.

## State of the work

Where things stand and what comes next: [docs/HANDOFF.md](docs/HANDOFF.md). Read it at the start of a session.
