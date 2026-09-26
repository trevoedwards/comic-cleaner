# Settings

Back to the [README](../README.md).

| Setting | Default | Notes |
|---|---|---|
| **Theme** | Follow system | Light, dark, high contrast or the OS setting, previewed live. High contrast is pure black on white, or white on black when the OS is dark; the removal and keep colours stay the same. |
| **Thumbnails** | Medium (180 px) | Small (120), Medium (180) or Large (240). |
| **Preset** | *(matches the fields)* | *Strict identical*, *Scanlation ads* (similarity 6) or *Within one book* (1 book). It only fills in the matching fields below; editing any of them makes it *Custom*. |
| **Near the edge** | 3 pages | How close to either end of a book counts as where adverts sit (1–10). Used to rank groups, for the mid-book warning, and by *Only Near the Edges*. |
| **Reopen the last library** | on | Brings back the books and review decisions from last time. Turning it off also deletes the saved session. |
| **Check for a new version** | off | Asks GitHub for the latest release number when the app starts, and says so in the status bar if there is one. It is the app's only network request, and sends nothing about your library. *Help → Check for Updates…* asks once. |
| **Similarity** | 0 | Hamming distance. `0` = identical only, `2`–`6` catches re-encodes, above ~`10` expect false matches. |
| **Minimum copies** | 2 | Occurrences before a group is shown. |
| **Across at least (books)** | 2 | Ads repeat across books. `1` also catches a page repeated within one book. |
| **Never match first / last page** | on / off | Protects covers, which legitimately repeat across a series. |
| **Include blank pages** | off | Blank pages look alike to *any* perceptual hash. |
| **Delete backups after a run** | off | Sweeps `.bak` files once every archive in the run has succeeded. |
| **Remember removed pages** | on | Keeps known junk, and marks it for removal in new books. |
| **Write cleaned copies to** | *(in place)* | Point at a folder to leave originals untouched. Books keep their folder layout underneath it, and an existing file is never overwritten. |
| **Copy removed pages to** | *(none)* | Each removed page is copied into this folder, under its book's name, before the book is replaced. If the copy fails, that book is left untouched. |
| **Skip files matching** | *(none)* | Glob patterns, one per line, matched against a file's name and its path inside the folder being added (`*sample*`, `Scans/*`). Matching files are not imported. |
| **Never change books in** | *(none)* | Folders, one per line. Their books are imported and reviewed as usual but never rewritten; Apply lists them as left out. |

The app also remembers two view choices: **View → Group Library by Series**
(groups books under their ComicInfo series, or their folder when they have
none; click a heading to fold it) and **Review → Only Near the Edges**.
