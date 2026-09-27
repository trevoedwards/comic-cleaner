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

1. **Check 0.2.2 by hand on a real Windows 11 desktop** (next section). Every
   GUI check so far ran on Linux, and the window-width fix was measured only
   under Qt's offscreen platform there.
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

On Windows 11, by hand, with `ComicCleaner-0.2.2-windows-x64.exe`. There is a
Windows 11 VM at `~/VMs/pagemaster-win11` (SSH on port 2222), shared with
PageMaster, so check it is free first.

1. At 1920x1080 and 150% scaling, the window fits the screen and can be made
   narrower than it opens.
2. Dark and High contrast themes on the native style: check boxes in the page
   grid and "Filter books" are readable (the 0.2.2 fix applies to Fusion only).
3. With 7-Zip installed, `.cbr` and `.cb7` read; without any tool, the banner
   names 7-Zip and WinRAR.
4. Apply a removal: after the result box is closed, the rewritten books are
   rescanned by themselves (the status line counts them all as scanned).
5. History restore puts the books back, and the library rescans them.

macOS has never been checked by hand; CI's self-test of the `.app` is the only
check.

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
  offset depending on the thumbnail's width.
- **Some files have CRLF line endings** from the Windows machine. Git
  normalizes them on commit (`.gitattributes`), so the warnings are harmless.
- **More than one tool writes here:** Cursor has rules in `.cursor/rules/` and
  `AGENTS.md`; Claude Code follows `CLAUDE.md`.
