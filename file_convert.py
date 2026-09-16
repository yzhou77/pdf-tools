"""
Convert common office/image formats to PDF.
Images (JPG/JPEG/PNG)  ->  Pillow (always available, pure Python)
Word / Excel / PowerPoint -> LibreOffice headless
`normalize_to_pdf()` is the helper used by every other tool: if the user
uploaded a PDF it's returned as-is, otherwise it gets converted first. That
lets merge / split / remove / rotate / watermark / encrypt / compress accept
images and Office documents as input too (requirement #8).
"""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import uuid
from werkzeug.utils import secure_filename
IMAGE_EXTS   = {'.jpg', '.jpeg', '.png'}
OFFICE_EXTS  = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
PDF_EXTS     = {'.pdf'}
ALLOWED_EXTS = IMAGE_EXTS | OFFICE_EXTS | PDF_EXTS
# Accept string for HTML <input accept="...">
HTML_ACCEPT = ','.join(sorted(ALLOWED_EXTS)) + ',application/pdf,image/jpeg,image/png'
# Seconds allowed for one LibreOffice conversion
LIBREOFFICE_TIMEOUT = 120
class ConversionError(Exception):
    """Raised when a file cannot be converted to PDF."""
def ext_of(filename: str) -> str:
    if not filename:
        return ''
    return os.path.splitext(filename)[1].lower()
def is_supported(filename: str) -> bool:
    return ext_of(filename) in ALLOWED_EXTS
def _find_libreoffice() -> str | None:
    for name in ('libreoffice', 'soffice'):
        path = shutil.which(name)
        if path:
            return path
    mac_path = '/Applications/LibreOffice.app/Contents/MacOS/soffice'
    if os.path.isfile(mac_path):
        return mac_path
    return None
