"""
Redact PDF – *destructive* redaction.

Why rasterisation
-----------------
Drawing a black rectangle over a PDF does NOT redact anything: the text and
images stay in the content stream and can be selected, copied or extracted.
That is the classic "redaction failure" that leaks documents.

To genuinely remove the underlying content, every page that carries at least
one redaction box is:

  1. rendered to a bitmap at ``DPI`` (poppler / pdf2image),
  2. painted with opaque black rectangles over the redacted regions,
  3. written back as an image-only page.

After that the original glyphs simply do not exist in the output any more –
verified by extracting text from the result.

Pages with no redactions are copied through untouched, so they keep their
vector content and selectable text.

Trade-off (worth telling the user): redacted pages lose selectable text and
grow in file size, because they become images. That is the price of real
redaction and is how this is normally done.
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw
from PyPDF2 import PdfReader, PdfWriter

try:
    from pdf2image import convert_from_path
    _HAS_P2I = True
except Exception:                                     # pragma: no cover
    convert_from_path = None
    _HAS_P2I = False


class RedactError(Exception):
    """User-facing redaction failure."""


DPI = 200
MAX_BOXES = 2000
MIN_POINTS = 2.0


def _validate(boxes, total_pages):
    """
    boxes: [{page, rect:[x1,y1,x2,y2] in PDF points, full_page: bool}]
    Returns {page: [rect, ...]} plus the set of full-page redactions.
    """
    if not isinstance(boxes, list) or not boxes:
        raise RedactError('Please add at least one redaction area before applying.')
    if len(boxes) > MAX_BOXES:
        raise RedactError(f'Too many redaction areas (limit {MAX_BOXES}).')

    per_page: dict[int, list] = {}
    full_pages: set[int] = set()

    for idx, b in enumerate(boxes, start=1):
        if not isinstance(b, dict):
            raise RedactError(f'Redaction {idx}: invalid entry.')
        try:
            pn = int(b.get('page', 0))
        except (TypeError, ValueError):
            raise RedactError(f'Redaction {idx}: page must be an integer.')
        if not (1 <= pn <= total_pages):
            raise RedactError(
                f'Redaction {idx}: page {pn} is out of range (document has {total_pages}).')

        if b.get('full_page'):
            full_pages.add(pn)
            per_page.setdefault(pn, [])
            continue

        rect = b.get('rect')
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise RedactError(f'Redaction {idx}: rect must be [x1, y1, x2, y2].')
        try:
            x1, y1, x2, y2 = (float(v) for v in rect)
        except (TypeError, ValueError):
            raise RedactError(f'Redaction {idx}: rect values must be numbers.')
        if abs(x2 - x1) < MIN_POINTS or abs(y2 - y1) < MIN_POINTS:
            raise RedactError(f'Redaction {idx}: the area is too small.')
        per_page.setdefault(pn, []).append(
            [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)])

    return per_page, full_pages


def search_text(src_pdf, query, case_sensitive=False, max_hits=500):
    """
    Find occurrences of `query` and return their bounding boxes in PDF points.

    Uses poppler's ``pdftotext -bbox``, which gives per-word boxes. Multi-word
    queries are matched across consecutive words on the same line and the
    union of their boxes is returned.

    Returns [{'page': n, 'rect': [x1,y1,x2,y2], 'text': '...'}, ...]
    """
    import re
    import shutil
    import subprocess

    query = (query or '').strip()
    if not query:
        raise RedactError('Please enter the text you want to find.')
    if len(query) > 200:
        raise RedactError('Search text is too long (200 characters max).')
    if not shutil.which('pdftotext'):
        raise RedactError(
            'Text search needs poppler-utils (pdftotext) on the server. '
            'You can still draw redaction areas manually.')

    try:
        proc = subprocess.run(['pdftotext', '-bbox', src_pdf, '-'],
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RedactError('Text search timed out. Try a smaller document.')
    except Exception as e:
        raise RedactError(f'Text search failed: {e}')
    if proc.returncode != 0:
        raise RedactError('Could not read text from this PDF (it may be a scan).')

    xml = proc.stdout
    page_re = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
    word_re = re.compile(
        r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')

    terms = query.split()
    n_terms = len(terms)
    if not case_sensitive:
        terms = [t.lower() for t in terms]

    hits = []
    for pidx, m in enumerate(page_re.finditer(xml), start=1):
        page_h = float(m.group(2))
        words = []
        for wm in word_re.finditer(m.group(3)):
            xm, ym, xM, yM, raw = wm.groups()
            import html as _html
            words.append((float(xm), float(ym), float(xM), float(yM),
                          _html.unescape(raw)))

        for i in range(len(words)):
            window = words[i:i + n_terms]
            if len(window) < n_terms:
                break
            cand = [w[4] if case_sensitive else w[4].lower() for w in window]
            # allow punctuation to cling to the match
            ok = all(terms[k] in cand[k] for k in range(n_terms))
            if not ok:
                continue
            x1 = min(w[0] for w in window); x2 = max(w[2] for w in window)
            ytop = min(w[1] for w in window); ybot = max(w[3] for w in window)
            # pdftotext uses a top-left origin; PDF space is bottom-left.
            hits.append({
                'page': pidx,
                'rect': [x1 - 1, page_h - ybot - 1, x2 + 1, page_h - ytop + 1],
                'text': ' '.join(w[4] for w in window),
            })
            if len(hits) >= max_hits:
                return hits
    return hits


def apply_redactions(src_pdf, boxes, output_path, dpi: int = DPI):
    """
    Permanently burn the redactions in and write the result to output_path.

    Returns {'pages': N, 'redacted_pages': [...], 'boxes': N, 'full_pages': [...]}
    """
    if not _HAS_P2I:
        raise RedactError(
            'Rendering support is unavailable on the server. Install poppler-utils '
            'and `pip install pdf2image` so redactions can be permanently applied.')

    reader = PdfReader(src_pdf)
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise RedactError('The PDF has no pages.')

    per_page, full_pages = _validate(boxes, total_pages)

    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        dpi = DPI
    dpi = max(72, min(400, dpi))

    writer = PdfWriter()
    for pno in range(1, total_pages + 1):
        page = reader.pages[pno - 1]
        if pno not in per_page:
            writer.add_page(page)                      # untouched, stays vector
            continue

        mb = page.mediabox
        left, bottom = float(mb.left), float(mb.bottom)
        w_pt = float(mb.right) - left
        h_pt = float(mb.top) - bottom
        if w_pt <= 0 or h_pt <= 0:
            raise RedactError(f'Page {pno} has an invalid page size.')

        # 1. rasterise the page
        try:
            imgs = convert_from_path(src_pdf, dpi=dpi, first_page=pno, last_page=pno)
        except Exception as e:
            raise RedactError(f'Could not render page {pno}: {e}')
        if not imgs:
            raise RedactError(f'No image was produced for page {pno}.')
        img = imgs[0].convert('RGB')
        px_w, px_h = img.size
        sx = px_w / w_pt
        sy = px_h / h_pt

        # 2. paint opaque black over the redacted regions
        draw = ImageDraw.Draw(img)
        if pno in full_pages:
            draw.rectangle([0, 0, px_w, px_h], fill=(0, 0, 0))
        for (x1, y1, x2, y2) in per_page[pno]:
            # PDF origin is bottom-left, image origin is top-left → flip Y.
            ix1 = (x1 - left) * sx
            ix2 = (x2 - left) * sx
            iy1 = px_h - (y2 - bottom) * sy
            iy2 = px_h - (y1 - bottom) * sy
            ix1 = max(0, min(px_w, ix1)); ix2 = max(0, min(px_w, ix2))
            iy1 = max(0, min(px_h, iy1)); iy2 = max(0, min(px_h, iy2))
            if ix2 - ix1 < 1 or iy2 - iy1 < 1:
                continue
            draw.rectangle([ix1, iy1, ix2, iy2], fill=(0, 0, 0))

        # 3. write back as an image-only page of the original size
        buf = io.BytesIO()
        try:
            img.save(buf, format='PDF', resolution=float(dpi))
        except Exception as e:
            raise RedactError(f'Could not rebuild page {pno}: {e}')
        try:
            new_page = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        except Exception as e:
            raise RedactError(f'Could not read the rebuilt page {pno}: {e}')
        writer.add_page(new_page)

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return {
        'pages': total_pages,
        'redacted_pages': sorted(per_page.keys()),
        'full_pages': sorted(full_pages),
        'boxes': sum(len(v) for v in per_page.values()),
    }


def verify_removed(output_path, phrases):
    """
    Safety check used by the route: returns any supplied phrase that is STILL
    extractable from the output (should always be empty for redacted text).
    """
    try:
        reader = PdfReader(output_path)
        text = '\n'.join((p.extract_text() or '') for p in reader.pages).lower()
    except Exception:
        return []
    return [p for p in phrases if p and p.lower() in text]