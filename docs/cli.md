# Command line

Back to the [README](../README.md).

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
comiccleaner clean /incoming --known --yes          # only what was removed before
comiccleaner known export junk.json                 # share the known-junk list
comiccleaner history list                           # past runs
comiccleaner history restore 12 --yes               # put run 12's books back
comiccleaner clean /comics --all --dry-run --plan plan.json   # write the plan down
comiccleaner clean /comics --known --yes --quarantine /removed # keep copies of what goes
comiccleaner pack export review-pack.json           # known junk + ignore list, one file
comiccleaner pack import review-pack.json           # merge one in
```

`--plan FILE` writes every book the run would clean or skip, with the pages,
the share of the book and whether it was a dry run, before anything else
happens; with `--dry-run` nothing else is written. `--quarantine DIR` copies each
removed page into `DIR/<book name>/` before the book is replaced; a book whose
pages cannot be copied is left untouched and reported as failed.

`--exclude GLOB` (repeatable, on `scan` and `clean`) leaves out files whose name,
or path relative to a folder you named, matches: `--exclude '*sample*'
--exclude 'Scans/*'`. `--edge-window N` (default 3, at most 10) sets how close
to either end of a book counts as "near the edge" for ranking and for the
mid-book warning; it does not change `--edges`, which limits what `clean`
removes. PDFs are skipped with a note: PDF is not supported.

`scan` also reports books that look like the **same issue twice** (at least 80%
of the smaller book's pages byte-identical to the other's), one line per pair,
and under `"duplicate_books"` in `--json`. Nothing is removed because of it.

`pack` moves the known-junk list and the ignore list between machines. A pack
exported from the GUI also carries its matching settings; the command line has
no saved settings, so it never writes them and only mentions them on import.

A library server hook, after new books arrive:

```bash
comiccleaner clean /library --known --yes
```

It removes only pages already remembered as known junk, so it never acts on a
match nobody has reviewed, and it will not find new adverts.

`clean --known` is the one to schedule: it removes only pages already removed
from this library once (or imported as known junk), so it never acts on a match
nobody has looked at. `--edges 3` only removes copies within three pages of the
start or end of a book, where adverts and credits sit. `scan --json` reports
each group's warnings and position alongside its pages.

Matching options (`--threshold`, `--min-copies`, `--min-books`,
`--include-first-page`, `--skip-last-page`, `--include-blank`) take the GUI's
defaults, but not its saved settings, so a script means the same thing whatever
someone last picked in the Settings dialog. `--exact-only` keeps only
byte-identical groups even at a looser threshold. Run `comiccleaner clean --help`
for everything else, including `--output`, `--backup-dir`, `--no-backup`,
`--delete-backups` and `--no-remember`.

`clean` asks before changing anything and refuses outright when nobody is there
to answer, so an unattended run needs `--yes`. It also skips any book that would
lose more than a quarter of its pages (`--max-fraction` sets the limit; the GUI
always uses a quarter). Adverts are a page or three, and a larger match usually
means two copies of the same issue are matching each other page for page.
`--threshold` goes up to 16, the same ceiling as the GUI's similarity slider. Ctrl+C stops after the current book.

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
