"""Keeping ComicInfo.xml consistent after pages are removed.

ComicTagger, Komga and most readers trust PageCount and the Pages block, so
leaving stale entries behind causes visible glitches (wrong page counts, covers
pointing at a deleted image).
"""

from __future__ import annotations

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


def update_comicinfo(
    xml_bytes: bytes, removed_indices: set[int], new_page_count: int
) -> bytes:
    """Drop removed Page elements, renumber the rest, and fix PageCount.

    Returns the original bytes unchanged if the XML cannot be parsed — a broken
    ComicInfo is not a reason to fail the whole removal.
    """
    try:
        root = ET.fromstring(xml_bytes.decode("utf-8-sig", errors="replace"))
    except ET.ParseError as exc:
        log.warning("ComicInfo.xml is not valid XML (%s); leaving it untouched", exc)
        return xml_bytes

    pages_el = root.find("Pages")
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

    count_el = root.find("PageCount")
    if count_el is not None:
        count_el.text = str(new_page_count)
    elif pages_el is not None:
        # Only add PageCount if the file already tracked pages at all.
        ET.SubElement(root, "PageCount").text = str(new_page_count)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
