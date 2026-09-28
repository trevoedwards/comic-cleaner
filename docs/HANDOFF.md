# Comic Cleaner handoff: 2026-09-28

Where the work stands and what comes next. [CLAUDE.md](../CLAUDE.md) holds the
rules (Docker-only development); [Development](development.md) has the commands.
This file covers the state of the work.

## Where things are

**0.2.3 is released** (tag `v0.2.3`, 2026-09-28): a GitHub release with Windows,
macOS and Linux binaries, its notes taken from [CHANGELOG.md](../CHANGELOG.md).
CI was green on every job, with no warnings, and the released Linux binary
passed its self-test on Debian 12 with the official 7-Zip. **Nothing is on
`master` beyond it.** The owner decides when the next release happens: do not
bump the version or push a tag until they ask.

Releases so far:

- **0.2.1** (2026-09-27): the library selection survives folding a heading,
  confirmation lines show file names only, and the scan's time left goes by
  bytes, not books.
- **0.2.2** (2026-09-27): the Linux binary is built on Ubuntu 22.04 (glibc 2.35;
  the 0.2.1 one needed 2.38 and would not start on Debian 12). Books rewritten
  by Apply in the GUI are rescanned again (the thread exited while the result
  box was open, so they never were). The window fits a laptop screen (its
  minimum was about 1500 px). On Linux's Fusion style, dark-theme check boxes
  and placeholder text are visible. It asked for `unrar` where 7-Zip lacked
  RAR support; 0.2.3 replaced that with the official 7-Zip.
- **0.2.3** (2026-09-28): see [CHANGELOG.md](../CHANGELOG.md) for the user-facing
  notes; what is behind them is below.

Development moved to a Linux machine and into Docker on 2026-09-27. `dev` runs
the tests; `gui` serves the real window to a browser over noVNC, and has
`xdotool` for driving it from a script (see [Development](development.md)).

### What went into 0.2.3

- **Windows fixes** (`1b11dca`, found by the Windows check below): the main
  window and the preview open no larger than the screen (they were fixed at
  1400x860 and 1100x820, and on 1280x800 the preview's Close button was under
  the taskbar), and a selected row takes the selection's text colour under the
  Windows styles (it was black on navy in high contrast).
- **The official 7-Zip everywhere** (`d1e50a2`). The app recommends it on every
  platform (`7zz` from 7-zip.org on Linux and macOS): Debian's, Ubuntu's,
  Fedora's and Homebrew's 7-Zip are built without the RAR codec, whose licence
  forbids using it to re-create RAR compression, so they read `.cb7` but not
  `.cbr`. With several 7-Zips installed, the first that reads RAR is used. The
  Settings "UnRAR" row lists only UnRAR itself. The Docker image and CI install
  the official 26.03, pinned by digest: bump `SEVENZIP_VERSION` and the
  digests together, in the Dockerfile and `ci.yml`. UnRAR and bsdtar are still
  used when present.
- **Leave .cbr and .cb7 books unchanged** (`7c96b3d`; Settings → Removing,
  `--leave-cbr`): those books are scanned and reviewed but never rewritten or
  converted; a zip named `.cbr` counts too. Apply and `clean` list them, plan
  files give the reason, and `--json` has `left_unchanged`. The README gained
  "Why CBZ?": RAR cannot be written by open tools, Komga converts CBR to CBZ
  before removing pages too, and `--output` or the new setting serve anyone
  keeping originals as they are.
- **Linked zoom in the preview** (`b0a98cd`): while comparing, both sides zoom
  and pan together, relative to fitting, so a rescaled copy shows the same
  part of the page; they keep their place when stepping, 1:1 gives the copy
  real pixels with the reference following, and both captions are two lines
  high so the images start level.
- **Layout** (`9054a49`, `24f2c8b`): at the window's minimum width the library
  column keeps 30 characters (about 180 px) instead of shrinking to about 75;
  the minimum is 1100 px on Linux and 1078 px on Windows, inside 1280. Group
  rows line up: every thumbnail gets the list's full 72 px box.
- **Tests** (`4625d39`, `7c96b3d`): `tests/test_rar_archives.py` reads five of
  libarchive's RAR4 and RAR5 archives (BSD-2-Clause, notice kept in
  `tests/data/rar/`) through every RAR reader installed; CI runs them with
  7-Zip on Linux, and 7-Zip and bsdtar on macOS and Windows.
  `tests/test_real_comics.py` scans and cleans the real Green Lantern Corps
  `.cbr` books from `/comics`; they are copyrighted, so it skips wherever they
  are not mounted, CI included.
- **Type checking** (`a7b68c0`, `0f35309`): the whole package passes mypy, and
  CI fails on any mypy error. Run `docker compose run --rm dev python -m mypy`
  along with ruff and pytest; skipping it locally is how one error reached CI
  on 2026-09-27.
- **Release notes and CI** (`406aec3`, `179f576`, `0b84768`; see "Releasing"
  and "CI" below).

