# Building a binary

Back to the [README](../README.md).

```bash
docker compose run --rm dev python build.py --clean --smoke-test
```

Options: `--onedir`, `--console`, `--cli`, `--clean`, `--smoke-test`, and `--smoke-only`
(re-test whatever is already in `dist/` without rebuilding). The Linux binary
builds in the Docker `dev` service like the above. `build.py` itself runs the
same on Windows and macOS, which is how CI builds those.

| Platform | Output | Icon |
|---|---|---|
| Windows | `dist/ComicCleaner.exe` | embedded `.ico` |
| macOS | `dist/ComicCleaner.app` | embedded `.icns` |
| Linux | `dist/ComicCleaner` | bundled PNG at runtime |
| Linux, with `--appimage` | `dist/ComicCleaner-x86_64.AppImage` | `.desktop` entry and 256 px PNG |
| Any, with `--cli` | `dist/comiccleaner-cli[.exe]` | embedded, as above |

`--appimage` (Linux only) makes a one-folder build under `build/appimage/`, so
it does not collide with the one-file `dist/ComicCleaner`, puts it in an AppDir
with a `.desktop` entry and icon, and packs it with a pinned `appimagetool` and
AppImage runtime, both checked against their SHA-256 and cached in
`build/tools/`. `--appimage --smoke-test` launches the result:

```bash
docker compose run --rm dev python build.py --appimage --smoke-test
```

There is no FUSE in Docker or on CI, so there the AppImage runs with
`APPIMAGE_EXTRACT_AND_RUN=1`, which unpacks it instead of mounting it. The
`dev` image carries the xcb-util libraries Qt's xcb plugin needs, as CI's Linux
build does, so PyInstaller bundles them. The glibc floor is the build
machine's: CI builds on Ubuntu 22.04 (2.35); a build in the `dev` image
(Debian 12) needs 2.36.

`--cli` builds the console variant for the command line. Only Windows needs it
(CI builds it there), and its smoke test runs a real `scan --json` rather than
just launching it.

> [!NOTE]
> PyInstaller cannot cross-compile — each binary must be built on the OS it
> targets. CI builds all three on every push and uploads them as artifacts.

## Publishing a release

Set the same version in `pyproject.toml` and `src/comiccleaner/__init__.py`,
give that version's section in [CHANGELOG.md](../CHANGELOG.md) its date in place
of "(unreleased)", commit, then tag and push:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

CI builds every platform and, if the tag matches both version numbers, publishes
a GitHub release. Its notes are that CHANGELOG section, with GitHub's generated
list of changes after it; a tag whose section is missing or still unreleased
fails within a minute, before anything is built. `python build.py
--release-notes 0.2.3` shows what would be published.

The release carries `ComicCleaner-<version>-windows-x64.exe`,
`comiccleaner-cli-<version>-windows-x64.exe`, `…-macos-arm64.zip`,
`…-linux-x64.tar.gz`, `ComicCleaner-<version>-x86_64.AppImage` and a
`SHA256SUMS.txt`.

## Optional: signing and PyPI

Two more workflows run only when started by hand from the Actions tab. Neither
runs on a tag, and neither touches the release above.

- **Sign and notarize (macOS)** (`sign-macos.yml`) builds the app, signs it with
  a Developer ID certificate, notarizes and staples it, and keeps the result as a
  workflow artifact. It needs the secrets `MACOS_CERTIFICATE` (base64 `.p12`),
  `MACOS_CERTIFICATE_PASSWORD`, `MACOS_SIGNING_IDENTITY`, `APPLE_ID`,
  `APPLE_TEAM_ID` and `APPLE_APP_PASSWORD`. If any is missing it logs that
  signing was skipped and succeeds.
- **PyPI package** (`pypi.yml`) builds the sdist and wheel, checks them with
  `twine check`, and keeps them as an artifact. It uploads to PyPI only when the
  `PYPI_TOKEN` secret is set; otherwise it logs that the upload was skipped.
