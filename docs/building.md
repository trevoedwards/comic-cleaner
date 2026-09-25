# Building a binary

Back to the [README](../README.md).

```bash
python build.py --clean --smoke-test
```

Options: `--onedir`, `--console`, `--cli`, `--clean`, `--smoke-test`, and `--smoke-only`
(re-test whatever is already in `dist/` without rebuilding). It runs the same on
Windows, macOS and Linux, and uses the project `.venv` if there is one.

| Platform | Output | Icon |
|---|---|---|
| Windows | `dist/ComicCleaner.exe` | embedded `.ico` |
| macOS | `dist/ComicCleaner.app` | embedded `.icns` |
| Linux | `dist/ComicCleaner` | bundled PNG at runtime |
| Any, with `--cli` | `dist/comiccleaner-cli[.exe]` | embedded, as above |

`--cli` builds the console variant for the command line. Only Windows needs it
(CI builds it there), and its smoke test runs a real `scan --json` rather than
just launching it.

> [!NOTE]
> PyInstaller cannot cross-compile — each binary must be built on the OS it
> targets. CI builds all three on every push and uploads them as artifacts.

## Publishing a release

Set the same version in `pyproject.toml` and `src/comiccleaner/__init__.py`,
commit, then tag and push:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

CI builds every platform and, if the tag matches both version numbers, publishes
a GitHub release with `ComicCleaner-<version>-windows-x64.exe`,
`comiccleaner-cli-<version>-windows-x64.exe`,
`…-macos-arm64.zip`, `…-linux-x64.tar.gz` and a `SHA256SUMS.txt`.