### What was verified, and how

- **Tests, ruff and mypy** in the `dev` container, and CI on Ubuntu (Python
  3.10 and 3.12), macOS and Windows, for every commit.
- **The command line on the real test comics** (`/comics`, below), on a
  writable copy: scan, dry run with a plan file, `--output`, clean in place
  with backups and quarantine, rescan (nothing left), `history restore`
  (byte-identical to the originals, `.cbr` back, converted `.cbz` gone), and
  `clean --known`. All 17 pages removed were real junk: two DC house adverts,
  a scan group's logo, a watermark.
- **The GUI by real input**, in the `gui` container with `xdotool` and
  screenshots: scan, D/K/S and Ctrl+Z, preview, grouping and folding, context
  menus, Apply, History restore (byte-identical), Settings and every theme;
  and each 0.2.3 change in the real window. Four bugs found this way were
  fixed in 0.2.2.
- **On a Windows 11 desktop**, twice (see "The Windows 11 check").
- **The released Linux binaries on Debian 12** (glibc 2.36), 0.2.2 and 0.2.3:
  each self-test read all 13 archives, `.cbr` included.

## Next steps: 0.3, in order

The milestone is getting Comic Cleaner to users, after checking the platform
most of them will use. There are no users yet: no issues or stars, and the
downloads are our own. Features beyond this wait for their feedback.

1. **Done: the Windows 11 check** (2026-09-27).
2. **Publish to PyPI.** The name `comiccleaner` is not taken (checked
   2026-09-27). The owner creates the PyPI project and an API token and adds it
   as the `PYPI_TOKEN` secret; then run **PyPI package** (`pypi.yml`) from the
   Actions tab. Check it in a clean container:
   `docker run --rm python:3.12 pip install comiccleaner && comiccleaner --help`.
   Then give the README a `pip install comiccleaner` line.
3. **Announce it: on hold.** Drafts exist (a Reddit post, a short Discord
   and forum post, title options, a pre-posting checklist and a 0.2.2
   screenshot), but the owner is not announcing releases yet (2026-09-27).
   Leave it until they say otherwise.
4. **Done: the testing gaps** (2026-09-27). Real RAR4 and RAR5 archives are read
   on every CI platform, and the preview's sides are linked while comparing. A
   scan of a real `.cbr` runs only locally, against `/comics`.
5. **Optional: a Linux AppImage**, if people ask for something other than the
   tarball.
6. **Decide: bundle the official 7-Zip in the release binaries**, as
   PageMaster does with its 7-Zip sidecar, so `.cbr` and `.cb7` open with
   nothing to install. Redistribution is allowed with 7-Zip's licence text.
   Costs: a few MB per platform (the binaries are 47-104 MB), keeping the
   bundled copy patched (7-Zip has had real CVEs, such as CVE-2025-0411), and
   nothing changes for a PyPI install. Decide with user feedback. Writing our
   own RAR reader or writer was considered and rejected: CBZ is the output
   everywhere, pages barely compress, and a new decoder of untrusted archives
   is attack surface that libarchive and 7-Zip have spent years fuzzing.

