"""
Sign PDF – stamp raster signatures (with transparency) onto PDF pages.

Approach
--------
For every page that receives at least one placement:
  1. Create a transparent-background overlay PDF of matching page size with
     reportlab, placing each PNG image at the correct rect/rotation.
  2. Merge that overlay on top of the original page with PyPDF2's
     ``page.merge_page``.
Pages without placements are passed through untouched. This keeps the original
vector content lossless.

A placement is:
    {
      'page': 1-based int,
      'rect': [x1, y1, x2, y2] in PDF user-space points (origin bottom-left),
      'rotation': degrees (counter-clockwise),
      'image': <key into images dict>,
    }
and ``images`` is ``{key: <PNG bytes>}``.
"""

from __future__ import annotations

import io
import math
import os

from PyPDF2 import PdfReader, PdfWriter
# reportlab is only needed when a signature is actually stamped; importing it
# lazily keeps application start-up fast.


class SignError(Exception):
    """User-facing signing failure."""


MAX_PLACEMENTS = 200
MAX_IMAGE_BYTES = 6 * 1024 * 1024     # 6 MB per signature image
MIN_PDF_POINTS = 6                    # min width/height of a placement


def _validate(placements, images, total_pages):
    if not isinstance(placements, list) or not placements:
        raise SignError('Please add at least one signature before saving.')
    if len(placements) > MAX_PLACEMENTS:
        raise SignError(f'Too many placements (limit {MAX_PLACEMENTS}).')
    if not isinstance(images, dict) or not images:
        raise SignError('No signature image was supplied.')

    cleaned = []
    for idx, p in enumerate(placements, start=1):
        if not isinstance(p, dict):
            raise SignError(f'Placement {idx}: invalid entry.')
        try:
            pn = int(p.get('page', 0))
        except (TypeError, ValueError):
            raise SignError(f'Placement {idx}: page must be an integer.')
        if not (1 <= pn <= total_pages):
            raise SignError(
                f'Placement {idx}: page {pn} is out of range (document has {total_pages}).')

        rect = p.get('rect')
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise SignError(f'Placement {idx}: rect must be [x1, y1, x2, y2].')
        try:
            x1, y1, x2, y2 = (float(v) for v in rect)
        except (TypeError, ValueError):
            raise SignError(f'Placement {idx}: rect values must be numbers.')
        if abs(x2 - x1) < MIN_PDF_POINTS or abs(y2 - y1) < MIN_PDF_POINTS:
            raise SignError(f'Placement {idx}: signature area is too small.')

        try:
            rot = float(p.get('rotation', 0) or 0)
        except (TypeError, ValueError):
            raise SignError(f'Placement {idx}: rotation must be a number.')
        if not math.isfinite(rot):
            raise SignError(f'Placement {idx}: rotation must be a finite number.')
        rot = max(-359.999, min(359.999, rot))

        key = str(p.get('image') or '').strip()
        if not key or key not in images:
            raise SignError(f'Placement {idx}: signature image is missing on the server.')

        cleaned.append({
            'page': pn,
            'rect': [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)],
            'rotation': rot,
            'image': key,
        })

    for key, data in images.items():
        if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            raise SignError(f'Signature image "{key}" is empty.')
        if len(data) > MAX_IMAGE_BYTES:
            raise SignError(
                f'Signature image "{key}" is larger than '
                f'{MAX_IMAGE_BYTES // (1024 * 1024)} MB.')

    return cleaned


def sign_pdf(src_pdf, placements, images, output_path):
    """
    Write ``src_pdf`` to ``output_path`` with raster signatures overlaid.

    Returns ``{'placements': N, 'pages': total, 'signed_pages': [1,3,...]}``.
    """
    try:
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas as rl_canvas
    except Exception as e:                            # pragma: no cover
        raise SignError(f'reportlab is required to stamp signatures: {e}')

    reader = PdfReader(src_pdf)
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise SignError('The PDF has no pages.')

    placements = _validate(placements, images, total_pages)

    image_readers = {}
    for key, data in images.items():
        try:
            image_readers[key] = ImageReader(io.BytesIO(data))
        except Exception as e:
            raise SignError(f'Could not read signature image "{key}": {e}')

    by_page: dict[int, list] = {}
    for p in placements:
        by_page.setdefault(p['page'], []).append(p)

    writer = PdfWriter()
    for i, page in enumerate(reader.pages, start=1):
        placements_here = by_page.get(i)
        if not placements_here:
            writer.add_page(page)
            continue

        mb = page.mediabox
        W = float(mb.right) - float(mb.left)
        H = float(mb.top) - float(mb.bottom)

        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(W, H))
        # Translate so our (0,0) matches the page's (left, bottom).
        c.translate(-float(mb.left), -float(mb.bottom))

        for p in placements_here:
            x1, y1, x2, y2 = p['rect']
            w, h = x2 - x1, y2 - y1
            cx, cy = x1 + w / 2.0, y1 + h / 2.0
            c.saveState()
            c.translate(cx, cy)
            if p['rotation']:
                c.rotate(p['rotation'])
            try:
                c.drawImage(image_readers[p['image']], -w / 2.0, -h / 2.0,
                            width=w, height=h, mask='auto',
                            preserveAspectRatio=False)
            except Exception as e:
                c.restoreState()
                raise SignError(f'Could not draw signature on page {i}: {e}')
            c.restoreState()
        c.showPage()
        c.save()

        overlay = PdfReader(io.BytesIO(buf.getvalue()))
        try:
            page.merge_page(overlay.pages[0])
        except Exception as e:
            raise SignError(f'Could not merge the signature overlay on page {i}: {e}')
        writer.add_page(page)

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return {
        'placements': len(placements),
        'pages': total_pages,
        'signed_pages': sorted(by_page.keys()),
    }