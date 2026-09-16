"""
PDF compression helpers.
Strategy
--------
1. Lossless pass: re-compress all page content streams (Flate). Always safe.
2. Lossy passes: re-encode embedded raster images as JPEG, optionally
   downscaling them. A ladder of (scale, quality) settings goes from mild to
   aggressive; a binary search over that ladder finds the *mildest* setting
   that still meets the requested target size, so we never degrade the file
   more than necessary.
If the target cannot be reached (e.g. a text-only PDF has a hard size floor),
the best result achieved is returned together with `reached=False` so the
caller can tell the user honestly instead of silently failing.
Pillow is optional: without it only the lossless pass runs.
"""
import io
import os
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import NameObject, NumberObject
try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except Exception:                                    # pragma: no cover
    Image = None
    PILLOW_AVAILABLE = False
# (scale, jpeg_quality) – mildest first, most aggressive last.
QUALITY_LADDER = [
    (1.00, 90), (1.00, 80), (1.00, 70), (1.00, 60),
    (0.90, 55), (0.80, 50), (0.75, 45), (0.65, 40),
    (0.55, 35), (0.50, 30), (0.40, 25), (0.35, 20),
    (0.25, 15), (0.20, 10),
]
# Images smaller than this (in pixels) are not worth touching.
MIN_PIXELS = 4096
SKIP_FILTERS = {'/CCITTFaxDecode', '/JBIG2Decode'}
class CompressionError(Exception):
    """Raised when the PDF cannot be processed at all."""
# ---------------------------------------------------------------------------
# Size helpers
# ---------------------------------------------------------------------------
def human_size(num_bytes):
    """Format a byte count for display, e.g. 1.4 MB."""
    try:
        num_bytes = float(num_bytes)
    except (TypeError, ValueError):
        return '0 B'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num_bytes < 1024 or unit == 'GB':
            if unit == 'B':
                return '%d %s' % (int(num_bytes), unit)
            return '%.2f %s' % (num_bytes, unit)
        num_bytes /= 1024.0
    return '%.2f GB' % num_bytes
def parse_target_size(value, unit):
    """
    Turn a user-supplied amount + unit into a byte count.
    Raises ValueError with a friendly message on bad input.
    """
    if value is None or str(value).strip() == '':
        raise ValueError('Please enter a target file size.')
    try:
        amount = float(str(value).strip())
    except ValueError:
        raise ValueError('Target file size must be a number (e.g. 500 or 1.5).')
    if amount <= 0:
        raise ValueError('Target file size must be greater than zero.')
    unit = (unit or 'KB').strip().upper()
    multipliers = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2}
    if unit not in multipliers:
        raise ValueError('Target size unit must be B, KB or MB.')
    target = int(amount * multipliers[unit])
    if target < 1024:
        raise ValueError('Target file size is unrealistically small; use at least 1 KB.')
    return target
# ---------------------------------------------------------------------------
# Image decoding / re-encoding
# ---------------------------------------------------------------------------
def _filters_of(obj):
    flt = obj.get('/Filter')
    if flt is None:
        return []
    if isinstance(flt, list):
        return [str(f) for f in flt]
    return [str(flt)]
def _colorspace_mode(obj):
    """Best-effort guess of a PIL mode + bytes-per-pixel for raw (Flate) data."""
    cs = obj.get('/ColorSpace')
    cs_name = str(cs) if cs is not None else ''
    if cs_name in ('/DeviceRGB', '/CalRGB'):
        return 'RGB', 3
    if cs_name in ('/DeviceGray', '/CalGray'):
        return 'L', 1
    if cs_name == '/DeviceCMYK':
        return 'CMYK', 4
    # ICCBased streams carry the component count in /N
    try:
        if isinstance(cs, list) and len(cs) >= 2 and str(cs[0]) == '/ICCBased':
            n = int(cs[1].get_object().get('/N', 3))
            return {1: ('L', 1), 3: ('RGB', 3), 4: ('CMYK', 4)}.get(n, ('RGB', 3))
    except Exception:
        pass
    return None, 0
