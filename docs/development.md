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
runs without a display.

## Trying the GUI by hand

The `gui` service runs the real window on a virtual display and serves it to a
browser with noVNC, so it works on a machine with no desktop session, over SSH:

```bash
docker compose up gui                             # Ctrl+C to stop
```

Then open <http://localhost:6080/vnc.html?autoconnect=1&resize=scale>. From
another machine, forward the port first: `ssh -L 6080:localhost:6080 <host>`.
The port is published on localhost only, and the VNC session has no password.

On first start it copies the test comics into `.gui/library`, a writable library
where removals can really be applied and restored; `.gui/home` keeps the app's
settings, cache and history between runs. Delete `.gui/` to start fresh. Closing
the window starts it again. `CC_GUI_SIZE` (default `1600x1000x24`) and
`CC_GUI_PORT` (default `6080`) in `.env` change the screen size and port. To check a *packaged* build (see
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
