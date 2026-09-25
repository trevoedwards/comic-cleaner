# Development

Back to the [README](../README.md).

```bash
python -m pytest          # core + offscreen GUI tests
python -m ruff check .
```

GUI tests drive the real window through Qt's `offscreen` platform, so the suite
runs without a display. To check a *packaged* build:

```bash
./dist/ComicCleaner --self-test "/path/to/comics"
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
