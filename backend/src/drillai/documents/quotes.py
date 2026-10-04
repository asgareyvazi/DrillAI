"""Checking that a quoted excerpt really occurs in the source it claims to come from.

What ``quote_verified`` means, and what it does not
---------------------------------------------------

It means: *the text in this evidence link occurs in the stored source*. It does **not** mean the
claim is true, the extraction is correct, or the number is right. An excerpt can match the page
character for character and still be misread — a bar reading taken as a pressure, a figure transcribed
from the wrong row. Verifying that is a human's job, and the flag is deliberately narrow so nobody
mistakes it for approval.

Before this module the flag was written ``False`` at every one of the two places that create an
evidence link, and never updated, so the platform held the information required to check the quote and
never used it. ``evidence_summary`` reported ``verified_quotes: 0`` for every document ever ingested.

Why matching is not string equality
-----------------------------------

Text arrives through a parser that collapses whitespace, drops zero-width characters and may have
substituted a currency or degree sign, and through extractors that rejoin a table row with ``|``. A
byte comparison would fail on every real pair. The comparison here is therefore normalised on both
sides — whitespace runs become one space, the ligatures and quote marks a PDF produces are folded to
their ASCII forms — and containment is then tested on the normalised strings. Case is folded, because
a heading transcribed in title case is not a different fact.

The verification reports *which* source it matched against: the region when one is known, the page
otherwise, and ``unavailable`` when the document was stored without text. That string is kept on the
row so a reader can see that ``quote_verified = false`` means "the text is not there" rather than
"nobody looked".
"""

from __future__ import annotations

import re
import unicodedata

#: Characters a PDF or a Word document produces that carry no meaning for a text match.
_FOLDINGS: dict[str, str] = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u00a0": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u202f": " ",
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}

#: Zero-width and control characters a parser may have left in the source text.
_STRIPPED = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")
_WHITESPACE = re.compile(r"\s+")


def normalize_for_match(text: str | None) -> str:
    """Fold a string to the form two texts are compared in.

    ``NFKC`` first, so a compatibility form (a full-width digit, a superscript) becomes its plain
    equivalent before any of the manual foldings apply.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text))
    for source, target in _FOLDINGS.items():
        folded = folded.replace(source, target)
    folded = _STRIPPED.sub("", folded)
    return _WHITESPACE.sub(" ", folded).strip().casefold()


def excerpt_occurs_in(*, excerpt: str | None, source_text: str | None) -> bool:
    """Whether the excerpt occurs in the source, after both are normalised.

    An empty excerpt is not verified. Treating "no excerpt" as verified would let a link that quotes
    nothing claim to have been checked.
    """
    needle = normalize_for_match(excerpt)
    if not needle:
        return False
    haystack = normalize_for_match(source_text)
    if not haystack:
        return False
    return needle in haystack


def verify_quote(
    *,
    excerpt: str | None,
    region_text: str | None,
    page_text: str | None,
) -> tuple[bool, str]:
    """Verify an excerpt against the region if there is one, else the page.

    Returns ``(verified, how)``, where ``how`` is ``region_text``, ``page_text`` or ``unavailable``.

    The region is tried first and *only* the region when one exists. Falling back to the page would
    turn "this came from region 7" into "this came from somewhere on page 3", which is the difference
    between a citable source and a plausible one — and the locator on the link still claims region 7.
    """
    if region_text is not None:
        if excerpt_occurs_in(excerpt=excerpt, source_text=region_text):
            return True, "region_text"
        return False, "region_text"
    if page_text is not None:
        if excerpt_occurs_in(excerpt=excerpt, source_text=page_text):
            return True, "page_text"
        return False, "page_text"
    return False, "unavailable"
