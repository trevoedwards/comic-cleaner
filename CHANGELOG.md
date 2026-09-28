# Changelog

What changed in each release of Comic Cleaner. Downloads are on the
[releases page](https://github.com/trevoedwards/comic-cleaner/releases).

## 0.3.0 (unreleased)

### New

- **A Linux AppImage.** `ComicCleaner-<version>-x86_64.AppImage` is one file
  to download, make executable and run, with no tarball to unpack. It carries
  the libraries Qt needs to open a window, including the X11 ones many desktops
  lack, and has a desktop entry and icon for AppImage integration tools. It
  mounts itself through FUSE; where that is missing, run it with
  `--appimage-extract-and-run`. The tarball is still there.

### Changed

- The Linux tarball now carries the rest of the X11 (xcb-util) libraries Qt's
  window support needs, not only `libxcb-cursor0`, so it starts on more
  desktops.

## 0.2.3 (2026-09-28)

### New

- **Leave .cbr and .cb7 books unchanged.** A new setting (**Settings →
  Removing**) and command-line option (`--leave-cbr`) for keeping RAR and 7z
  books exactly as they are. Those books are still scanned and reviewed, and
  their matches are listed, but they are never rewritten or converted to
  `.cbz`. A ZIP named `.cbr` counts too. The confirmation names the books it
  leaves alone, saved plans give the reason, and `clean --json` lists them
  under `left_unchanged`.
- **Linked zoom when comparing.** In the preview, both sides now zoom and pan
  together and show the same part of the page, even when one copy has been
  rescaled. They keep their place as you step through the copies, and **1:1**
  shows the copy's real pixels with the reference following it.

### Fixed

- The main window and the preview always opened at 1400×860 and 1100×820. On
  smaller screens they ran under the taskbar, and the preview's Next, Fit, 1:1
  and Close buttons were out of reach. Both now open within the screen.
- On Windows, a selected row kept its own text colour: black on dark blue in
  High contrast, and hard to read in the light theme. Selected rows now use
  the selection's text colour.
- At the window's smallest width, the library column shrank to a sliver. It
  now stays wide enough to read, and the window still fits a screen 1280
  pixels wide.
- In the list of duplicate groups, each row's text started at a different place
  depending on the shape of its thumbnail. The rows now line up.
- In the preview, a caption that wrapped onto a second line pushed one image
  lower than the other. Both images now start level.

### Changed

- **The official 7-Zip is now the recommended tool for `.cbr` and `.cb7` on
  every platform**: on Linux and macOS, that is `7zz` from
  [7-zip.org](https://www.7-zip.org/). The 7-Zip that Debian, Ubuntu, Fedora
  and Homebrew ship is built without RAR support, so it reads `.cb7` but not
  `.cbr`. Comic Cleaner detects that and says so, and with several 7-Zips
  installed it uses one that reads RAR. If you installed `unrar` as 0.2.2
  suggested, it still works, and so does bsdtar; Windows 10 and 11 read `.cbr`
  through their own `tar.exe` even without 7-Zip.
- **Settings → Archive tools** lists UnRAR only when UnRAR itself is
  installed, instead of showing the 7-Zip path in that row.
- The README explains [why a cleaned `.cbr` comes back as a `.cbz`](https://github.com/trevoedwards/comic-cleaner#why-cbz)
  and how to keep your originals untouched.

### Tested

Checked by hand on Windows 11 and Linux against real comics. The automated
tests now also read real RAR4 and RAR5 archives on Windows, macOS and Linux.
The requirements are unchanged; the Linux binary needs glibc 2.35 or newer.
