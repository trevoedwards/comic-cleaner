# Settings

Back to the [README](../README.md).

| Setting | Default | Notes |
|---|---|---|
| **Theme** | Follow system | Light, dark or the OS setting, previewed live. |
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
