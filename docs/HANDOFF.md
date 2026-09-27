# Comic Cleaner handoff: 2026-09-27

Where the work stands and what comes next. [CLAUDE.md](../CLAUDE.md) holds the
rules (Docker-only development); [Development](development.md) has the commands.
This file covers the state of the work.

## Where things are

**0.2.2 is released** (tag `v0.2.2`, GitHub release with Windows, macOS and
Linux binaries). Since 0.2.0:

- 0.2.1: the library selection survives folding a heading, confirmation lines
  show file names only, and the scan's time left goes by bytes, not books.
- 0.2.2: the Linux binary is built on Ubuntu 22.04 (glibc 2.35; the 0.2.1 one
  needed 2.38 and would not start on Debian 12). A 7-Zip without RAR support
  (Debian's and Ubuntu's) is detected and the app asks for `unrar`. Books
  rewritten by Apply in the GUI are rescanned again (they never were: the
  thread exited while the result box was open). The window fits a laptop
  screen (its minimum was about 1500 px). On Linux's Fusion style, dark-theme
  check boxes and placeholder text are visible.

**On `master`, not yet released** (`1b11dca`, found by the Windows check
below): the main window and the preview open no larger than the screen (they
were fixed at 1400x860 and 1100x820, and on 1280x800 the preview's Close
button was under the taskbar), and a selected row takes the selection's text
colour under the Windows styles (it was black on navy in high contrast).
Verified on Windows 11 with the CI build of that commit. It goes out with the
next release.

Development moved to a Linux machine and into Docker on 2026-09-27. `dev` runs
the tests; `gui` serves the real window to a browser over noVNC (see
[Development](development.md)).

### What was verified, and how

- **Full suite and ruff** in the `dev` container, and CI green on Ubuntu
  (Python 3.10 and 3.12), macOS and Windows.
- **The command line on the real test comics** (`/comics`, below), on a
  writable copy: scan, dry run with a plan file, `--output`, clean in place
  with backups and quarantine, rescan (nothing left), `history restore`
  (byte-identical to the originals, `.cbr` back, converted `.cbz` gone), and
  `clean --known`. All 17 pages removed were real junk: two DC house adverts,
  a scan group's logo, a watermark.
- **The GUI by real input**, in the `gui` container with `xdotool` and
  screenshots: scan, D/K/S and Ctrl+Z, preview (1:1, pan, next, untick),
  grouping and folding, context menus, Apply, History restore (byte-identical),
  Settings and every theme. Four bugs found this way are fixed in 0.2.2.
- **The released Linux binary on Debian 12** (glibc 2.36): its self-test read
  all 13 archives, `.cbr` included.

## Next steps: 0.3, in order

The milestone is getting Comic Cleaner to users, after checking the platform
most of them will use. There are no users yet: no issues or stars, and the
downloads are our own. Features beyond this wait for their feedback.

1. **Done 2026-09-27: 0.2.2 checked by hand on Windows 11** (see "Tests that
   still need running" for what that covered and what it found).
2. **Publish to PyPI.** The name `comiccleaner` is not taken (checked
   2026-09-27). The owner creates the PyPI project and an API token and adds it
   as the `PYPI_TOKEN` secret; then run **PyPI package** (`pypi.yml`) from the
   Actions tab. Check it in a clean container:
   `docker run --rm python:3.12 pip install comiccleaner && comiccleaner --help`.
   Then give the README a `pip install comiccleaner` line.
3. **Announce it.** Draft a short post (what it does, a screenshot from
   `docs/`, the release link) for the owner to post: the Komga, Kavita and
   Mylar communities, r/comicbooks. Outward-facing, so the owner posts it.
4. **Close the testing gaps** in CI and the preview:
   - CI installs `unrar-free` on Ubuntu, which cannot read RAR5, so no `.cbr`
     test there exercises RAR5. Install the real `unrar` (Ubuntu multiverse).
     A RAR5 fixture needs the proprietary `rar` to make; decide whether to
     commit a small one.
   - In the preview, each side zooms and pans on its own; linking them while
     comparing would keep the two copies lined up.
5. **Optional: a Linux AppImage**, if people ask for something other than the
   tarball.

Not in 0.3 (decided 2026-09-27): code signing (macOS notarization and a
Windows certificate cost money and need the owner's accounts), EPUB or PDF
support, and Komga/Kavita integration beyond the post-import `clean --known`
hook. Revisit once there is feedback.

## Tests that still need running

**Windows 11, done 2026-09-27** on the VM at `~/VMs/pagemaster-win11`
(1280x800, 100%; shared with PageMaster, so ask its session first), with
0.2.2 and then the CI build of `1b11dca`, by real clicks and screenshots:

- Fits: the window can be made 987 px wide, well inside 1280. It opened at
  1300x820, off the screen, and the preview's buttons were off the bottom;
  fixed in `1b11dca` and checked (opens at 1272x743, preview whole).
- Themes on the native style: dark check boxes and "Filter books" are fine.
  High contrast drew the selected row black on navy; fixed and checked.
- `.cbr` reads with no 7-Zip installed: Windows 10 and 11 ship `tar.exe`
  (bsdtar 3.8), which reads RAR, so the "no tool" banner cannot appear there.
  With a portable 7-Zip set by `COMICCLEANER_7Z`, Settings lists it.
- Apply, then the rescan starts by itself after the result box (pages
  249 to 244); History restore puts all 10 books back byte-identical.

Not checked: 150% scaling (the VM is at 100%, and it is PageMaster's to
change). macOS has never been checked by hand; CI's self-test of the `.app`
is the only check.

How the VM was driven, for next time: SSH as `pm` (key and known_hosts in
`~/VMs/pagemaster-win11/ssh/`, port 2222), everything in one folder under
`C:\Users\pm`, launched in the desktop session through `C:\Books\run.ps1`,
with a small PowerShell script of our own for clicks (`SetCursorPos`,
`mouse_event`), keys (`SendKeys`) and screenshots (`CopyFromScreen`). Start
the app with `Start-Process -WindowStyle Normal`: from a hidden-window process
its main window comes up hidden. Afterwards remove the folder,
`HKCU\Software\Playback Software\Comic Cleaner`,
`%LOCALAPPDATA%\Playback Software\Comic Cleaner` and any `_MEI*` temp folder
holding `comiccleaner`.

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
  fresh. The `xdotool` driving scripts used for the checks lived there too;
  `docker compose exec gui sh -c 'DISPLAY=:1 xdotool ...'` is the pattern.
- **Cosmetic, not fixed:** in the group list, text starts at a different
  offset depending on the thumbnail's width. At the window's minimum width the
  library column shrinks to about 75 px ("Batman: ..."). In Settings, the
  UnRAR row shows `7z.exe` when there is no UnRAR, since 7-Zip reads RAR too.
- **Some files have CRLF line endings** from the Windows machine. Git
  normalizes them on commit (`.gitattributes`), so the warnings are harmless.
- **More than one tool writes here:** Cursor has rules in `.cursor/rules/` and
  `AGENTS.md`; Claude Code follows `CLAUDE.md`.
