"""Typografi: no-break spaces where Norwegian typesetting wants them.

Typography only, never the words: a number keeps its thousands together
("2 330 692" with narrow no-break spaces) and stays on the line with the
word or unit after it ("54 stemmer", "18 %"); "kl.", "ca.", "nr.", "s.",
"Gnr.", "Bnr." and "§" keep their number; a line never starts with a spaced dash; and a
reference like "§ 12-1" never breaks at its hyphen.

The hyphen is held with a nowrap span rather than U+2011: none of the
self-hosted fonts has a glyph for U+2011, so the browser would draw that one
hyphen from a fallback font. U+202F is safe: shapers draw a missing space
from the font's own space glyph.

typo() returns escaped Markup, so it is safe as a Jinja filter.
"""

import re

from markupsafe import Markup, escape

NBSP = " "
NNBSP = " "  # narrow no-break space: the thousands separator

_TUSEN = re.compile(r"(?<=\d) (?=\d{3}(?!\d))")
_TALL_ORD = re.compile(r"(?<=\d) (?=[^\W\d_]|%)")
_FORKORTING = re.compile(r"(?<![\w.])(?i:kl|ca|nr|gnr|bnr)\. ")
_SIDE = re.compile(r"(?<![\w.])s\. (?=\d)")
_PARAGRAF = re.compile(r"(§+) ")
# A spaced dash, also the spaced hyphen the kommune's titles use as one ("Torget - Skuteviken").
_TANKESTREK = re.compile(r" (?=[-–—] )")
_BINDESTREK = re.compile(r"(?<![\w-])\d+(?:-\d+)+(?![\w-])")


def mellomrom(text: str) -> str:
    """The no-break spaces, on plain text (also safe on escaped HTML text:
    no rule touches '&', '<', '>' or the digits of an entity)."""
    text = _TUSEN.sub(NNBSP, text)
    text = _TALL_ORD.sub(NBSP, text)
    text = _FORKORTING.sub(lambda m: m.group(0)[:-1] + NBSP, text)
    text = _SIDE.sub("s." + NBSP, text)
    text = _PARAGRAF.sub(lambda m: m.group(1) + NBSP, text)
    return _TANKESTREK.sub(NBSP, text)


def _bindestrek(html_text: str) -> str:
    return _BINDESTREK.sub(lambda m: f'<span class="nb">{m.group(0)}</span>', html_text)


def typo(value) -> Markup:
    """Plain text → escaped Markup with the no-break rules applied. Markup
    that is already HTML keeps its tags; only the text between them changes."""
    if value is None:
        return Markup("")
    if isinstance(value, Markup):
        parts = re.split(r"(<[^>]*>)", str(value))
        return Markup("".join(p if p.startswith("<") else _bindestrek(mellomrom(p)) for p in parts))
    return Markup(_bindestrek(str(escape(mellomrom(str(value))))))
