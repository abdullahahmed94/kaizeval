"""Small, dependency-free text helpers shared by deterministic checks, the sample agent and the fake model."""

from __future__ import annotations

import re
import unicodedata

STOPWORDS = frozenset("""
a an and are as at be but by can could do does for from had has have how i if in into is it its
may me my of on or our should so than that the their them then there these they this to was we
were what when where which who whom why will with would you your about any also not no yes
according say says said tell me please explain describe document documentation policy cover covers
work works know
""".split())

_WORD = re.compile(r"[^\W_]+(?:[.,][0-9]+)*", re.UNICODE)
_SENT = re.compile(r"(?<=[.!?؟。])\s+|\n+")
_ARABIC_DIACRITICS = re.compile(r"[ً-ٰٟـ]")

ABSTAIN_PATTERNS = [
    r"\bi (?:do not|don't|dont) know\b", r"\bi(?: am|'m) not sure\b", r"\bno information\b",
    r"\bnot (?:able|in a position) to (?:answer|help)\b", r"\bcannot (?:answer|find|help with)\b",
    r"\bcan't (?:answer|find|help with)\b", r"\bunable to (?:answer|find)\b",
    r"\b(?:not|isn't|is not) (?:covered|mentioned|available|stated|found) in\b",
    r"\boutside (?:of )?(?:my|the) (?:scope|knowledge)\b", r"\bdo not have (?:that|this|any|enough) information\b",
    r"\bdon't have (?:that|this|any|enough) information\b",
    r"لا أعرف", r"لا اعرف", r"مش عارف", r"لا تتوفر", r"ليس لدي معلومات", r"معنديش معلومة",
]
_ABSTAIN = re.compile("|".join(ABSTAIN_PATTERNS), re.IGNORECASE)


def normalize(text: str) -> str:
    """Lower-case, strip diacritics and punctuation noise, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[‘’“”\"'`*_#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOPWORDS and len(t) > 1]


def stem(token: str) -> str:
    """A deliberately tiny English suffix stripper (used for optional query expansion)."""
    for suffix in ("ies", "ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def sentences(text: str) -> list[str]:
    return [p.strip() for p in _SENT.split(text or "") if p and p.strip()]


def contains_fact(haystack: str, fact: str) -> bool:
    """True when ``fact`` appears in ``haystack`` after normalisation (numbers compared without separators)."""
    h, f = normalize(haystack), normalize(fact)
    if not f:
        return False
    if f in h:
        return True
    squeeze = lambda value: re.sub(r"(?<=\d)[,\s](?=\d)", "", value)  # noqa: E731
    return squeeze(f) in squeeze(h)


def overlap_recall(reference: str, candidate: str) -> float:
    """Share of the reference's content tokens that also occur in the candidate."""
    ref = set(content_tokens(reference))
    if not ref:
        return 0.0
    cand = set(content_tokens(candidate))
    return len(ref & cand) / len(ref)


def looks_like_abstention(text: str, extra_patterns: list[str] | None = None) -> bool:
    if not text or not text.strip():
        return True
    if _ABSTAIN.search(text):
        return True
    for pattern in extra_patterns or []:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def dominant_script(text: str) -> str:
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        script = "arabic" if "ARABIC" in name else "cjk" if "CJK" in name else "cyrillic" if "CYRILLIC" in name else "latin"
        counts[script] = counts.get(script, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else "none"


INJECTION_PATTERNS = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) instructions|disregard (?:the )?(?:above|previous)|"
    r"you are now|system prompt|reveal your (?:instructions|prompt)|act as (?:an? )?(?:unrestricted|different)|"
    r"</?(?:system|instructions?)>", re.IGNORECASE)


def injection_flags(text: str) -> list[str]:
    return ["possible_injection"] if INJECTION_PATTERNS.search(text or "") else []
