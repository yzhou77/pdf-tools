"""
PDF comparison.

Extracts text from two documents (re-using the OCR fallback from
``summarize.extract_pages_text``) and diffs them with ``difflib``, whose
opcodes map directly onto what we need:

    equal   -> unchanged
    insert  -> added   (present only in document B)
    delete  -> deleted (present only in document A)
    replace -> modified (paired A/B units, plus a word-level sub-diff)

Every unit keeps the page it came from, so the report can tell the user
*where* each change lives in each file.

Granularity
-----------
paragraph | sentence | line | word  (default: sentence)
Inside a "modified" pair we always run an extra word-level diff so the UI can
highlight exactly which words changed.

Whitespace
----------
``ignore_whitespace=True``  (default) collapses whitespace runs before
comparing, so re-flowed text is not reported as a change.
``ignore_whitespace=False`` compares raw text, surfacing space/tab/line-break
differences.

Large documents
---------------
difflib is O(n*m) in the worst case, so when the number of units exceeds
``CHUNK_UNITS`` the diff is performed in sequential aligned windows ("chunks")
and the per-chunk results are concatenated. Progress is reported per chunk.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import time
import jobs

from PyPDF2 import PdfReader

try:
    from summarize import extract_pages_text, split_sentences, detect_language
    _HAS_SUM = True
except Exception:
    extract_pages_text = None
    split_sentences = None
    detect_language = None
    _HAS_SUM = False


class CompareError(Exception):
    """User-facing comparison failure."""


GRANULARITIES = {
    'paragraph': 'Paragraph',
    'sentence':  'Sentence',
    'line':      'Line',
    'word':      'Word',
}

KIND_LABELS = {
    'unchanged': 'Unchanged',
    'added':     'Added',
    'deleted':   'Deleted',
    'modified':  'Modified',
}

MAX_UNITS = 60000          # hard safety cap per document
CHUNK_UNITS = 4000         # window size for the chunked diff path
LARGE_DOC_PAGES = 100

# Inside a difflib 'replace' block, two paired units are only called
# "modified" when they are at least this similar; otherwise they are reported
# as a separate deletion + addition (see _emit).
MODIFY_THRESHOLD = 0.40


# ---------------------------------------------------------------------------
# Tokenising into comparable units, keeping page attribution
# ---------------------------------------------------------------------------
def _norm_key(text: str, ignore_ws: bool) -> str:
    if ignore_ws:
        return ' '.join(text.split())
    return text


def tokenize(text_by_page: dict, granularity: str, ignore_ws: bool) -> list[dict]:
    """
    Turn {page: text} into an ordered list of units:
        {'text': <raw>, 'key': <comparison key>, 'page': <int>}
    """
    if granularity not in GRANULARITIES:
        raise CompareError(f'Unknown granularity "{granularity}".')

    units: list[dict] = []
    for page in sorted(text_by_page):
        raw = text_by_page[page] or ''
        if not raw.strip():
            continue

        if granularity == 'paragraph':
            parts = re.split(r'\n[ \t]*\n+', raw)
        elif granularity == 'line':
            parts = raw.split('\n')
        elif granularity == 'word':
            parts = re.findall(r'\S+', raw)
        else:  # sentence
            if split_sentences is None:
                parts = re.split(r'(?<=[.!?。！？])\s+', raw)
            else:
                lang = detect_language(raw) if detect_language else 'en'
                parts = split_sentences(raw, lang)

        for p in parts:
            if granularity == 'word':
                text = p
            else:
                text = p if not ignore_ws else p.strip()
            if not text:
                continue
            key = _norm_key(text, ignore_ws)
            if ignore_ws and not key:
                continue
            units.append({'text': text, 'key': key, 'page': page})
            if len(units) > MAX_UNITS:
                raise CompareError(
                    f'The selected pages produce more than {MAX_UNITS:,} comparison '
                    f'units. Please compare fewer pages or use a coarser granularity '
                    f'(paragraph instead of word).')
    return units


# ---------------------------------------------------------------------------
# Word-level sub-diff used inside "modified" pairs
# ---------------------------------------------------------------------------
def word_diff(a: str, b: str, ignore_ws: bool = True) -> list[dict]:
    """Return [{'op': 'equal'|'insert'|'delete', 'text': ...}] for two strings."""
    aw = re.findall(r'\s+|\S+', a or '')
    bw = re.findall(r'\s+|\S+', b or '')
    if ignore_ws:
        a_keys = [w if w.strip() else ' ' for w in aw]
        b_keys = [w if w.strip() else ' ' for w in bw]
    else:
        a_keys, b_keys = aw, bw

    sm = difflib.SequenceMatcher(None, a_keys, b_keys, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            out.append({'op': 'equal', 'text': ''.join(aw[i1:i2])})
        elif tag == 'delete':
            out.append({'op': 'delete', 'text': ''.join(aw[i1:i2])})
        elif tag == 'insert':
            out.append({'op': 'insert', 'text': ''.join(bw[j1:j2])})
        else:  # replace
            out.append({'op': 'delete', 'text': ''.join(aw[i1:i2])})
            out.append({'op': 'insert', 'text': ''.join(bw[j1:j2])})
    return [p for p in out if p['text'] != '']


# ---------------------------------------------------------------------------
# Core diff
# ---------------------------------------------------------------------------
def _emit(tag, a_units, b_units, ignore_ws) -> list[dict]:
    """Convert one difflib opcode block into UI-ready entries."""
    entries: list[dict] = []

    if tag == 'equal':
        for ua, ub in zip(a_units, b_units):
            entries.append({
                'kind': 'unchanged',
                'left_text': ua['text'],  'left_page': ua['page'],
                'right_text': ub['text'], 'right_page': ub['page'],
            })
        return entries

    if tag == 'delete':
        for ua in a_units:
            entries.append({
                'kind': 'deleted',
                'left_text': ua['text'], 'left_page': ua['page'],
                'right_text': None,      'right_page': None,
            })
        return entries

    if tag == 'insert':
        for ub in b_units:
            entries.append({
                'kind': 'added',
                'left_text': None,        'left_page': None,
                'right_text': ub['text'], 'right_page': ub['page'],
            })
        return entries

    # replace: pair up as "modified" only when the two units are actually
    # related. difflib merges an adjacent delete+insert into a single
    # 'replace', which would otherwise pair unrelated paragraphs together and
    # report them as a "modification". Below MODIFY_THRESHOLD similarity we
    # report them honestly as a separate deletion plus a separate addition.
    n = min(len(a_units), len(b_units))
    for i in range(n):
        ua, ub = a_units[i], b_units[i]
        ratio = difflib.SequenceMatcher(None, ua['key'], ub['key'],
                                        autojunk=False).ratio()
        if ratio >= MODIFY_THRESHOLD:
            entries.append({
                'kind': 'modified',
                'left_text': ua['text'],  'left_page': ua['page'],
                'right_text': ub['text'], 'right_page': ub['page'],
                'similarity': round(ratio * 100, 1),
                'words': word_diff(ua['text'], ub['text'], ignore_ws),
            })
        else:
            entries.append({
                'kind': 'deleted',
                'left_text': ua['text'], 'left_page': ua['page'],
                'right_text': None,      'right_page': None,
            })
            entries.append({
                'kind': 'added',
                'left_text': None,        'left_page': None,
                'right_text': ub['text'], 'right_page': ub['page'],
            })
    for ua in a_units[n:]:
        entries.append({
            'kind': 'deleted',
            'left_text': ua['text'], 'left_page': ua['page'],
            'right_text': None,      'right_page': None,
        })
    for ub in b_units[n:]:
        entries.append({
            'kind': 'added',
            'left_text': None,        'left_page': None,
            'right_text': ub['text'], 'right_page': ub['page'],
        })
    return entries


def diff_units(a_units: list[dict], b_units: list[dict], ignore_ws: bool,
               progress=None, cancelled=None) -> list[dict]:
    """Diff two unit lists, chunking when they are large."""
    a_keys = [u['key'] for u in a_units]
    b_keys = [u['key'] for u in b_units]

    if max(len(a_keys), len(b_keys)) <= CHUNK_UNITS:
        if progress:
            progress(1, 1, 'Comparing documents…')
        sm = difflib.SequenceMatcher(None, a_keys, b_keys, autojunk=False)
        out: list[dict] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if cancelled and cancelled():
                raise CompareError('Cancelled by user.')
            out.extend(_emit(tag, a_units[i1:i2], b_units[j1:j2], ignore_ws))
        return out

    # Large: walk sequential aligned windows so each SequenceMatcher stays small.
    out = []
    ai = bi = 0
    total_chunks = max(1, (max(len(a_keys), len(b_keys)) + CHUNK_UNITS - 1) // CHUNK_UNITS)
    chunk_no = 0
    while ai < len(a_keys) or bi < len(b_keys):
        if cancelled and cancelled():
            raise CompareError('Cancelled by user.')
        chunk_no += 1
        if progress:
            # total_chunks is an estimate; never show a ratio above 100 %.
            shown_total = max(total_chunks, chunk_no)
            progress(chunk_no, shown_total,
                     f'Comparing chunk {chunk_no}/{shown_total}…')
        a_end = min(len(a_keys), ai + CHUNK_UNITS)
        b_end = min(len(b_keys), bi + CHUNK_UNITS)
        sa, sb = a_units[ai:a_end], b_units[bi:b_end]
        sm = difflib.SequenceMatcher(None, [u['key'] for u in sa],
                                     [u['key'] for u in sb], autojunk=False)
        ops = sm.get_opcodes()

        # Consume everything except a trailing equal run, which we leave for the
        # next window so matches straddling the boundary still line up.
        if len(ops) > 1 and ops[-1][0] == 'equal' and a_end < len(a_keys):
            ops = ops[:-1]
            last = ops[-1]
            consumed_a, consumed_b = last[2], last[4]
        else:
            consumed_a, consumed_b = len(sa), len(sb)

        for tag, i1, i2, j1, j2 in ops:
            out.extend(_emit(tag, sa[i1:i2], sb[j1:j2], ignore_ws))

        if consumed_a == 0 and consumed_b == 0:      # safety against stalling
            consumed_a, consumed_b = len(sa), len(sb)
        ai += consumed_a
        bi += consumed_b
    return out


def summarize_stats(entries: list[dict]) -> dict:
    stats = {'unchanged': 0, 'added': 0, 'deleted': 0, 'modified': 0}
    for e in entries:
        stats[e['kind']] = stats.get(e['kind'], 0) + 1
    stats['total'] = len(entries)
    changed = stats['added'] + stats['deleted'] + stats['modified']
    stats['changed'] = changed
    stats['similarity'] = round(
        (stats['unchanged'] / stats['total'] * 100.0) if stats['total'] else 100.0, 1)
    return stats


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def build_report(entries: list[dict], stats: dict, meta: dict) -> str:
    lines = []
    lines.append('PDF COMPARISON REPORT')
    lines.append('=' * 64)
    lines.append(f'Document A      : {meta.get("name_a", "document A")}')
    lines.append(f'Pages compared A: {meta.get("pages_a", "")}')
    lines.append(f'Document B      : {meta.get("name_b", "document B")}')
    lines.append(f'Pages compared B: {meta.get("pages_b", "")}')
    lines.append(f'Granularity     : {GRANULARITIES.get(meta.get("granularity"), "?")}')
    lines.append(f'Whitespace      : {"included" if not meta.get("ignore_ws") else "ignored"}')
    if meta.get('ocr_a'):
        lines.append(f'OCR used on A   : {", ".join(str(p) for p in meta["ocr_a"])}')
    if meta.get('ocr_b'):
        lines.append(f'OCR used on B   : {", ".join(str(p) for p in meta["ocr_b"])}')
    lines.append('')
    lines.append(f'Similarity      : {stats["similarity"]}%')
    lines.append(f'Unchanged       : {stats["unchanged"]}')
    lines.append(f'Added           : {stats["added"]}')
    lines.append(f'Deleted         : {stats["deleted"]}')
    lines.append(f'Modified        : {stats["modified"]}')
    lines.append('')
    lines.append('-' * 64)
    lines.append('CHANGES (unchanged blocks omitted)')
    lines.append('-' * 64)
    lines.append('')

    any_change = False
    for e in entries:
        if e['kind'] == 'unchanged':
            continue
        any_change = True
        if e['kind'] == 'added':
            lines.append(f'[ADDED]     B page {e["right_page"]}')
            lines.append(f'  + {e["right_text"]}')
        elif e['kind'] == 'deleted':
            lines.append(f'[DELETED]   A page {e["left_page"]}')
            lines.append(f'  - {e["left_text"]}')
        else:
            lines.append(f'[MODIFIED]  A page {e["left_page"]}  ->  B page {e["right_page"]}')
            lines.append(f'  - {e["left_text"]}')
            lines.append(f'  + {e["right_text"]}')
        lines.append('')

    if not any_change:
        lines.append('(No differences found in the selected pages.)')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Job registry (shared implementation – see jobs.py)
# ---------------------------------------------------------------------------
_REGISTRY = jobs.JobRegistry({
    'entries': [],
    'stats': {},
    'meta': {},
    'report_name': None,
})


def new_job() -> str:
    return _REGISTRY.new()


def get_job(jid: str):
    return _REGISTRY.get(jid)


def cancel_job(jid: str) -> bool:
    return _REGISTRY.cancel(jid)


_patch = _REGISTRY.patch
_cancelled = _REGISTRY.is_cancelled

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def start_job(jid: str, path_a: str, pages_a: list[int], name_a: str,
              path_b: str, pages_b: list[int], name_b: str,
              granularity: str, ignore_ws: bool,
              generated_dir: str, ocr_lang: str = 'eng',
              cleanup_paths=None) -> None:
    t = threading.Thread(
        target=_worker,
        args=(jid, path_a, list(pages_a), name_a, path_b, list(pages_b), name_b,
              granularity, ignore_ws, generated_dir, ocr_lang,
              list(cleanup_paths or [])),
        daemon=True,
    )
    t.start()

def _worker(jid, path_a, pages_a, name_a, path_b, pages_b, name_b,
            granularity, ignore_ws, generated_dir, ocr_lang, cleanup_paths=None):
    try:
        if not _HAS_SUM:
            raise CompareError('Text extraction helpers are unavailable on the server.')
        if granularity not in GRANULARITIES:
            raise CompareError(f'Unknown granularity "{granularity}".')

        _patch(jid, status='running', progress=2, message='Reading document A…')

        def prog_a(i, total, msg):
            _patch(jid, progress=2 + int(28 * i / max(1, total)), message='A: ' + msg)

        text_a, ocr_a = extract_pages_text(path_a, pages_a, lang_for_ocr=ocr_lang,
                                           progress=prog_a,
                                           cancelled=lambda: _cancelled(jid))

        _patch(jid, progress=32, message='Reading document B…')

        def prog_b(i, total, msg):
            _patch(jid, progress=32 + int(28 * i / max(1, total)), message='B: ' + msg)

        text_b, ocr_b = extract_pages_text(path_b, pages_b, lang_for_ocr=ocr_lang,
                                           progress=prog_b,
                                           cancelled=lambda: _cancelled(jid))

        a_chars = sum(1 for t in text_a.values() for c in t if not c.isspace())
        b_chars = sum(1 for t in text_b.values() for c in t if not c.isspace())
        if a_chars == 0 and b_chars == 0:
            raise CompareError(
                'Neither document produced readable text on the selected pages. '
                'If they are scans, install Tesseract so OCR can run, or run the '
                'OCR PDF tool first.')

        _patch(jid, progress=62, message='Splitting text into comparison units…')
        units_a = tokenize(text_a, granularity, ignore_ws)
        units_b = tokenize(text_b, granularity, ignore_ws)

        if not units_a and not units_b:
            raise CompareError('No comparable text units were found on the selected pages.')

        def prog_d(i, total, msg):
            _patch(jid, progress=66 + int(26 * i / max(1, total)), message=msg)

        entries = diff_units(units_a, units_b, ignore_ws,
                             progress=prog_d, cancelled=lambda: _cancelled(jid))
        stats = summarize_stats(entries)

        meta = {
            'name_a': name_a, 'name_b': name_b,
            'pages_a': ', '.join(str(p) for p in pages_a),
            'pages_b': ', '.join(str(p) for p in pages_b),
            'granularity': granularity,
            'ignore_ws': ignore_ws,
            'ocr_a': ocr_a, 'ocr_b': ocr_b,
            'units_a': len(units_a), 'units_b': len(units_b),
        }

        _patch(jid, progress=94, message='Writing comparison report…')
        report = build_report(entries, stats, meta)
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_',
                      os.path.splitext(name_a or 'a')[0])[:24] or 'compare'
        report_name = f'compare_{jid}_{safe}.txt'
        os.makedirs(generated_dir, exist_ok=True)
        with open(os.path.join(generated_dir, report_name), 'w', encoding='utf-8') as fh:
            fh.write(report)

        _patch(jid, status='done', progress=100, entries=entries, stats=stats,
               meta=meta, report_name=report_name, finished_at=time.time(),
               message=(f'Comparison complete: {stats["changed"]} change(s), '
                        f'{stats["similarity"]}% similar.'))
    except CompareError as e:
        _REGISTRY.fail(jid, str(e), 'Cancelled. No comparison was produced.')
    except Exception as e:                           # pragma: no cover
        _REGISTRY.fail(jid, f'Unexpected error: {e}',
                       'Cancelled. No comparison was produced.')
    finally:
        jobs.discard(cleanup_paths)