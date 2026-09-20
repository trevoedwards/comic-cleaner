# Comic Duplicate Page Remover

Finds pages that repeat across your comic archives — injected ads, scanlation
credits, "read more at..." filler — and strips them out, the way Komga's
duplicate-pages feature does, but as a standalone desktop tool.

Import archives the way you would in ComicTagger (drag and drop, or Add
Files/Folder), scan, review what it found, and remove.

## What it does

- Reads `.cbz` / `.zip` in-process, and `.cbr` / `.rar` / `.cb7` / `.7z` via
  7-Zip or UnRAR if either is installed.
- Hashes every page twice: a SHA-256 of the stored bytes (exact matches) and a
  64-bit perceptual difference hash (the same ad re-encoded, rescaled or
  recompressed).
- Groups matching pages and ranks them by how many books they appear in, which
  is the strongest signal that a page is an advert rather than story content.
- Rewrites the archives without the pages you marked, updating `ComicInfo.xml`
  so page counts stay correct.

## Requirements

- Python 3.10+ (tested on 3.12)
- Optional, for `.cbr`/`.cb7`: [7-Zip](https://www.7-zip.org/) or WinRAR.
  The app finds them on `PATH` or in the usual install folders, and Settings
  shows which backend it picked up.

Everything else is three pip packages (PySide6, Pillow, numpy) installed into a
project-local `.venv`, so nothing lands on your system Python.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Running

```powershell
.\.venv\Scripts\python.exe -m comicdedupe
```

You can also pass paths to import on startup:

```powershell
.\.venv\Scripts\python.exe -m comicdedupe "D:\Comics\Some Series"
```

## Using it

1. **Import** — drag archives or folders onto the window, or use Add Files /
   Add Folder. Folders are searched recursively.
2. **Scan** — every page is decoded and hashed. Results are cached against each
   file's size and modification time, so re-scanning an unchanged library is
   instant.
3. **Review** — the middle column lists duplicate groups, sorted by how many
   books they affect. Select one to see every copy. All copies are ticked for
   removal by default; untick any you want to keep.
4. **Decide** — *Remove this page everywhere*, *Keep*, or *Ignore forever*
   (which hides the group from all future scans; ignores persist in the cache).
5. **Apply** — a confirmation dialog lists exactly what will change. There is a
   dry-run checkbox that reports without touching anything.

### Settings worth knowing

| Setting | Default | Notes |
|---|---|---|
| Similarity | 0 | Hamming distance on the perceptual hash. 0 = identical images only. 2–6 catches re-encodes. Above ~10 expect false matches. |
| Minimum copies | 2 | How many occurrences before a group is shown. |
| Across at least (books) | 2 | Ads repeat across books; raise this to cut noise, set to 1 to catch a page repeated inside a single book. |
| Never match the first page | on | Protects covers, which legitimately repeat across a series. |
| Include blank pages | off | Blank and solid-colour pages look alike to *any* perceptual hash, so they are hidden unless you ask for them. |
| Theme | Follow system | Light, Dark, or follow the OS setting. Previews live in the Settings dialog. |
| Delete backups after a run | off | Sweeps the `.bak` files once every archive in the run has succeeded. |

Changing any matching setting re-groups instantly — it does not re-scan.

## Safety

Removal is the only destructive operation, and it is deliberately cautious:

- The rebuilt archive is written to a temp file and **verified** (opens cleanly,
  has the expected page count) before the original is touched.
- The original is then moved to `<name>.cbz.bak` (or a backup folder you pick)
  and the new file swapped in. If anything fails, the original is put back.
- It refuses to remove every page from an archive.
- It refuses to act on a plan that no longer matches the file on disk, so an
  archive edited since the scan is skipped rather than mangled.
- If a file is open in another program, Windows blocks the swap; the app says
  so and leaves that archive alone.

**`.cbr` and `.cb7` cannot be written to.** When you remove pages from one, the
result is written as a `.cbz` next to it and the original is kept as the backup.

### Getting rid of the .bak files

Backups are kept by default, which means a run leaves a `.bak` next to every
archive it touched. Three ways to deal with them:

- The **removal-finished dialog** offers a one-click *Delete N backup(s)* button.
- **Clean Up Backups...** on the toolbar sweeps backups left by earlier runs.
- **Settings > Delete the backups once the whole run has succeeded** does it
  automatically. Backups still protect each rewrite while it happens; they are
  only removed at the end, and only if *nothing* in the run failed.

You can also point Settings at a **backup folder** to keep them out of your
comics directory entirely.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest        # core + offscreen GUI tests
.\.venv\Scripts\python.exe -m ruff check .
```

The GUI tests drive the real window with Qt's `offscreen` platform, so they run
without a display.

To check a packaged build without a display:

```powershell
.\dist\comicdedupe.exe --self-test "D:\Comics\Some Series"
```

It reports the archive tools it found, the Pillow codecs available, and what a
scan actually produced. Windowed builds have no console, so it writes
`comicdedupe-selftest.txt` next to wherever you ran it.

### Building a portable .exe

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\build.ps1
```

This produces `dist\comicdedupe.exe`, a single self-contained file. PyInstaller
cannot cross-compile, so a Windows build has to happen on Windows.

## Layout

```
src/comicdedupe/
  assets/        # app icon, bundled into the build
  resources.py   # asset lookup that also works inside a PyInstaller bundle
  core/          # no Qt imports - fully testable headless
    archive.py     read cbz/cbr/cb7, write cbz
    extern.py      locate and drive 7-Zip / UnRAR
    hashing.py     content SHA + perceptual dhash
    scanner.py     walk archives, hash pages, in parallel
    cache.py       SQLite hash cache and ignore list
    grouping.py    cluster pages into duplicate groups
    remover.py     verified, backed-up archive rewriting
    comicinfo.py   keep ComicInfo.xml consistent
  gui/           # PySide6 layer
    main_window.py three aligned columns: library, groups, page grid
    theme.py       light / dark / follow-system palettes
    about.py       About dialog
    settings.py    preferences and the settings dialog
```

`core` never imports Qt, so the whole matching and removal pipeline can be used
as a library or driven from a script.
