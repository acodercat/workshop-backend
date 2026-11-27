"""Between Traditional (Hong Kong) and Simplified Chinese.

Minutes are written in Traditional; a Simplified copy is derived rather
than generated, because it says exactly the same thing in another script
and costs nothing. Character-level conversion (OpenCC ``hk2s`` / ``s2hk``):
it changes how words are written, not which words are used.
"""

import re
from functools import cache

from opencc import OpenCC

from app.schemas.meeting import MinutesLanguage

_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")


def has_han(text: str) -> bool:
    return bool(_HAN.search(text))


@cache
def _converter(config: str) -> OpenCC:
    return OpenCC(config)


def to_script(text: str, target: MinutesLanguage) -> str:
    """``text`` in the script ``target`` names; English is not a script."""
    if target == "sc":
        return _converter("hk2s").convert(text)
    if target == "tc":
        return _converter("s2hk").convert(text)
    raise ValueError("English is not a Chinese script")


def with_script_variants(variants: dict[MinutesLanguage, str]) -> dict[MinutesLanguage, str]:
    """Fill in the missing Chinese script from the one that is present."""
    tc, sc = variants.get("tc", ""), variants.get("sc", "")
    if not sc and tc and has_han(tc):
        return {**variants, "sc": to_script(tc, "sc")}
    if not tc and sc and has_han(sc):
        return {**variants, "tc": to_script(sc, "tc")}
    return variants