# ---------------------------------------------------------------------------
# Image -> PDF
# ---------------------------------------------------------------------------
def _image_to_pdf(src_path: str, out_path: str) -> None:
    try:
        from PIL import Image
    except ImportError as e:
        raise ConversionError(
            'Pillow is required for image conversion. '
            'Please run: pip install Pillow') from e
    try:
        img = Image.open(src_path)
        img.load()
    except Exception as e:
        raise ConversionError(f'Could not read the image: {e}')
    if img.mode in ('RGBA', 'LA'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode == 'P':
        img = img.convert('RGB')
    elif img.mode not in ('RGB', 'L', 'CMYK'):
        img = img.convert('RGB')
    try:
        img.save(out_path, format='PDF', resolution=150.0)
    except Exception as e:
        raise ConversionError(f'Could not write the PDF: {e}')
# ---------------------------------------------------------------------------
# Office -> PDF (LibreOffice headless)
# ---------------------------------------------------------------------------
def _office_to_pdf(src_path: str, out_path: str) -> None:
    soffice = _find_libreoffice()
    if not soffice:
        raise ConversionError(
            'LibreOffice is required to convert Word / Excel / PowerPoint '
            'files. Please install LibreOffice and make sure "soffice" is on '
            'the PATH (on Debian/Ubuntu: apt install libreoffice).')
    with tempfile.TemporaryDirectory(prefix='lo_conv_') as workdir:
        profile = os.path.join(workdir, 'profile')
        os.makedirs(profile, exist_ok=True)
        out_dir = os.path.join(workdir, 'out')
        os.makedirs(out_dir, exist_ok=True)
        cmd = [
            soffice,
            '-env:UserInstallation=file://' + profile,
            '--headless',
            '--nologo',
            '--nofirststartwizard',
            '--norestore',
            '--convert-to', 'pdf',
            '--outdir', out_dir,
            src_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=LIBREOFFICE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ConversionError('Conversion timed out. Please try a smaller file.')
        except Exception as e:
            raise ConversionError(f'LibreOffice could not be launched: {e}')
        if result.returncode != 0:
            err = (result.stderr or result.stdout or b'').decode(
                'utf-8', errors='replace')[:500]
            raise ConversionError(f'LibreOffice conversion failed: {err.strip() or "unknown error"}')
        base = os.path.splitext(os.path.basename(src_path))[0]
        produced = os.path.join(out_dir, base + '.pdf')
        if not os.path.isfile(produced):
            pdfs = [f for f in os.listdir(out_dir) if f.lower().endswith('.pdf')]
            if not pdfs:
                raise ConversionError('LibreOffice did not produce a PDF (empty or unsupported file?).')
            produced = os.path.join(out_dir, pdfs[0])
        os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
        shutil.copyfile(produced, out_path)
# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def convert_to_pdf(src_path: str, out_path: str, ext: str | None = None) -> str:
    """Convert a file on disk to PDF, written to `out_path`. Returns out_path."""
    ext = (ext or ext_of(src_path)).lower()
    if ext not in ALLOWED_EXTS:
        raise ConversionError(
            f'Unsupported file type "{ext or "?"}". '
            f'Supported types: {", ".join(sorted(ALLOWED_EXTS))}.')
    if ext in PDF_EXTS:
        shutil.copyfile(src_path, out_path)
        return out_path
    if not os.path.isfile(src_path) or os.path.getsize(src_path) == 0:
        raise ConversionError('The uploaded file is empty.')
    if ext in IMAGE_EXTS:
        _image_to_pdf(src_path, out_path)
    elif ext in OFFICE_EXTS:
        _office_to_pdf(src_path, out_path)
    else:
        raise ConversionError(f'Unsupported file type "{ext}".')
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise ConversionError('Conversion produced an empty PDF.')
    return out_path
def normalize_to_pdf(file_storage, work_dir: str) -> tuple[str, str, bool]:
    """
    Accept a Werkzeug FileStorage. If it is already a PDF, save it as-is.
    Otherwise convert it to PDF. Returns (pdf_path, display_name, converted).
    """
    if file_storage is None or not getattr(file_storage, 'filename', ''):
        raise ConversionError('No file was uploaded.')
    raw_name = file_storage.filename
    safe_name = secure_filename(raw_name) or 'upload'
    ext = ext_of(safe_name) or ext_of(raw_name)
    if ext not in ALLOWED_EXTS:
        raise ConversionError(
            f'Unsupported file type "{ext or "(unknown)"}". '
            f'Supported: {", ".join(sorted(ALLOWED_EXTS))}.')
    os.makedirs(work_dir, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    src_path = os.path.join(work_dir, f'src_{token}{ext}')
    file_storage.save(src_path)
    if os.path.getsize(src_path) == 0:
        os.unlink(src_path)
        raise ConversionError('The uploaded file is empty.')
    if ext in PDF_EXTS:
        validate_pdf(src_path)
        return src_path, raw_name, False
    pdf_path = os.path.join(work_dir, f'conv_{token}.pdf')
    try:
        convert_to_pdf(src_path, pdf_path, ext=ext)
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass
    validate_pdf(pdf_path)
    display = os.path.splitext(raw_name)[0] + '.pdf'
    return pdf_path, display, True

def validate_pdf(path: str) -> None:
    """
    Check that `path` is a readable, unencrypted PDF with at least one page.

    Every tool funnels uploads through normalize_to_pdf(), so doing this here
    means each tool reports an actionable message instead of leaking PyPDF2
    internals like "File has not been decrypted" or "EOF marker not found".
    """
    from PyPDF2 import PdfReader
    try:
        reader = PdfReader(path)
    except Exception:
        raise ConversionError(
            'This file could not be read as a PDF. It may be corrupted, '
            'incomplete, or not really a PDF.')
    try:
        if reader.is_encrypted:
            # Some PDFs are encrypted with an empty owner password; those can
            # still be opened, so only reject the ones we genuinely cannot read.
            try:
                if reader.decrypt('') == 0:
                    raise ConversionError(
                        'This PDF is password-protected. Please remove the password '
                        'before using this tool.')
            except ConversionError:
                raise
            except Exception:
                raise ConversionError(
                    'This PDF is password-protected. Please remove the password '
                    'before using this tool.')
        if len(reader.pages) == 0:
            raise ConversionError('This PDF has no pages.')
        # Touch the first page so structurally broken files fail here, not
        # halfway through an operation.
        _ = reader.pages[0]
    except ConversionError:
        raise
    except Exception:
        raise ConversionError(
            'This PDF could not be processed — it appears to be damaged.')