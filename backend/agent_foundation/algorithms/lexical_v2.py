from __future__ import annotations

import re


_ASCII_TERM = re.compile(r"[a-z0-9_]+")
_CJK_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def lexical_terms(value: str) -> frozenset[str]:
    """Return stable terms for whitespace-delimited text and unsegmented CJK text."""

    normalized = value.casefold()
    terms = set(_ASCII_TERM.findall(normalized))
    for match in _CJK_SEQUENCE.finditer(normalized):
        sequence = match.group(0)
        terms.update(sequence)
        terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return frozenset(terms)
