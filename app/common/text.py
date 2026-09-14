"""Text helpers shared by validation and display.

``printable`` exists so that "does this text say anything" is decided by one
function. A validator that asked ``value.strip()`` while a renderer stripped
control characters first would disagree about text made only of format
characters — U+200B is not whitespace to ``str.strip``, and is nothing at all
on screen — and the renderer would then index into an empty result.
"""

import unicodedata


def printable(text: str) -> str:
    """*text* without its control and format characters.

    Drops every Unicode ``C*`` category — controls (U+0000 first among them,
    which Postgres rejects in ``text``), format characters such as the
    zero-width space, surrogates and unassigned code points — while keeping
    tab and the line breaks, which a caller splitting on lines still needs.
    """
    return "".join(
        ch for ch in text if ch in "\t\n\r" or not unicodedata.category(ch).startswith("C")
    )
