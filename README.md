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
   A large folder is walked in the background and can be stopped part way; the
   status line says what was skipped (PDFs, plain `.rar`/`.7z`, and anything
   matching your exclude patterns).
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
| <kbd>S</kbd> | Defer: decide later, and go to the next undecided group |
| <kbd>W</kbd> | Go to the next group with a warning |
| <kbd>Space</kbd> | Tick or untick the selected copy |
| <kbd>Enter</kbd> | Open the selected copy full size |
| <kbd>Ctrl</kbd>+<kbd>Z</kbd> | Undo the last review decision (up to 50; not across a real removal) |
| <kbd>Delete</kbd> | In the library, remove the selected books from the list (not from disk) |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> / <kbd>Ctrl</kbd>+<kbd>R</kbd> | Add files / scan |

The letter keys only work while the group list, the copies or the decision
buttons have focus, never while you are typing in a box. Shift- or Ctrl-click
several groups and <kbd>D</kbd>, <kbd>K</kbd>, <kbd>I</kbd>, <kbd>S</kbd>,
**Mark all**, **Clear all** and **Mark safe** act on all of them. Right-click a
book, group or copy for more (reveal in folder, copy a path or group id, open
the archive in your reader).

In the full-size preview, <kbd>←</kbd> <kbd>→</kbd> step through the copies,
<kbd>Space</kbd> ticks or unticks the one on screen, and for **similar** groups
<kbd>H</kbd> paints whatever differs from the reference image in red. That is
how you spot an advert whose issue number or date changes from book to book.
Similar groups list the copies furthest from the reference first, so any page
that single-linkage chained in shows up at the start, not buried.
**Compare with this** (<kbd>P</kbd>) makes the copy on screen the reference,
in any group. The wheel zooms, dragging pans, **1:1** (<kbd>1</kbd>) shows real
pixels and **Fit** (<kbd>F</kbd>) goes back to the whole page.

### More ways to review and apply

- **Apply to Selected Books…** applies the marked pages to the books selected in
  the library only. **Review → Remove Known Junk Only…** is the GUI's
  `clean --known`: it marks the known-junk groups and applies just those.
- **Only Near the Edges** leaves unticked every copy further into its book than
  the edge window (Settings, default 3 pages), like `clean --edges`.
- The confirmation lists each book's share of pages removed and the first page
  names, flags any first or last page, and **Save Plan…** writes the plan to JSON
  and CSV without changing anything.
- Books that look like the **same issue twice** get a banner; nothing is marked
  because of it.
- **File → Export / Import Review Pack…** moves your matching settings, known
  junk and ignore list to another machine. Settings are applied only after you
  confirm the changes they make.
- The first time a run would delete pages matched only by an **imported**
  known-junk list, you are asked once more.
- **History…** can pin a run (its backups survive **Clean Up Backups…**) or
  delete one run's backups, and shows the first page before a restore.

PDF is not supported, and neither `.cbr` nor `.cb7` can be written: cleaning one
writes a `.cbz` beside it and keeps the original as the backup. See
[Reviewing groups](docs/review.md) and [Settings](docs/settings.md) for the rest.

### After a library import (Komga, Kavita, ComicRack)

There is no server integration. Instead, run the command line from whatever
post-import hook your server or downloader offers:

```bash
comiccleaner clean /library --known --yes
```

This only removes pages you have already removed once (or imported as known
junk), so it never acts on a match nobody has looked at. It will not find new
adverts; review those in the app.

## Documentation

- [Reviewing groups](docs/review.md) — narrowing the list, known junk
- [Command line](docs/cli.md)
- [How matching works](docs/matching.md)
- [Settings](docs/settings.md)
- [Safety](docs/safety.md)
- [Building a binary](docs/building.md) — including the optional signing and PyPI workflows
- [Development](docs/development.md)

## Credits

Developed by **Trevor Edwards** — [Playback Software](https://playbacksoftware.com/).

Licensed under the [MIT License](LICENSE).
