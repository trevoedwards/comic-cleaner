<div align="center">

<img src="docs/icon.png" width="112" alt="Comic Cleaner icon">

# Comic Cleaner

**Find and remove duplicate pages across your comic archives.**

Ads, scanlation credits and "read more at…" filler — gone, in bulk, across a whole series.

[![CI](https://github.com/trevoedwards/comic-cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/trevoedwards/comic-cleaner/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52?logo=qt&logoColor=white)](https://doc.qt.io/qtforpython/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#requirements)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

| Dark | Light |
|:---:|:---:|
| <img src="docs/screenshot-dark.png" alt="Comic Cleaner, dark theme"> | <img src="docs/screenshot-light.png" alt="Comic Cleaner, light theme"> |

Repackaged comics pick up junk — the same advert injected into all forty issues
of a run, a credits page stapled onto every chapter. The giveaway is repetition:
a page in *one* book is content, a page in *thirty* almost certainly is not.

Comic Cleaner hashes every page, groups the images that keep reappearing, ranks
them by how many books they affect, and strips out the ones you confirm — across
a whole library at once, without unpacking anything by hand.

## Requirements

- **Python 3.10+** (developed on 3.12), or just grab a prebuilt binary from the
  [Releases page](https://github.com/trevoedwards/comic-cleaner/releases/latest).
- **Optional:** [7-Zip](https://www.7-zip.org/), `unrar` or WinRAR — only for
  `.cbr` / `.cb7`. Found automatically; **Settings → Archive tools** shows what
  was detected. `.cbz` needs nothing. To use a specific 7-Zip, set
  `COMICCLEANER_7Z` to its path.

Everything else is three pip packages (PySide6, Pillow, numpy) in a
project-local `.venv`. Nothing lands on your system Python.

## Install and run

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # .venv\Scripts\python.exe on Windows
python -m comiccleaner "/path/to/comics"        # paths are optional
```

1. **Import** — drag archives or folders in, or use *Add Files* / *Add Folder*.
2. **Scan** — pages are decoded and hashed, then cached against each file's size
   and mtime, so re-scanning an unchanged library is instant.
3. **Review** — groups are ranked by how many books they affect. All copies are
   ticked for removal; untick any you want to keep. Double-click a page to see it
   full size.
4. **Apply** — a confirmation dialog spells out every change, with a dry run.
   The books that were rewritten are rescanned automatically afterwards.

> [!TIP]
> Changing a matching setting re-groups instantly — it does not re-scan.

The library and your review decisions are saved as you go, so closing the app
mid-review loses nothing: next launch reopens the same books and rescans them,
which is instant for any book the hash cache already knows.

### Reviewing from the keyboard

| Key | Does |
|---|---|
| <kbd>D</kbd> | Remove the ticked copies of this group, then go to the next undecided group |
| <kbd>K</kbd> | Keep this group, then go to the next undecided group |
| <kbd>I</kbd> | Ignore this page from now on (undo it from **Ignored Pages…**) |
| <kbd>Space</kbd> | Tick or untick the selected copy |
| <kbd>Enter</kbd> | Open the selected copy full size |
| <kbd>Delete</kbd> | In the library, remove the selected books from the list (not from disk) |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> / <kbd>Ctrl</kbd>+<kbd>R</kbd> | Add files / scan |

In the full-size preview, <kbd>←</kbd> <kbd>→</kbd> step through the copies,
<kbd>Space</kbd> ticks or unticks the one on screen, and for **similar** groups
<kbd>H</kbd> paints whatever differs from the reference image in red. That is
how you spot an advert whose issue number or date changes from book to book.
Similar groups list the copies furthest from the reference first, so any page
that single-linkage chained in shows up at the start, not buried.

## Command line

The same pipeline runs without a window, for scripts, schedulers and library
post-processing hooks. It never loads Qt, so it works on a headless server, and
it shares the GUI's hash cache and ignore list, so a book the GUI has already
scanned costs nothing to scan again.

```bash
comiccleaner scan  /path/to/comics                  # report what repeats; changes nothing
comiccleaner scan  /path/to/comics --json           # the same, for another program
comiccleaner clean /path/to/comics --all --dry-run  # what a clean would do
comiccleaner clean /path/to/comics --all --yes      # do it, unattended
comiccleaner clean /path/to/comics --group 8c193c   # one group, by an ID from scan
```

Matching options (`--threshold`, `--min-copies`, `--min-books`,
`--include-first-page`, `--skip-last-page`, `--include-blank`) take the GUI's
defaults, but not its saved settings, so a script means the same thing whatever
someone last picked in the Settings dialog. `--exact-only` keeps only
byte-identical groups even at a looser threshold, which is the safest thing to
automate. Run `comiccleaner clean --help` for everything else, including
`--output`, `--backup-dir`, `--no-backup` and `--delete-backups`.

`clean` asks before changing anything and refuses outright when nobody is there
to answer, so an unattended run needs `--yes`. It also skips any book that would
lose more than a quarter of its pages (`--max-fraction` sets the limit). Adverts
are a page or three, and a larger match usually means two copies of the same
issue are matching each other page for page. Ctrl+C stops after the current book.

| Exit code | Meaning |
|---|---|
| `0` | Done |
| `1` | Some books could not be read or cleaned (the rest were) |
| `2` | Bad options, or a refusal. Nothing was changed |
| `130` | Interrupted |

> [!NOTE]
> **Windows:** the regular `ComicCleaner.exe` is a windowed app, and a windowed
> app has no console, so it cannot report back to a terminal. Use
> `comiccleaner-cli.exe` from the release page instead. It is the same program
> built as a console app. **macOS and Linux:** the regular binary works from a
> terminal; on macOS it is inside the app, at
> `ComicCleaner.app/Contents/MacOS/ComicCleaner`.

## How matching works

| Signal | Catches | Shown as |
|---|---|---|
| SHA-256 of the stored bytes | Byte-identical copies | **identical** |
| 64-bit difference hash | The same image re-encoded, rescaled or recoloured | **similar** |

The perceptual hash compares each pixel to its right-hand neighbour, so it
survives the rescaling and recompression an advert picks up on its way through
three repackagings.

Past a few thousand pages, similarity search switches to **multi-index
hashing** — each hash is split into `threshold + 1` bands, and by the pigeonhole
principle any true match must share a band exactly, so only band buckets get
compared. It is an exact speed-up, not an approximation, and the tests assert it
matches brute force. 50,000 pages at threshold 6: ~1.3 s instead of ~102 s.

> [!NOTE]
> Clustering is single-linkage — if A matches B and B matches C, all three group
> together even when A and C are further apart than the threshold. Hence the
> default of 0, and why loose values deserve a look before you bulk delete.

**Ignore** remembers every hash in the group, and hides any page within the
current threshold of one of them. An ignore made at similarity 6 therefore
still holds at 0, and one made at 0 also hides re-encoded copies once you
loosen the setting. **Ignored Pages…** on the toolbar lists everything
ignored, with a thumbnail, and brings any of it back.

## Settings

| Setting | Default | Notes |
|---|---|---|
| **Theme** | Follow system | Light, dark or the OS setting, previewed live. |
| **Reopen the last library** | on | Brings back the books and review decisions from last time. Turning it off also deletes the saved session. |
| **Similarity** | 0 | Hamming distance. `0` = identical only, `2`–`6` catches re-encodes, above ~`10` expect false matches. |
| **Minimum copies** | 2 | Occurrences before a group is shown. |
| **Across at least (books)** | 2 | Ads repeat across books. `1` also catches a page repeated within one book. |
| **Never match first / last page** | on / off | Protects covers, which legitimately repeat across a series. |
| **Include blank pages** | off | Blank pages look alike to *any* perceptual hash. |
| **Delete backups after a run** | off | Sweeps `.bak` files once every archive in the run has succeeded. |
| **Write cleaned copies to** | *(in place)* | Point at a folder to leave originals untouched. Books keep their folder layout underneath it, and an existing file is never overwritten. |

## Safety

Removal is the only destructive operation, and it is deliberately paranoid:

- The rebuilt archive is written to a temp file and **verified** — opens cleanly,
  correct page count — *before* the original is touched.
- The original is then moved aside atomically and the new file swapped in. On any
  failure the original is put back.
- It refuses to empty an archive. Every page marked for removal is re-checked
  against its hash from the scan, so a book that was re-packed or reordered since
  is left alone rather than losing the wrong page.
- A file locked by another program is skipped with an explanation, not mangled.
- `ComicInfo.xml` is kept in step: `PageCount` is updated, and each `Page` entry's
  `Image` index is shifted so it still points at the same image.

Backups are kept by default. Clear them from the removal-finished dialog, from
**Clean Up Backups…** on the toolbar, automatically via Settings, or keep them
out of the way entirely with a backup folder.

> [!WARNING]
> `.cbr` and `.cb7` cannot be written to. Removing pages from one produces a
> **`.cbz` beside it**, with the original kept as the backup.

Back up anything irreplaceable before a large run, and try the dry run first.

## Building a binary

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

### Publishing a release

Set the same version in `pyproject.toml` and `src/comiccleaner/__init__.py`,
commit, then tag and push:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

CI builds every platform and, if the tag matches both version numbers, publishes
a GitHub release with `ComicCleaner-<version>-windows-x64.exe`,
`comiccleaner-cli-<version>-windows-x64.exe`,
`…-macos-arm64.zip`, `…-linux-x64.tar.gz` and a `SHA256SUMS.txt`.

## When something goes wrong

Unhandled errors are written to a **`crashlog` folder beside wherever the app was
launched from**, and the app tells you the path. Each report has the traceback,
versions, platform, detected archive tools, and the last couple of hundred log
lines. Background scan and removal threads are covered too. If the launch folder
is read-only the report falls back to the app data directory rather than being
lost.

## Development

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

## Credits

Developed by **Trevor Edwards** — [Playback Software](https://playbacksoftware.com/).

Licensed under the [MIT License](LICENSE).
