# Safety

Back to the [README](../README.md).

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

Every run is recorded. **History…** on the toolbar (or `comiccleaner history`)
lists them, and puts any book back exactly as it was while its backup exists:
the cleaned version is discarded, and a `.cbr` that was rebuilt as `.cbz` gets
its `.cbr` back. A book changed since the run is left alone rather than
overwritten. **Export CSV…** writes the whole record out for a spreadsheet.

> [!WARNING]
> `.cbr` and `.cb7` cannot be written to. Removing pages from one produces a
> **`.cbz` beside it**, with the original kept as the backup.

Back up anything irreplaceable before a large run, and try the dry run first.
