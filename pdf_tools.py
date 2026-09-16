import os, zipfile
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import RectangleObject
def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
def merge(files, output_path='output.pdf'):
    pdfWriter = PdfWriter()
    for pdf in files:
        pdfReader = PdfReader(pdf)
        for page in pdfReader.pages:
            pdfWriter.add_page(page)
    with open(output_path, 'wb') as pdfOutput:
        pdfWriter.write(pdfOutput)
def split_segments(pdf, segments, out_dir, base_name='segment'):
    """
    Split PDF according to list of segments.
    Each segment is a dict {'start': int, 'end': int} (1-indexed, inclusive).
    Returns list of dicts: [{'name': 'segment_1_p1-5.pdf', 'path': '...', 'start': 1, 'end': 5}, ...]
    Raises ValueError on invalid input.
    """
    _ensure_dir(out_dir)
    pdfReader = PdfReader(pdf)
    numPages = len(pdfReader.pages)
    if not segments:
        raise ValueError('No segments provided. Please add at least one segment.')
    results = []
    for idx, seg in enumerate(segments, start=1):
        start = seg.get('start')
        end = seg.get('end')
        if start is None or end is None or start == '' or end == '':
            raise ValueError(f'Segment {idx}: both start and end pages are required.')
        try:
            start = int(start); end = int(end)
        except (TypeError, ValueError):
            raise ValueError(f'Segment {idx}: start and end must be integers.')
        if start < 1 or end < 1:
            raise ValueError(f'Segment {idx}: page numbers must be >= 1.')
        if start > end:
            raise ValueError(f'Segment {idx}: start page ({start}) cannot be greater than end page ({end}).')
        if start > numPages or end > numPages:
            raise ValueError(f'Segment {idx}: page out of range. PDF has {numPages} pages.')
        writer = PdfWriter()
        for pageNo in range(start - 1, end):
            writer.add_page(pdfReader.pages[pageNo])
        name = f'{base_name}_{idx}_p{start}-{end}.pdf'
        out_path = os.path.join(out_dir, name)
        with open(out_path, 'wb') as f:
            writer.write(f)
        results.append({'name': name, 'path': out_path, 'start': start, 'end': end})
    return results
def make_zip(files, zip_path):
    """Create a zip archive containing the given files (each: {'name', 'path'})."""
    _ensure_dir(os.path.dirname(zip_path) or '.')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f['path'], arcname=f['name'])
    return zip_path
# --- Legacy helpers kept so older routes keep working ---------------------
def split(pdf, rangeList, pageList):
    try:
        os.remove('new.zip')
    except FileNotFoundError:
        pass
    _ensure_dir('temp')
    pdfReader = PdfReader(pdf)
    numPages = len(pdfReader.pages)
    downloadZip = zipfile.ZipFile('new.zip', 'a')
    added = False
    for lst in rangeList:
        pdfWriter = PdfWriter()
        if lst[0] <= numPages and lst[1] <= numPages:
            added = True
            for pageNo in range(lst[0] - 1, lst[1]):
                pdfWriter.add_page(pdfReader.pages[pageNo])
            out_name = 'temp/output' + str(lst[0]) + str(lst[1]) + '.pdf'
            with open(out_name, 'wb') as pdfOutput:
                pdfWriter.write(pdfOutput)
            downloadZip.write(out_name, compress_type=zipfile.ZIP_DEFLATED)
    for page in pageList:
        pdfWriter = PdfWriter()
        if page <= numPages:
            added = True
            pdfWriter.add_page(pdfReader.pages[page - 1])
            out_name = 'temp/output' + str(page) + '.pdf'
            with open(out_name, 'wb') as pdfOutput:
                pdfWriter.write(pdfOutput)
            downloadZip.write(out_name, compress_type=zipfile.ZIP_DEFLATED)
    downloadZip.close()
    for leftover in os.listdir('temp'):
        os.remove('temp/' + leftover)
    return added
def remove(pdf, matches, output_path='output.pdf'):
    """
    Remove the given 1-indexed pages from the PDF.
    `matches` is a sorted list of unique page numbers.
    Returns dict {'removed': [...], 'ignored': [...], 'total_pages': N}.
    Raises ValueError when nothing valid can be removed.
    """
    pdfReader = PdfReader(pdf)
    numPages = len(pdfReader.pages)
    valid   = [m for m in matches if 1 <= m <= numPages]
    invalid = [m for m in matches if not (1 <= m <= numPages)]
    if not valid:
        raise ValueError(f'No valid pages specified. PDF has {numPages} pages.')
    if len(valid) >= numPages:
        raise ValueError('Cannot remove every page of the PDF.')
    pdfWriter = PdfWriter()
    valid_set = set(valid)
    for pageNo in range(numPages):
        if (pageNo + 1) in valid_set:
            continue
        pdfWriter.add_page(pdfReader.pages[pageNo])
    with open(output_path, 'wb') as pdfOutput:
        pdfWriter.write(pdfOutput)
    return {'removed': sorted(valid), 'ignored': sorted(invalid), 'total_pages': numPages}