Not in 0.3 (decided 2026-09-27): code signing (macOS notarization and a
Windows certificate cost money and need the owner's accounts), EPUB or PDF
support, and Komga/Kavita integration beyond the post-import `clean --known`
hook. Revisit once there is feedback.

## Releasing

Only when the owner asks. 0.2.3 went out this way on 2026-09-28:

1. Add the version's section to [CHANGELOG.md](../CHANGELOG.md) as work lands,
   headed `## X.Y.Z (unreleased)`, written for people downloading the app.
2. Check changes that touch the window on a Windows desktop first (the VM
   below): the native Windows style draws lists and check boxes differently
   from Fusion, and two bugs were found there.
3. Set the version in `pyproject.toml` and `src/comiccleaner/__init__.py`,
   change "(unreleased)" in the heading to the date, and commit.
4. Tag `vX.Y.Z` (annotated, "Comic Cleaner X.Y.Z") and push the tag.

CI then checks the tag against both version numbers, and the tag's test job
runs `python build.py --release-notes X.Y.Z` before anything is built: a
section that is missing, empty or still unreleased fails the release within a
minute. The release job publishes that section, unwrapped into whole
paragraphs (GitHub shows every line break in a release description), with
GitHub's generated compare link after it. `python build.py --release-notes
X.Y.Z` previews exactly what will be published. Afterwards, download the Linux
binary and run its `--self-test` on Debian 12 with the official 7-Zip.

## CI

- **Green, with no warnings**, as of 0.2.3. Before that, three runs failed on
  2026-09-27 and each was fixed by the next commit: a window-width test
  measured in pixels (Windows' offscreen fonts are about 1.7x wider), a
  deliberate diagnostic run on a temporary branch, and a mypy error.
- **Actions** are on their Node 24 majors (checkout 7, setup-python 7,
  upload-artifact 7, download-artifact 8, 2026-09-28), which ended seven Node 20
  deprecation warnings a run. None of their breaking changes touch how they
  are used here.
- **Ubuntu is pinned**: the test and release jobs run on `ubuntu-24.04`, because
  `ubuntu-latest` becomes 26.04 on 2026-10-19 and the Qt libraries are
  installed by package name; move them on purpose. The Linux build stays on
  `ubuntu-22.04` for glibc 2.35; GitHub will retire that image in time, so plan
  its replacement.
- **Linting the workflows**: `docker run --rm -v "$PWD":/repo:ro --workdir /repo
  rhysd/actionlint:latest -no-color`. It reports only two shellcheck notes on
  the release job's `*` globs, left as they are: `./*` would put `./` into
  `SHA256SUMS.txt`.

## The Windows 11 check

On the VM at `~/VMs/pagemaster-win11` (1280x800, 100%), shared with PageMaster:
**ask its session first**, and wait for its "free" message. It keeps a
don't-touch list: `C:\Bench`, `C:\Books`, `C:\Portable`, `C:\OEM`, the SSH
setup, the 9223 port proxy, the WebView2 policy key, and anything PageMaster
(0.9.0 is installed and owns the comic file types, so launch Comic Cleaner by
path, never by double-clicking a book).

**0.2.2, 2026-09-27 afternoon**, then the CI build of `1b11dca`:

- The window could be made 987 px wide, well inside 1280, but opened at
  1300x820, off the screen, and the preview's buttons were off the bottom;
  fixed in `1b11dca` and checked (opens at 1272x743, preview whole).
- Themes on the native style: dark check boxes and "Filter books" are fine.
  High contrast drew the selected row black on navy; fixed and checked.
- `.cbr` reads with no 7-Zip installed: Windows 10 and 11 ship `tar.exe`
  (bsdtar 3.8), which reads RAR, so the "no tool" banner cannot appear there.
- Apply, then the rescan starts by itself after the result box (pages 249 to
  244); History restore puts all 10 books back byte-identical.

**Everything in 0.2.3, 2026-09-27 evening**, with the CI build of `0f35309`,
all passed: the window opens at 1272x743 and narrows to 1078 px with the
library still readable; group rows line up; Settings lists the official 7-Zip
and "UnRAR: not found"; with "Leave .cbr and .cb7 books unchanged" ticked,
Apply's confirmation named the five Green Lantern `.cbr` books, cleaned only
the Batman `.cbz` ones, and left the `.cbr` files byte-identical; linked zoom
kept an 800x1200 reference and a 600x900 copy on the same part of the page
through the wheel, a drag, stepping and 1:1.

Not checked: 150% scaling (the VM is at 100%, and it is PageMaster's to
change). macOS has never been checked by hand; CI's self-test of the `.app` is
the only check.

How the VM is driven: SSH as `pm` (key and known_hosts in
`~/VMs/pagemaster-win11/ssh/`, port 2222), everything in one folder under
`C:\Users\pm`, launched in the desktop session through `C:\Books\run.ps1`
(unmodified), with a small PowerShell script of our own for clicks
(`SetCursorPos`, `mouse_event`, including the wheel and drags), keys
(`SendKeys`) and screenshots (`CopyFromScreen`). Start the app with
`Start-Process -WindowStyle Normal`: from a hidden-window process its main
window comes up hidden. Afterwards remove the folder,
`HKCU\Software\Playback Software\Comic Cleaner`,
`%LOCALAPPDATA%\Playback Software\Comic Cleaner` and any `_MEI*` temp folder
holding `comiccleaner`, then tell the PageMaster session it is free.

## Test content

`CC_TEST_COMICS` in `.env` is mounted read-only at `/comics`. On this machine it
is `/home/prime/Temp/PageMaster-Test-Content`, shared with PageMaster: 8 CBZ
(Batman: Shadow of the Bat #001-005, 1992; Justice League International TPBs
1-3), 5 CBR (Green Lantern Corps #001-005, 2006), and books the cleaner skips
(2 PDF, 4 EPUB, 1 AZW3). It is copyrighted, so it is not in the repo. A scan
finds 4 groups, 17 pages, 8.9 MB.

## Loose ends

- **`gh` is installed** at `~/.local/bin/gh` (the official release binary;
  `sudo` needs a password here) and logged in as `trevoedwards` over HTTPS.
- **`.gui/` is local state** for the `gui` service: a writable copy of the test
  comics, the app's settings and history, and screenshots. Delete it to start
  fresh. `docker compose exec gui sh -c 'DISPLAY=:1 xdotool ...'` is the
  pattern for driving it.
- **Some files have CRLF line endings** from the Windows machine. Git
  normalizes them on commit (`.gitattributes`), so the warnings are harmless.
- **More than one tool writes here:** Cursor has rules in `.cursor/rules/` and
  `AGENTS.md`; Claude Code follows `CLAUDE.md`.