def _decode_image(obj):
    """
    Return a PIL Image for an /Image XObject, or None if we can't / shouldn't
    handle it.
    """
    if not PILLOW_AVAILABLE:
        return None
    if obj.get('/ImageMask', False):
        return None                                  # 1-bit stencil mask
    filters = _filters_of(obj)
    if any(f in SKIP_FILTERS for f in filters):
        return None
    try:
        width = int(obj.get('/Width', 0))
        height = int(obj.get('/Height', 0))
    except Exception:
        return None
    if width * height < MIN_PIXELS:
        return None
    # 1) Already a JPEG / JPEG2000 payload – hand the bytes straight to Pillow.
    if '/DCTDecode' in filters or '/JPXDecode' in filters:
        try:
            img = Image.open(io.BytesIO(obj._data))
            img.load()
            return img
        except Exception:
            pass
    # 2) Flate/LZW raw samples – rebuild from the decoded bytes.
    if '/FlateDecode' in filters or '/LZWDecode' in filters or not filters:
        try:
            bits = int(obj.get('/BitsPerComponent', 8))
            mode, bpp = _colorspace_mode(obj)
            if mode and bits == 8:
                raw = obj.get_data()
                expected = width * height * bpp
                if len(raw) >= expected:
                    img = Image.frombytes(mode, (width, height), raw[:expected])
                    return img
        except Exception:
            pass
    # 3) Last resort: PyPDF2's own converter (signature varies across versions).
    try:
        from PyPDF2.filters import _xobj_to_image
        result = _xobj_to_image(obj)
        data = None
        if isinstance(result, tuple):
            if len(result) == 3 and result[2] is not None:
                return result[2]                     # newer pypdf returns a PIL image
            if len(result) >= 2:
                data = result[1]
        if data:
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
    except Exception:
        pass
    return None
def _write_jpeg_into(obj, pil_img, scale, quality):
    """
    Re-encode `pil_img` as JPEG and overwrite the XObject's stream in place.
    Returns the number of bytes written, or None when it isn't worth it.
    """
    original_len = len(obj._data or b'')
    width, height = pil_img.size
    if scale < 1.0:
        width = max(1, int(pil_img.width * scale))
        height = max(1, int(pil_img.height * scale))
        try:
            resample = Image.LANCZOS
        except AttributeError:                       # very old Pillow
            resample = Image.BICUBIC
        pil_img = pil_img.resize((width, height), resample)
    # JPEG supports L / RGB / CMYK only.
    if pil_img.mode in ('1', 'L', 'I;16', 'I'):
        pil_img = pil_img.convert('L')
        colorspace = '/DeviceGray'
    elif pil_img.mode == 'CMYK':
        colorspace = '/DeviceCMYK'
    else:
        if pil_img.mode in ('RGBA', 'LA', 'P'):
            pil_img = pil_img.convert('RGB')
        elif pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')
        colorspace = '/DeviceRGB'
    buf = io.BytesIO()
    try:
        pil_img.save(buf, format='JPEG', quality=int(quality), optimize=True)
    except Exception:
        return None
    data = buf.getvalue()
    # Only accept it if we actually save space.
    if original_len and len(data) >= original_len and scale >= 1.0:
        return None
    obj._data = data
    obj[NameObject('/Filter')] = NameObject('/DCTDecode')
    obj[NameObject('/Width')] = NumberObject(width)
    obj[NameObject('/Height')] = NumberObject(height)
    obj[NameObject('/ColorSpace')] = NameObject(colorspace)
    obj[NameObject('/BitsPerComponent')] = NumberObject(8)
    obj[NameObject('/Length')] = NumberObject(len(data))
    for key in ('/DecodeParms', '/DecodeParams', '/Decode'):
        if key in obj:
            try:
                del obj[key]
            except Exception:
                pass
    return len(data)
# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------
def _iter_image_xobjects(reader):
    """Yield every unique /Image XObject in the document."""
    seen = set()
    for page in reader.pages:
        resources = page.get('/Resources')
        if resources is None:
            continue
        try:
            resources = resources.get_object()
            xobjects = resources.get('/XObject')
            if xobjects is None:
                continue
            xobjects = xobjects.get_object()
        except Exception:
            continue
        for name in list(xobjects.keys()):
            try:
                ref = xobjects[name]
                key = getattr(ref, 'idnum', None) or id(ref)
                if key in seen:
                    continue
                seen.add(key)
                obj = ref.get_object()
                if str(obj.get('/Subtype')) == '/Image':
                    yield obj
            except Exception:
                continue
def _serialize(reader, compress_streams=True):
    writer = PdfWriter()
    for page in reader.pages:
        if compress_streams:
            try:
                page.compress_content_streams()
            except Exception:
                pass
        writer.add_page(page)
    try:
        # Drop bulky metadata we don't need.
        writer.add_metadata({})
    except Exception:
        pass
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
def _lossless_pass(src_bytes):
    reader = PdfReader(io.BytesIO(src_bytes))
    return _serialize(reader, compress_streams=True)