def rotate(pdf, degree, output_path='output.pdf'):
    """Rotate every page by `degree`. PyPDF2 requires a multiple of 90."""
    try:
        degree = int(degree)
    except (TypeError, ValueError):
        raise ValueError('Rotation degree must be an integer.')
    if degree % 90 != 0:
        raise ValueError('Rotation degree must be a multiple of 90 (e.g. 90, 180, 270, -90).')
    pdfReader = PdfReader(pdf)
    pdfWriter = PdfWriter()
    for page in pdfReader.pages:
        page.rotate(degree)
        pdfWriter.add_page(page)
    with open(output_path, 'wb') as pdfOutput:
        pdfWriter.write(pdfOutput)
def watermark(pdf, watermark_file, typ, output_path='output.pdf'):
    if typ not in ('first', 'all'):
        raise ValueError("Watermark target must be 'first' or 'all'.")
    pdfReader = PdfReader(pdf)
    watermarkReader = PdfReader(watermark_file)
    if len(watermarkReader.pages) == 0:
        raise ValueError('Watermark PDF has no pages.')
    pdfWriter = PdfWriter()
    pageObj = watermarkReader.pages[0]
    numPages = len(pdfReader.pages)
    if typ == 'first':
        page = pdfReader.pages[0]
        page.merge_page(pageObj)
        pdfWriter.add_page(page)
        for pageNo in range(1, numPages):
            pdfWriter.add_page(pdfReader.pages[pageNo])
    else:  # 'all'
        for pageNo in range(numPages):
            page = pdfReader.pages[pageNo]
            page.merge_page(pageObj)
            pdfWriter.add_page(page)
    with open(output_path, 'wb') as pdfOutput:
        pdfWriter.write(pdfOutput)
def encrypt(pdf, password, output_path='output.pdf'):
    if not password:
        raise ValueError('Password cannot be empty.')
    pdfReader = PdfReader(pdf)
    pdfWriter = PdfWriter()
    for page in pdfReader.pages:
        pdfWriter.add_page(page)
    pdfWriter.encrypt(password)
    with open(output_path, 'wb') as pdfOutput:
        pdfWriter.write(pdfOutput)
def crop_pdf(src, mode, rects, output_path='output.pdf'):
    """
    Crop the PDF's pages by setting each page's CropBox.

    Parameters
    ----------
    src : path or file-like object
        The source PDF.
    mode : str
        'all'      – apply the same rectangle to every page
        'per_page' – apply different rectangles to individual pages; pages
                     not listed are left uncropped.
    rects : list | dict
        If `mode == 'all'` : a single [x1, y1, x2, y2] in PDF user-space points
                             (origin bottom-left, inclusive).
        If `mode == 'per_page'` : {page_number_1based: [x1, y1, x2, y2], ...}
    output_path : str
        Destination file path.

    Returns
    -------
    dict : {'pages_cropped': [..], 'total_pages': N}
    Raises ValueError on invalid input.
    """
    reader = PdfReader(src)
    num_pages = len(reader.pages)
    if num_pages == 0:
        raise ValueError('The PDF has no pages.')

    def _validate_rect(r, label):
        if not isinstance(r, (list, tuple)) or len(r) != 4:
            raise ValueError(f'{label}: rectangle must be [x1, y1, x2, y2].')
        try:
            x1, y1, x2, y2 = (float(v) for v in r)
        except (TypeError, ValueError):
            raise ValueError(f'{label}: rectangle values must be numbers.')
        # Accept corners in any order – a drag from bottom-right to top-left is
        # unambiguous, and sign/redact already normalise this way. Only a
        # genuinely empty rectangle is an error.
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        if x1 == x2 or y1 == y2:
            raise ValueError(f'{label}: rectangle has zero area.')
        return [x1, y1, x2, y2]

    per_page = {}
    if mode == 'all':
        rect = _validate_rect(rects, 'Crop area')
        for p in range(1, num_pages + 1):
            per_page[p] = list(rect)
    elif mode == 'per_page':
        if not isinstance(rects, dict) or not rects:
            raise ValueError('No page selections provided. Draw a rectangle on at least one page.')
        for k, v in rects.items():
            try:
                pn = int(k)
            except (TypeError, ValueError):
                raise ValueError(f'Invalid page number "{k}".')
            if not (1 <= pn <= num_pages):
                raise ValueError(f'Page {pn} is out of range (PDF has {num_pages} pages).')
            per_page[pn] = _validate_rect(v, f'Page {pn}')
    else:
        raise ValueError("Crop mode must be 'all' or 'per_page'.")

    writer = PdfWriter()
    for idx, page in enumerate(reader.pages, start=1):
        if idx in per_page:
            x1, y1, x2, y2 = per_page[idx]
            mb = page.mediabox
            mb_x1, mb_y1 = float(mb.left), float(mb.bottom)
            mb_x2, mb_y2 = float(mb.right), float(mb.top)
            cx1 = max(mb_x1, min(mb_x2, x1))
            cx2 = max(mb_x1, min(mb_x2, x2))
            cy1 = max(mb_y1, min(mb_y2, y1))
            cy2 = max(mb_y1, min(mb_y2, y2))
            if cx1 >= cx2 or cy1 >= cy2:
                raise ValueError(
                    f'Page {idx}: the crop area falls entirely outside the page.')
            page.cropbox = RectangleObject([cx1, cy1, cx2, cy2])
        writer.add_page(page)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return {'pages_cropped': sorted(per_page.keys()),
            'total_pages': num_pages}
