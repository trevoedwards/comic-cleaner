"""Keeping ComicInfo.xml consistent after pages are removed.

ComicTagger, Komga and most readers trust PageCount and the Pages block, so
leaving stale entries behind causes visible glitches (wrong page counts, covers
pointing at a deleted image).

Page Image indexes are read and rewritten in this app's reading order: page
entries sorted by filename with numbers compared as numbers (see
archive.natural_key), so "9.jpg" comes before "10.jpg". That can differ from the
raw order of entries inside the zip or RAR. A tool that numbered pages by raw
archive order will see its indexes renumbered in natural filename order instead.
"""

from __future__ import annotations

import contextlib
import io
import logging
import xml.etree.ElementTree as ET
from bisect import bisect_left

log = logging.getLogger(__name__)

COMICINFO_NAME = "ComicInfo.xml"
SEP_ALT = chr(92)


def find_comicinfo(entry_names: list[str]) -> str | None:
    """ComicInfo.xml, matched case-insensitively and at any depth."""
    for name in entry_names:
        base = name.replace(SEP_ALT, "/").rsplit("/", 1)[-1]
        if base.lower() == COMICINFO_NAME.lower():
            return name
    return None


def _local(tag: object) -> str:
    """An element's name without its namespace: "{uri}Pages" -> "Pages"."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _namespace(tag: str) -> str:
    """The "{uri}" part of a tag, or "" for an unqualified one."""
    return tag[: tag.index("}") + 1] if tag.startswith("{") else ""


def _child(parent: ET.Element, name: str) -> ET.Element | None:
    """The first direct child called `name`, in whatever namespace."""
    return next((el for el in parent if _local(el.tag) == name), None)


def _declared_namespaces(text: str) -> list[tuple[str, str]]:
    """(prefix, uri) for every namespace the document declares; "" is the default."""
    found: list[tuple[str, str]] = []
    with contextlib.suppress(ET.ParseError):
        for _event, pair in ET.iterparse(io.StringIO(text), events=("start-ns",)):
            if pair not in found:
                found.append(pair)
    return found


def _serialise_namespaced(root: ET.Element, text: str) -> bytes:
    """Write a namespaced document back with the prefixes it came with.

    ElementTree would otherwise invent "ns0:" prefixes, which some readers do
    not understand even though the XML means the same thing. Its own
    default_namespace option cannot be used: it rejects the unqualified
    attributes (Image, Type) every Page element has. Registering the default
    namespace under the empty prefix gives the same output without that check.
    """
    # The registry is process-wide, so it is put back afterwards: one book's
    # prefixes must not leak into how the next book is written.
    registry: dict[str, str] = getattr(ET, "_namespace_map", {})
    saved = dict(registry)
    try:
        for prefix, uri in _declared_namespaces(text):
            # Reserved "ns<n>" prefixes are refused; ElementTree then picks its own.
            with contextlib.suppress(ValueError):
                ET.register_namespace(prefix, uri)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    finally:
        registry.clear()
        registry.update(saved)


# What the library shows from ComicInfo.xml, by element name.
METADATA_FIELDS = ("Series", "Number", "Title")

# A ComicInfo.xml is a few kilobytes; anything far bigger is not worth parsing.
_MAX_METADATA_BYTES = 4 * 1024 * 1024


def read_metadata(xml_bytes: bytes) -> dict[str, str]:
    """Series, Number and Title from a ComicInfo.xml, matched by local name.

    Only direct children of the root count, whatever their namespace. A broken
    or oversized document gives an empty dict: metadata is a nicety, never a
    reason to fail a scan.
    """
    if len(xml_bytes) > _MAX_METADATA_BYTES:
        return {}
    try:
        root = ET.fromstring(xml_bytes.decode("utf-8-sig", errors="replace"))
    except ET.ParseError as exc:
        log.debug("ComicInfo.xml is not valid XML (%s); no metadata read", exc)
        return {}
    found: dict[str, str] = {}
    for name in METADATA_FIELDS:
        element = _child(root, name)
        text = (element.text or "").strip() if element is not None else ""
        if text:
            found[name.lower()] = " ".join(text.split())[:200]
    return found


def update_comicinfo(
    xml_bytes: bytes, removed_indices: set[int], new_page_count: int
) -> bytes:
    """Drop removed Page elements, renumber the rest, and fix PageCount.

    `removed_indices` are positions in this app's reading order (see the module
    docstring). Elements are matched by local name, so a namespaced ComicInfo is
    updated too, and written back with its own namespaces.

    Returns the original bytes unchanged if the XML cannot be parsed — a broken
    ComicInfo is not a reason to fail the whole removal.
    """
    text = xml_bytes.decode("utf-8-sig", errors="replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        log.warning("ComicInfo.xml is not valid XML (%s); leaving it untouched", exc)
        return xml_bytes

    pages_el = _child(root, "Pages")
    if pages_el is not None:
        removed_sorted = sorted(removed_indices)
        for page_el in list(pages_el):
            raw = page_el.get("Image")
            try:
                image_index = int(raw) if raw is not None else None
            except ValueError:
                image_index = None
            if image_index is None:
                continue
            if image_index in removed_indices:
                pages_el.remove(page_el)
                continue
            # Each survivor slides down by the number of removed pages before it.
            # Counting positions among the listed elements instead would be wrong:
            # many tools list only the special pages (cover, ads), not every one.
            page_el.set("Image", str(image_index - bisect_left(removed_sorted, image_index)))

    count_el = _child(root, "PageCount")
    if count_el is not None:
        count_el.text = str(new_page_count)
    elif pages_el is not None:
        # Only add PageCount if the file already tracked pages at all.
        tag = _namespace(pages_el.tag) + "PageCount"
        ET.SubElement(root, tag).text = str(new_page_count)

    if _namespace(root.tag):
        return _serialise_namespaced(root, text)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