def _lossy_pass(src_bytes, scale, quality, image_cache):
    """
    Produce a candidate PDF with all images re-encoded at (scale, quality).
    `image_cache` maps xobject id -> decoded PIL image so we only decode once
    across the whole search.
    """
    reader = PdfReader(io.BytesIO(src_bytes))
    changed = 0
    for obj in _iter_image_xobjects(reader):
        key = id(obj)
        ref_key = obj.indirect_reference.idnum if getattr(obj, 'indirect_reference', None) else key
        if ref_key in image_cache:
            pil = image_cache[ref_key]
        else:
            pil = _decode_image(obj)
            image_cache[ref_key] = pil
        if pil is None:
            continue
        if _write_jpeg_into(obj, pil, scale, quality):
            changed += 1
    return _serialize(reader, compress_streams=True), changed
# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def compress_pdf(src_bytes, target_bytes, output_path):
    """
    Compress `src_bytes` so the result is <= `target_bytes`, writing it to
    `output_path`.
    Returns a dict:
        original_size, final_size, target_size, reached (bool),
        ratio (percent saved), images_changed, lossy (bool), note (str)
    """
    original_size = len(src_bytes)
    if original_size == 0:
        raise CompressionError('The uploaded file is empty.')
    # Validate it's a readable PDF before doing any work.
    try:
        probe = PdfReader(io.BytesIO(src_bytes))
        if probe.is_encrypted:
            raise CompressionError(
                'This PDF is password-protected. Please decrypt it before compressing.')
        if len(probe.pages) == 0:
            raise CompressionError('This PDF has no pages.')
    except CompressionError:
        raise
    except Exception as exc:
        raise CompressionError('Could not read the PDF: %s' % exc)
    note = ''
    best = None
    images_changed = 0
    lossy_used = False
    # ---- Pass 1: lossless ------------------------------------------------
    try:
        best = _lossless_pass(src_bytes)
    except Exception:
        best = src_bytes
    if len(best) > original_size:
        best = src_bytes                             # never grow the file
    if len(best) <= target_bytes:
        note = 'Target reached with lossless optimisation – image quality untouched.'
    else:
        # ---- Pass 2: lossy ladder (binary search for mildest setting) ----
        if not PILLOW_AVAILABLE:
            note = ('Pillow is not installed, so only lossless optimisation could be '
                    'applied. Install Pillow (pip install Pillow) for stronger compression.')
        else:
            cache = {}
            # Is the target even achievable? Try the most aggressive rung first.
            try:
                hardest, hardest_changed = _lossy_pass(
                    src_bytes, *QUALITY_LADDER[-1], image_cache=cache)
            except Exception as exc:
                hardest, hardest_changed = None, 0
                note = 'Image compression failed (%s); returned lossless result.' % exc
            if hardest is not None:
                lossy_used = True
                if len(hardest) > target_bytes:
                    # Can't reach the target – keep the smallest we produced.
                    if len(hardest) < len(best):
                        best = hardest
                        images_changed = hardest_changed
                    note = ('The target size could not be reached; this is the smallest '
                            'version achievable without destroying the document.')
                else:
                    # Binary search the ladder for the mildest rung that fits.
                    lo, hi = 0, len(QUALITY_LADDER) - 1
                    best = hardest
                    images_changed = hardest_changed
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        try:
                            candidate, changed = _lossy_pass(
                                src_bytes, *QUALITY_LADDER[mid], image_cache=cache)
                        except Exception:
                            lo = mid + 1
                            continue
                        if len(candidate) <= target_bytes:
                            best = candidate
                            images_changed = changed
                            hi = mid - 1             # try to be even gentler
                        else:
                            lo = mid + 1
                    note = 'Target reached by re-compressing embedded images.'
    final_size = len(best)
    # Safety: never emit something unreadable.
    try:
        check = PdfReader(io.BytesIO(best))
        if len(check.pages) == 0:
            raise ValueError('no pages')
    except Exception:
        best = src_bytes
        final_size = len(best)
        note = ('Compression produced an invalid file, so the original was kept. '
                'Please try a different target size.')
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'wb') as fh:
        fh.write(best)
    ratio = 0.0
    if original_size:
        ratio = max(0.0, (1.0 - float(final_size) / float(original_size)) * 100.0)
    return {
        'original_size': original_size,
        'final_size': final_size,
        'target_size': target_bytes,
        'reached': final_size <= target_bytes,
        'ratio': round(ratio, 1),
        'images_changed': images_changed,
        'lossy': lossy_used,
        'note': note,
    }