def organize_pdf(plan, sources, output_path='output.pdf'):
    """
    Build a new PDF by following an ordered "page plan".

    This is lossless – pages are copied from their source documents, never
    rasterised – so it handles reorder / delete / duplicate / rotate / insert
    in a single pass.

    Parameters
    ----------
    plan : list of dict
        Ordered page descriptors. Each item is either:
          {'src': <source key>, 'page': <1-based int>, 'rotate': 0|90|180|270}
        or a blank page:
          {'src': 'blank', 'width': <points>, 'height': <points>}
    sources : dict
        Maps a source key (e.g. 'main', 'insert_0') to a PDF path / file object.
    output_path : str
        Destination path.

    Returns
    -------
    dict : {'pages': <page count of result>}
    Raises ValueError on invalid input.
    """
    if not isinstance(plan, list) or len(plan) == 0:
        raise ValueError('The result would have no pages. Please keep at least one page.')
    if len(plan) > 2000:
        raise ValueError('Too many pages in the result (limit is 2000).')

    readers = {}
    for key, path in (sources or {}).items():
        try:
            readers[key] = PdfReader(path)
        except Exception as e:
            raise ValueError(f'Could not read source "{key}": {e}')

    writer = PdfWriter()
    for idx, item in enumerate(plan, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'Item {idx}: invalid entry.')
        src = str(item.get('src', ''))

        # ---- blank page ------------------------------------------------
        if src == 'blank':
            try:
                wpt = float(item.get('width', 612))
                hpt = float(item.get('height', 792))
            except (TypeError, ValueError):
                raise ValueError(f'Item {idx}: blank page size must be numeric.')
            if not (1 <= wpt <= 20000) or not (1 <= hpt <= 20000):
                raise ValueError(f'Item {idx}: blank page size is out of range.')
            writer.add_blank_page(width=wpt, height=hpt)
            continue

        # ---- page copied from a source --------------------------------
        if src not in readers:
            raise ValueError(f'Item {idx}: unknown page source "{src}".')
        reader = readers[src]
        try:
            pnum = int(item.get('page', 0))
        except (TypeError, ValueError):
            raise ValueError(f'Item {idx}: page number must be an integer.')
        if not (1 <= pnum <= len(reader.pages)):
            raise ValueError(
                f'Item {idx}: page {pnum} is out of range for "{src}" '
                f'(it has {len(reader.pages)} page(s)).')

        try:
            rot = int(item.get('rotate', 0))
        except (TypeError, ValueError):
            raise ValueError(f'Item {idx}: rotation must be an integer.')
        if rot % 90 != 0:
            raise ValueError(f'Item {idx}: rotation must be a multiple of 90.')
        rot %= 360

        # add_page() clones, so duplicated pages rotate independently.
        writer.add_page(reader.pages[pnum - 1])
        if rot:
            writer.pages[-1].rotate(rot)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return {'pages': len(writer.pages)}