"""
Page-selection parsing shared by every tool that asks "which pages?".

This logic used to live inside ``ocr.py`` (``parse_page_range``) even though
Summarize and Compare needed it too, so ``app.py`` reached into the OCR module
through a confusingly named shim (``sum_module_parse_range``). It is not an OCR
concern, so it lives here instead and OCR re-exports it for compatibility.

``resolve_scope`` implements the All / Current / Selected / Range choice that
the OCR, Summarize and Compare screens all present.
"""

from __future__ import annotations

import re


class PageSelectError(ValueError):
    """Invalid page selection. Subclasses ValueError so existing
    `except ValueError` handlers keep working."""


def parse_page_range(spec: str, total: int) -> list[int]:
    """Turn '1-3,5,7-8' into a sorted, unique, in-range list of page numbers."""
    spec = (spec or '').strip()
    if not spec:
        raise PageSelectError('Page range is empty.')
    pages: set[int] = set()
    for token in re.split(r'[\s,]+', spec):
        if not token:
            continue
        m = re.match(r'^(\d+)-(\d+)$', token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < 1 or b < 1 or a > b:
                raise PageSelectError(f'Invalid range "{token}".')
            for p in range(a, b + 1):
                if 1 <= p <= total:
                    pages.add(p)
        elif token.isdigit():
            p = int(token)
            if 1 <= p <= total:
                pages.add(p)
        else:
            raise PageSelectError(f'Invalid page range token "{token}".')
    if not pages:
        raise PageSelectError('Page range matched no valid pages.')
    return sorted(pages)


def parse_page_list(spec: str, total: int) -> list[int]:
    """Parse a plain comma/space separated list of page numbers."""
    out = []
    for token in [t for t in re.split(r'[\s,]+', spec or '') if t]:
        if not token.isdigit():
            raise PageSelectError(f'Invalid page number "{token}".')
        p = int(token)
        if 1 <= p <= total:
            out.append(p)
    out = sorted(set(out))
    if not out:
        raise PageSelectError('No valid pages selected.')
    return out


def resolve_scope(scope: str, pages_raw: str, current_page_raw, total_pages: int) -> list[int]:
    """
    Resolve the page-scope radio choice into concrete page numbers.

    `current_page_raw` is deliberately preferred over `pages_raw` for the
    'current' scope: the page-numbers textbox may still hold stale text from a
    previous choice, which previously caused a raw int() crash.
    """
    scope = (scope or 'all').strip()
    if scope == 'all':
        return list(range(1, total_pages + 1))
    if scope == 'current':
        raw = str(current_page_raw or pages_raw or '1').strip()
        if not raw.isdigit():
            raise PageSelectError(f'Invalid current page "{raw}".')
        cp = int(raw)
        if not (1 <= cp <= total_pages):
            raise PageSelectError(
                f'Current page {cp} is out of range (1–{total_pages}).')
        return [cp]
    if scope == 'selected':
        return parse_page_list(pages_raw, total_pages)
    if scope == 'range':
        return parse_page_range(pages_raw, total_pages)
    raise PageSelectError(f'Unknown scope "{scope}".')