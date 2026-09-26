# Reviewing groups

Back to the [README](../README.md).

## Narrowing the review

- **Select books** in the library to see only the groups they contain, and type
  in **Filter books** to find one. Each book shows how many of its pages repeat,
  or how many are marked for removal.
- The **group filter** shows only undecided, marked, known-junk, identical,
  similar, or flagged groups. **Show all** clears both.
- **Mark all** and **Clear all** act on the groups shown, or only on the selected
  groups when several are selected. **Mark safe** marks known junk, and pages
  identical across enough books with nothing to warn about: no blank pages, no
  covers, and not only mid-book.
- The group filter also has **Deferred** and **Mid-book only**, and the sort has
  **Warnings first**, **Near the edges** and **Widest similar** (the similar
  groups whose copies differ most from their reference). **Next Warning** (W)
  jumps to the next flagged group; hover over a warning for what it means.
- **Defer** (S) sets a group aside: it stays listed, nothing is remembered, and
  "next undecided" steps over it. The status line counts what is still undecided.
- Books show "Series #Number" from their ComicInfo.xml when they have one.

## Undo

**Ctrl+Z** takes back the last review step: a decision (on one group or many),
a tick, an ignore, or a preview's changes. Fifty steps are kept for as long as
the app is open. Once a real removal starts the history is cleared, since Undo
puts back decisions, not files; **History…** is how a run is undone.

## Known junk

Once a library is clean, the advert that used to repeat across forty books is
gone, so the same advert arriving in book forty-one repeats against nothing and
would never be grouped. So every page you remove is **remembered**: wherever it
turns up again, even in a single new book, it is grouped as **known junk** and
marked for removal. Apply still asks first.

**Remembered Pages…** on the toolbar lists known junk with a thumbnail, and
lets you forget any of it. **Export…** writes the list to a file and **Import…**
merges in someone else's, so a list of one scanlation group's credit pages can
be shared. Only import lists from people you trust: whatever is on one is marked
for removal on sight. The setting *Remember removed pages* turns all of this off.
Each entry has a note and tags you can edit, and the search box looks through
notes, tags and ids.

The first time a run would delete pages that only an **imported** list vouches
for (they match nothing you removed yourself), the app asks once more, naming
how many imported signatures are involved. Saying yes covers the rest of that
session.

**Review → Remove Known Junk Only…** marks every known-junk group and applies
just those, leaving every other group's decision alone.
