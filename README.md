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
   A folder brings in its `.cbz`, `.zip`, `.cbr` and `.cb7` files. Plain `.rar`
   and `.7z` files are skipped there, since they are usually not comics; add one
   directly (or name it on the command line) and it is imported like any other.
2. **Scan** — pages are decoded and hashed, then cached against each file's size
   and mtime, so re-scanning an unchanged library is instant. The cache follows a
   library that has been moved, too.
3. **Review** — groups are ranked by how many books they affect, then by whether
   they sit where junk sits (the first or last few pages). All copies are ticked
   for removal; untick any you want to keep. Double-click a page to see it full
   size. **Mark safe** marks only what needs no second look.
4. **Apply** — a confirmation dialog spells out every change, with a dry run.
   The books that were rewritten are rescanned automatically afterwards, and the
   run can be undone from **History…** while its backups exist.

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
| <kbd>I</kbd> | Ignore this page from now on (undo it from **Remembered Pages…**) |
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

## Documentation

- [Reviewing groups](docs/review.md) — narrowing the list, known junk
- [Command line](docs/cli.md)
- [How matching works](docs/matching.md)
- [Settings](docs/settings.md)
- [Safety](docs/safety.md)
- [Building a binary](docs/building.md)
- [Development](docs/development.md)

## Credits

Developed by **Trevor Edwards** — [Playback Software](https://playbacksoftware.com/).

Licensed under the [MIT License](LICENSE).
