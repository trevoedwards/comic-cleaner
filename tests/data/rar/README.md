# RAR test archives

Only RARLAB's own `rar` can create a RAR archive, so these are borrowed rather
than built. They are libarchive's test files, from
<https://github.com/libarchive/libarchive/tree/9dc6678ca2ab57fac75889451cd8db1e45e40120/libarchive/test>,
decoded unchanged from the `.uu` files there:

| File | What it covers |
|---|---|
| `test_read_format_rar.rar` | RAR4, compressed, a folder and a symlink |
| `test_read_format_rar5_stored.rar` | RAR5, stored (no compression) |
| `test_read_format_rar5_compressed.rar` | RAR5, compressed |
| `test_read_format_rar5_multiple_files_solid.rar` | RAR5, solid, four files |
| `test_read_format_rar5_unicode.rar` | RAR5, non-ASCII names, a hard link and a symlink |

Their contents were checked with two independent decoders, the official 7-Zip
and libarchive's bsdtar, which agreed on every file. The expected hashes are in
`tests/test_rar_archives.py`.

libarchive is under the 2-clause BSD licence; its notice is kept, whole, in
[`COPYING.libarchive`](COPYING.libarchive).
