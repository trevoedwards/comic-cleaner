# Development

Back to the [README](../README.md).

All development runs in Docker; nothing is installed on the host. The `dev`
service has Python 3.12, the `dev` and `build` extras, 7-Zip, and the libraries
Qt needs to run offscreen. The repository is bind-mounted at `/workspace`, so
edits on the host are live in the container without a rebuild.

```bash
cp .env.example .env                              # once; point CC_TEST_COMICS at some comics
docker compose build                              # again after changing pyproject.toml (--no-cache)
docker compose run --rm dev python -m pytest      # core + offscreen GUI tests
docker compose run --rm dev python -m ruff check .
docker compose run --rm dev bash                  # a shell
```

The container runs as your host user (`CC_UID`/`CC_GID`, default 1000), so
caches, `dist/` and crash logs it writes stay yours.

`CC_TEST_COMICS` in `.env` is mounted **read-only** at `/comics`. The headless
commands work against it directly; `clean` needs `--dry-run`, or `--output`
pointing somewhere writable:

```bash
docker compose run --rm dev python -m comiccleaner scan /comics
docker compose run --rm dev python -m comiccleaner clean /comics --all --dry-run
```

GUI tests drive the real window through Qt's `offscreen` platform, so the suite
runs without a display. To check a *packaged* build (see
[Building a binary](building.md)):

```bash
docker compose run --rm dev ./dist/ComicCleaner --self-test /comics
```

It reports the archive tools found, whether the icon was bundled, which Pillow
codecs survived packaging, and what a real scan produced — catching the silent
packaging failures, like a missing JPEG codec that would make every scan quietly
find nothing.

`src/comiccleaner/core/` never imports Qt, so the whole scan → match → remove
pipeline works as a library; `gui/` is the PySide6 layer on top.

## When something goes wrong

Unhandled errors are written to a **`crashlog` folder beside wherever the app was
launched from**, and the app tells you the path. Each report has the traceback,
versions, platform, detected archive tools, and the last couple of hundred log
lines. Background scan and removal threads are covered too. If the launch folder
is read-only the report falls back to the app data directory rather than being
lost.
