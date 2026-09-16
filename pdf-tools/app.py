import os, re, json, uuid

from flask import (
    Flask, render_template, request, url_for,
    send_file, send_from_directory, jsonify, abort,
)
from werkzeug.utils import secure_filename

from pdf_tools import (
    merge, split, remove, rotate, watermark, encrypt,
    split_segments, make_zip, crop_pdf, organize_pdf,
)
from pdf_compress import (
    compress_pdf, parse_target_size, human_size, CompressionError,
)
from file_convert import (
    ALLOWED_EXTS, HTML_ACCEPT, ConversionError,
    convert_to_pdf, normalize_to_pdf, is_supported, ext_of,
)
import ocr as ocr_module
import summarize as sum_module
import compare as cmp_module
import formbuilder as fb_module
import signpdf as sign_module
import redact as redact_module
from pageselect import resolve_scope as _resolve_scope

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-only-change-me')
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64 MB total upload

GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generated')
os.makedirs(GENERATED_DIR, exist_ok=True)

WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads_tmp')
os.makedirs(WORK_DIR, exist_ok=True)


# -----------------------------------------------------------------------------
# Tool catalogue – single source of truth for the navigation and the homepage.
# Declaring it here (rather than in a template) means a tool is described once
# and every template can render it.
# -----------------------------------------------------------------------------
TOOL_GROUPS = [
    {
        'key': 'organize',
        'name': 'Organize',
        'icon': 'fa-clone',
        'blurb': 'Rearrange, combine and trim the pages of a document.',
        'tools': [
            ('merge_pdf',         'Merge PDFs',   'fa-object-group', 'Combine several files into one document.'),
            ('split_pdf',         'Split PDF',    'fa-cut',          'Extract page ranges into separate files.'),
            ('organize_pdf_view', 'Organize PDF', 'fa-th',           'Reorder, duplicate, rotate and insert pages.'),
            ('remove_pages',      'Remove Pages', 'fa-trash',        'Delete the pages you no longer need.'),
            ('rotate_pdf',        'Rotate PDF',   'fa-repeat',       'Turn pages to any multiple of 90°.'),
            ('crop_pdf_view',     'Crop PDF',     'fa-crop',         'Trim margins by dragging a selection.'),
        ],
    },
    {
        'key': 'convert',
        'name': 'Convert & Optimize',
        'icon': 'fa-exchange',
        'blurb': 'Move between formats and get file sizes under control.',
        'tools': [
            ('convert_pdf_view',  'PDF Convert',     'fa-file-pdf-o', 'Turn images and Office files into PDF.'),
            ('compress_pdf_view', 'PDF Compression', 'fa-compress',   'Shrink a PDF toward a target size.'),
        ],
    },
    {
        'key': 'edit',
        'name': 'Edit & Sign',
        'icon': 'fa-pencil',
        'blurb': 'Add content, fill in fields and sign off on documents.',
        'tools': [
            ('edit_pdf_view', 'PDF Edit',         'fa-paint-brush',     'Add text, images, drawings and highlights.'),
            ('form_pdf_view', 'PDF Form Builder', 'fa-check-square-o',  'Build a fillable form with real PDF fields.'),
            ('sign_pdf_view', 'Sign PDF',         'fa-pencil-square-o', 'Draw, type or upload a signature.'),
            ('watermark_pdf', 'Add Watermark',    'fa-tint',            'Stamp a watermark over your pages.'),
        ],
    },
    {
        'key': 'insight',
        'name': 'Read & Analyze',
        'icon': 'fa-search',
        'blurb': 'Pull meaning out of documents you already have.',
        'tools': [
            ('ocr_pdf_view',       'OCR PDF',       'fa-eye',        'Recognize text in scans and make them searchable.'),
            ('summarize_pdf_view', 'PDF Summarize', 'fa-align-left', 'Summarize the important content.'),
            ('compare_pdf_view',   'PDF Compare',   'fa-columns',    'See exactly what changed between two files.'),
        ],
    },
    {
        'key': 'protect',
        'name': 'Protect',
        'icon': 'fa-lock',
        'blurb': 'Control who can open a file and what it reveals.',
        'tools': [
            ('encrypt_pdf',     'Encrypt PDF', 'fa-lock',   'Protect a document with a password.'),
            ('redact_pdf_view', 'Redact PDF',  'fa-eraser', 'Permanently remove sensitive content.'),
        ],
    },
]

TOOL_COUNT = sum(len(g['tools']) for g in TOOL_GROUPS)


# Make the accept="..." string available to every template.
@app.context_processor
def _inject_upload_accept():
    return {
        'UPLOAD_ACCEPT': HTML_ACCEPT,
        'TOOL_GROUPS': TOOL_GROUPS,
        'TOOL_COUNT': TOOL_COUNT,
    }


def _open_as_pdf(file_storage):
    """
    Accept any supported upload and return (path_to_pdf, display_name, cleanup_fn).
    Caller should call cleanup_fn() when done.
    """
    pdf_path, display, _ = normalize_to_pdf(file_storage, WORK_DIR)

    def cleanup():
        try:
            if os.path.isfile(pdf_path) and pdf_path.startswith(WORK_DIR):
                os.unlink(pdf_path)
        except OSError:
            pass
    return pdf_path, display, cleanup


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _new_token():
    return uuid.uuid4().hex[:12]


def _safe_name(name):
    name = secure_filename(name) or 'file.pdf'
    if not name.lower().endswith('.pdf') and not name.lower().endswith('.zip'):
        name += '.pdf'
    return name


# -----------------------------------------------------------------------------
# Error handlers – every tool talks to the server over fetch()/JSON, so HTML
# error pages surfaced as the useless "Server returned an unexpected response".
# -----------------------------------------------------------------------------
def _wants_json():
    return (request.path != '/' and
            (request.method == 'POST' or
             request.accept_mimetypes.best == 'application/json'))


@app.errorhandler(413)
def _too_large(e):
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    msg = (f'That upload is too large. The server limit is {limit_mb} MB in total '
           f'(all files in one request combined).')
    if _wants_json():
        return jsonify(success=False, error=msg), 413
    return msg, 413


@app.errorhandler(404)
def _not_found(e):
    if _wants_json():
        return jsonify(success=False, error='Not found.'), 404
    return render_template('home.html'), 404


@app.errorhandler(500)
def _server_error(e):
    app.logger.exception('Unhandled server error')
    if _wants_json():
        return jsonify(success=False,
                       error='The server hit an unexpected error. Please try again.'), 500
    return 'Internal server error', 500

def _output_target(prefix: str, display_name: str | None = None, ext: str = 'pdf'):
    """
    Build a unique (name, path) pair inside GENERATED_DIR for a tool's output.

    Thirteen routes repeated the same three lines of token/name/path plumbing,
    several of them with subtly different filename-sanitising. One helper keeps
    the naming consistent and the routes shorter.
    """
    token = _new_token()
    if display_name:
        base = os.path.splitext(secure_filename(display_name) or '')[0][:40]
        name = f'{prefix}_{token}_{base}.{ext}' if base else f'{prefix}_{token}.{ext}'
    else:
        name = f'{prefix}_{token}.{ext}'
    return name, os.path.join(GENERATED_DIR, name)

def _prepare_preview(field: str = 'file'):
    """
    Shared handler for every tool's ``action=prepare`` step.

    All the viewer-based tools need the same thing: take the upload, convert it
    to PDF if necessary, and stream it back so pdf.js can render pages in the
    browser. This was copy-pasted into nine routes; now there is one copy.

    Returns a Flask response (the PDF, or a JSON error with its status code).
    """
    upload = request.files.get(field)
    if not upload or not upload.filename:
        return jsonify(success=False, error='Please upload a file.'), 400
    try:
        pdf_path, display_name, cleanup = _open_as_pdf(upload)
    except ConversionError as e:
        return jsonify(success=False, error=str(e)), 400
    try:
        return send_file(pdf_path, mimetype='application/pdf',
                         as_attachment=False, download_name=display_name,
                         max_age=0)
    finally:
        cleanup()


def _job_status(module, jid: str, done_fields=None, url_fields=None):
    """
    Shared handler for every tool's ``action=status`` poll.

    Builds the common envelope (status/progress/message/error) and, once the
    job is done, copies the tool-specific result fields plus turns generated
    filenames into download URLs.

      done_fields: job keys to expose only when finished, e.g. ['summary']
      url_fields : {response_key: job_key_holding_a_filename}
    """
    job = module.get_job(jid)
    if not job:
        return jsonify(success=False, error='Unknown or expired job.'), 404

    payload = {
        'success': True,
        'id': job['id'],
        'status': job['status'],
        'progress': job['progress'],
        'message': job['message'],
        'error': job['error'],
    }
    # Always-useful extras, when the tool tracks them.
    for key in ('total', 'processed', 'current_page', 'backend', 'chunks',
                'source_language', 'ocr_pages'):
        if key in job:
            payload[key] = job[key]

    if job['status'] == 'done':
        for key in (done_fields or []):
            payload[key] = job.get(key)
        for out_key, job_key in (url_fields or {}).items():
            name = job.get(job_key)
            payload[out_key] = url_for('download_file', filename=name) if name else None
            payload[job_key] = name
    return jsonify(payload)


@app.route('/download/<path:filename>')
def download_file(filename):
    """Serve a previously generated file as a download."""
    # secure_filename strips path separators so this is safe against traversal.
    safe = secure_filename(filename)
    full = os.path.join(GENERATED_DIR, safe)
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(GENERATED_DIR, safe, as_attachment=True)


# -----------------------------------------------------------------------------
# Home
# -----------------------------------------------------------------------------
@app.route('/')
def home():
    return render_template('home.html')


@app.route('/healthz')
def healthz():
    """Lightweight health check used by the hosting platform."""
    return jsonify(status='ok')


# -----------------------------------------------------------------------------
# Merge
# -----------------------------------------------------------------------------
@app.route('/merge_pdf', methods=['GET', 'POST'])
def merge_pdf():
    if request.method == 'POST':
        files = request.files.getlist("files")
        files = [f for f in files if f and f.filename]
        if len(files) < 2:
            return jsonify(success=False,
                           error='Please upload at least two files to merge.'), 400

        cleanups, pdf_paths = [], []
        try:
            for f in files:
                try:
                    pdf_path, _, cleanup = _open_as_pdf(f)
                except ConversionError as e:
                    return jsonify(success=False,
                                   error=f'"{f.filename}": {e}'), 400
                pdf_paths.append(pdf_path)
                cleanups.append(cleanup)

            out_name, out_path = _output_target('merged')
            try:
                merge(pdf_paths, output_path=out_path)
            except Exception as e:
                return jsonify(success=False, error=f'Could not merge files: {e}'), 500
            return jsonify(success=True,
                           url=url_for('download_file', filename=out_name),
                           name=out_name)
        finally:
            for c in cleanups:
                c()
    return render_template('pdf/merge_pdf.html')


# -----------------------------------------------------------------------------
# Split
# -----------------------------------------------------------------------------
@app.route('/split_pdf', methods=['GET', 'POST'])
def split_pdf():
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False,
                           error='Please upload a file.'), 400

        segments_raw = request.form.get('segments', '')
        try:
            segments = json.loads(segments_raw) if segments_raw else []
        except json.JSONDecodeError:
            return jsonify(success=False,
                           error='Invalid segment data submitted.'), 400

        if not segments:
            return jsonify(success=False,
                           error='Please add at least one segment (start & end page).'), 400

        try:
            pdf_path, display_name, cleanup = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400

        token = _new_token()
        out_dir = os.path.join(GENERATED_DIR, f'split_{token}')
        base = os.path.splitext(secure_filename(display_name) or 'file.pdf')[0] or 'split'

        try:
            results = split_segments(pdf_path, segments, out_dir, base_name=base)
        except ValueError as e:
            cleanup()
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            cleanup()
            return jsonify(success=False, error=f'Could not split PDF: {e}'), 500
        cleanup()

        # Move generated files to flat GENERATED_DIR with unique prefixed names
        segments_response = []
        for r in results:
            unique_name = f'split_{token}_{r["name"]}'
            dst = os.path.join(GENERATED_DIR, unique_name)
            os.replace(r['path'], dst)
            r['path'] = dst
            segments_response.append({
                'name': r['name'],
                'start': r['start'],
                'end': r['end'],
                'url': url_for('download_file', filename=unique_name),
            })

        # cleanup the temporary sub-directory
        try:
            os.rmdir(out_dir)
        except OSError:
            pass

        # bundle all into a zip for "Download All"
        zip_name = f'split_{token}_all.zip'
        zip_path = os.path.join(GENERATED_DIR, zip_name)
        make_zip([{'name': s['name'], 'path': os.path.join(GENERATED_DIR, f'split_{token}_' + s['name'])}
                  for s in segments_response], zip_path)

        return jsonify(success=True,
                       segments=segments_response,
                       all_url=url_for('download_file', filename=zip_name))
    return render_template('pdf/split_pdf.html')


# -----------------------------------------------------------------------------
# Remove pages
# -----------------------------------------------------------------------------
@app.route('/remove_pages', methods=['GET', 'POST'])
def remove_pages():
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False,
                           error='Please upload a file.'), 400

        ranges = request.form.get('range', '').strip()
        if not ranges:
            return jsonify(success=False,
                           error='Please enter page range(s) to remove (e.g. 1-3,5,7).'), 400

        regexObj = re.compile(r'\d+-\d+|\d+')
        matches = regexObj.findall(ranges)
        only_pages = []
        for match in matches:
            if '-' in match:
                left, right = match.split('-')
                if int(left) <= int(right):
                    for num in range(int(left), int(right) + 1):
                        only_pages.append(num)
                else:
                    return jsonify(success=False,
                                   error=f'Invalid range "{match}" – start must be <= end.'), 400
            else:
                only_pages.append(int(match))

        if not only_pages:
            return jsonify(success=False,
                           error='Could not parse any pages. Use a format like 1-3,5,7.'), 400

        page_list = sorted(set(only_pages))

        try:
            pdf_path, _, cleanup = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400

        out_name, out_path = _output_target('removed')
        try:
            info = remove(pdf_path, page_list, output_path=out_path)
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not remove pages: {e}'), 500
        finally:
            cleanup()

        return jsonify(success=True,
                       url=url_for('download_file', filename=out_name),
                       name=out_name,
                       removed=info['removed'],
                       ignored=info['ignored'],
                       total_pages=info['total_pages'])
    return render_template('pdf/remove_pages.html')


# -----------------------------------------------------------------------------
# Rotate
# -----------------------------------------------------------------------------
@app.route('/rotate_pdf', methods=['GET', 'POST'])
def rotate_pdf():
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False, error='Please upload a file.'), 400

        raw = (request.form.get('degreeOfRotation') or '').strip()
        if raw == 'custom':
            raw = (request.form.get('customDegree') or '').strip()
        if not raw:
            return jsonify(success=False, error='Please choose or enter a rotation degree.'), 400
        try:
            degree = int(raw)
        except ValueError:
            return jsonify(success=False,
                           error='Custom rotation must be an integer (e.g. 90, 180, -90).'), 400
        if degree % 90 != 0:
            return jsonify(success=False,
                           error='Rotation degree must be a multiple of 90.'), 400

        try:
            pdf_path, _, cleanup = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400

        out_name, out_path = _output_target('rotated')
        try:
            rotate(pdf_path, degree, output_path=out_path)
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not rotate PDF: {e}'), 500
        finally:
            cleanup()
        return jsonify(success=True,
                       url=url_for('download_file', filename=out_name),
                       name=out_name, degree=degree)
    return render_template('pdf/rotate_pdf.html')


# -----------------------------------------------------------------------------
# Watermark
# -----------------------------------------------------------------------------
@app.route('/watermark_pdf', methods=['GET', 'POST'])
def watermark_pdf():
    if request.method == 'POST':
        upload = request.files.get('file')
        watermark_upload = request.files.get('watermark_file')
        if not upload or not upload.filename:
            return jsonify(success=False, error='Please upload the file to be watermarked.'), 400
        if not watermark_upload or not watermark_upload.filename:
            return jsonify(success=False, error='Please upload the watermark file.'), 400
        pages = request.form.get('typeOfWatermark', 'first')
        if pages not in ('first', 'all'):
            return jsonify(success=False, error='Please choose which pages to watermark.'), 400

        try:
            pdf_path, _, cleanup1 = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400
        try:
            wm_path, _, cleanup2 = _open_as_pdf(watermark_upload)
        except ConversionError as e:
            cleanup1()
            return jsonify(success=False, error=f'Watermark: {e}'), 400

        out_name, out_path = _output_target('watermarked')
        try:
            watermark(pdf_path, wm_path, pages, output_path=out_path)
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not watermark PDF: {e}'), 500
        finally:
            cleanup1()
            cleanup2()
        return jsonify(success=True,
                       url=url_for('download_file', filename=out_name),
                       name=out_name)
    return render_template('pdf/watermark_pdf.html')


# -----------------------------------------------------------------------------
# Encrypt
# -----------------------------------------------------------------------------
@app.route('/encrypt_pdf', methods=['GET', 'POST'])
def encrypt_pdf():
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False, error='Please upload a file.'), 400
        password = request.form.get('password') or ''
        if not password:
            return jsonify(success=False, error='Please enter a password.'), 400

        try:
            pdf_path, _, cleanup = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400

        out_name, out_path = _output_target('encrypted')
        try:
            encrypt(pdf_path, password, output_path=out_path)
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not encrypt PDF: {e}'), 500
        finally:
            cleanup()
        return jsonify(success=True,
                       url=url_for('download_file', filename=out_name),
                       name=out_name)
    return render_template('pdf/encrypt_pdf.html')


# -----------------------------------------------------------------------------
# Compress
# -----------------------------------------------------------------------------
@app.route('/compress_pdf', methods=['GET', 'POST'])
def compress_pdf_view():
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False, error='Please upload a file.'), 400

        try:
            target_bytes = parse_target_size(request.form.get('targetSize'),
                                             request.form.get('targetUnit'))
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400

        try:
            pdf_path, _, cleanup = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400
        try:
            with open(pdf_path, 'rb') as fh:
                src_bytes = fh.read()
        except Exception as e:
            cleanup()
            return jsonify(success=False, error=f'Could not read the upload: {e}'), 400
        cleanup()

        out_name, out_path = _output_target('compressed')

        try:
            info = compress_pdf(src_bytes, target_bytes, out_path)
        except CompressionError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not compress PDF: {e}'), 500

        return jsonify(
            success=True,
            url=url_for('download_file', filename=out_name),
            name=out_name,
            reached=info['reached'],
            original_size=info['original_size'],
            final_size=info['final_size'],
            target_size=info['target_size'],
            original_size_h=human_size(info['original_size']),
            final_size_h=human_size(info['final_size']),
            target_size_h=human_size(info['target_size']),
            ratio=info['ratio'],
            note=info['note'],
        )
    return render_template('pdf/compress_pdf.html')


# -----------------------------------------------------------------------------
# Redact PDF (destructive – underlying content is removed, not just covered)
# -----------------------------------------------------------------------------
@app.route('/redact_pdf', methods=['GET', 'POST'])
def redact_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'prepare':
            return _prepare_preview()

        # -------- text search, returns PDF-space boxes for each occurrence
        if action == 'search':
            upload = request.files.get('file')
            if not upload or not upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400
            query = request.form.get('query') or ''
            case_sensitive = (request.form.get('case_sensitive') or '').lower() in ('1', 'true', 'on', 'yes')
            try:
                pdf_path, _, cleanup = _open_as_pdf(upload)
            except ConversionError as e:
                return jsonify(success=False, error=str(e)), 400
            try:
                hits = redact_module.search_text(pdf_path, query, case_sensitive)
            except redact_module.RedactError as e:
                return jsonify(success=False, error=str(e)), 400
            except Exception as e:
                return jsonify(success=False, error=f'Search failed: {e}'), 500
            finally:
                cleanup()
            return jsonify(success=True, query=query.strip(),
                           count=len(hits), hits=hits)

        # -------- permanently apply
        if action == 'save':
            upload = request.files.get('file')
            if not upload or not upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400

            raw = request.form.get('boxes', '')
            try:
                boxes = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return jsonify(success=False, error='Invalid redaction data submitted.'), 400
            if not boxes:
                return jsonify(success=False,
                               error='Please add at least one redaction area before applying.'), 400

            verify_raw = request.form.get('verify', '')
            try:
                verify_phrases = json.loads(verify_raw) if verify_raw else []
            except json.JSONDecodeError:
                verify_phrases = []

            try:
                pdf_path, display_name, cleanup = _open_as_pdf(upload)
            except ConversionError as e:
                return jsonify(success=False, error=str(e)), 400

            out_name, out_path = _output_target('redacted', display_name)

            try:
                info = redact_module.apply_redactions(pdf_path, boxes, out_path)
            except redact_module.RedactError as e:
                return jsonify(success=False, error=str(e)), 400
            except Exception as e:
                return jsonify(success=False, error=f'Could not apply redactions: {e}'), 500
            finally:
                cleanup()

            # Safety net: confirm the redacted phrases really are unextractable.
            leaked = []
            if isinstance(verify_phrases, list) and verify_phrases:
                leaked = redact_module.verify_removed(
                    out_path, [str(p) for p in verify_phrases][:50])

            return jsonify(success=True,
                           url=url_for('download_file', filename=out_name),
                           name=out_name,
                           pages=info['pages'],
                           redacted_pages=info['redacted_pages'],
                           full_pages=info['full_pages'],
                           boxes=info['boxes'],
                           leaked=leaked)

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/redact_pdf.html')


# -----------------------------------------------------------------------------
# Sign PDF (stamp raster signatures, initials and dates onto pages)
# -----------------------------------------------------------------------------
@app.route('/sign_pdf', methods=['GET', 'POST'])
def sign_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'prepare':
            return _prepare_preview()

        if action == 'save':
            upload = request.files.get('file')
            if not upload or not upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400

            raw = request.form.get('placements', '')
            try:
                placements = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return jsonify(success=False, error='Invalid placement data submitted.'), 400
            if not placements:
                return jsonify(success=False,
                               error='Please add at least one signature before saving.'), 400

            # Signature images arrive as files named sig_<key>.png; keep PNG bytes
            images = {}
            for field_name, storage in request.files.items():
                if not field_name.startswith('sig_'):
                    continue
                if not storage or not storage.filename:
                    continue
                key = field_name[4:]
                try:
                    images[key] = storage.read()
                except Exception as e:
                    return jsonify(success=False,
                                   error=f'Could not read signature "{key}": {e}'), 400
            if not images:
                return jsonify(success=False, error='No signature images were uploaded.'), 400

            try:
                pdf_path, display_name, cleanup = _open_as_pdf(upload)
            except ConversionError as e:
                return jsonify(success=False, error=str(e)), 400

            out_name, out_path = _output_target('signed', display_name)

            try:
                info = sign_module.sign_pdf(pdf_path, placements, images, out_path)
            except sign_module.SignError as e:
                return jsonify(success=False, error=str(e)), 400
            except Exception as e:
                return jsonify(success=False, error=f'Could not sign the PDF: {e}'), 500
            finally:
                cleanup()

            return jsonify(success=True,
                           url=url_for('download_file', filename=out_name),
                           name=out_name,
                           placements=info['placements'],
                           pages=info['pages'],
                           signed_pages=info['signed_pages'])

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/sign_pdf.html')


# -----------------------------------------------------------------------------
# Form Builder (real interactive AcroForm fields)
# -----------------------------------------------------------------------------
@app.route('/form_pdf', methods=['GET', 'POST'])
def form_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        # -------- prepare: convert upload, return previewable PDF
        if action == 'prepare':
            return _prepare_preview()

        # -------- save: build the fillable PDF
        if action == 'save':
            upload = request.files.get('file')
            if not upload or not upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400

            raw = request.form.get('fields', '')
            try:
                fields = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return jsonify(success=False, error='Invalid form definition submitted.'), 400
            if not fields:
                return jsonify(success=False,
                               error='Please add at least one form field before saving.'), 400

            try:
                pdf_path, display_name, cleanup = _open_as_pdf(upload)
            except ConversionError as e:
                return jsonify(success=False, error=str(e)), 400

            out_name, out_path = _output_target('form', display_name)

            try:
                info = fb_module.build_form(pdf_path, fields, out_path)
            except fb_module.FormBuilderError as e:
                return jsonify(success=False, error=str(e)), 400
            except Exception as e:
                return jsonify(success=False, error=f'Could not build the form: {e}'), 500
            finally:
                cleanup()

            return jsonify(success=True,
                           url=url_for('download_file', filename=out_name),
                           name=out_name,
                           fields=info['fields'],
                           counts=info['counts'],
                           radio_groups=info['radio_groups'],
                           pages=info['pages'])

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/form_pdf.html')


# -----------------------------------------------------------------------------
# Compare
# -----------------------------------------------------------------------------
@app.route('/compare_pdf', methods=['GET', 'POST'])
def compare_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'start')

        # -------- prepare one document for preview (field name: 'file')
        if action == 'prepare':
            return _prepare_preview()

        if action == 'status':
            return _job_status(
                cmp_module, (request.form.get('job') or '').strip(),
                done_fields=['entries', 'stats', 'meta'],
                url_fields={'report_url': 'report_name'})

        if action == 'cancel':
            jid = (request.form.get('job') or '').strip()
            ok = cmp_module.cancel_job(jid)
            return jsonify(success=ok, cancelled=ok)

        # -------- start a comparison
        if action == 'start':
            up_a = request.files.get('file_a')
            up_b = request.files.get('file_b')
            if not up_a or not up_a.filename:
                return jsonify(success=False, error='Please upload the original document (A).'), 400
            if not up_b or not up_b.filename:
                return jsonify(success=False, error='Please upload the document to compare (B).'), 400

            granularity = (request.form.get('granularity') or 'sentence').strip()
            if granularity not in cmp_module.GRANULARITIES:
                return jsonify(success=False,
                               error=f'Unknown granularity "{granularity}".'), 400

            # checkbox: "Show Whitespace Differences" → ignore_ws is the inverse
            show_ws = (request.form.get('show_whitespace') or '').strip().lower() in ('1', 'true', 'on', 'yes')
            ignore_ws = not show_ws

            ocr_lang = (request.form.get('ocr_language') or 'eng').strip()

            scope_a = (request.form.get('scope_a') or 'all').strip()
            pages_a_raw = (request.form.get('pages_a') or '').strip()
            cur_a = (request.form.get('current_page_a') or '').strip()
            scope_b = (request.form.get('scope_b') or 'all').strip()
            pages_b_raw = (request.form.get('pages_b') or '').strip()
            cur_b = (request.form.get('current_page_b') or '').strip()

            cleanups = []
            try:
                try:
                    path_a, name_a, clean_a = _open_as_pdf(up_a)
                except ConversionError as e:
                    return jsonify(success=False, error=f'Document A: {e}'), 400
                cleanups.append(clean_a)
                try:
                    path_b, name_b, clean_b = _open_as_pdf(up_b)
                except ConversionError as e:
                    return jsonify(success=False, error=f'Document B: {e}'), 400
                cleanups.append(clean_b)

                from PyPDF2 import PdfReader as _R
                try:
                    total_a = len(_R(path_a).pages)
                    total_b = len(_R(path_b).pages)
                except Exception as e:
                    return jsonify(success=False, error=f'Could not read PDF: {e}'), 400
                if total_a == 0 or total_b == 0:
                    return jsonify(success=False, error='One of the documents has no pages.'), 400

                try:
                    pages_a = _resolve_scope(scope_a, pages_a_raw, cur_a, total_a)
                except ValueError as e:
                    return jsonify(success=False, error=f'Document A: {e}'), 400
                try:
                    pages_b = _resolve_scope(scope_b, pages_b_raw, cur_b, total_b)
                except ValueError as e:
                    return jsonify(success=False, error=f'Document B: {e}'), 400

                jid = cmp_module.new_job()
                base_a = os.path.splitext(secure_filename(name_a) or 'a.pdf')[0][:20] or 'a'
                base_b = os.path.splitext(secure_filename(name_b) or 'b.pdf')[0][:20] or 'b'
                job_a = os.path.join(WORK_DIR, f'cmpA_{jid}_{base_a}.pdf')
                job_b = os.path.join(WORK_DIR, f'cmpB_{jid}_{base_b}.pdf')
                with open(path_a, 'rb') as f, open(job_a, 'wb') as o:
                    o.write(f.read())
                with open(path_b, 'rb') as f, open(job_b, 'wb') as o:
                    o.write(f.read())

                cmp_module.start_job(jid, job_a, pages_a, name_a,
                                     job_b, pages_b, name_b,
                                     granularity, ignore_ws,
                                     GENERATED_DIR, ocr_lang=ocr_lang,
                                     cleanup_paths=[job_a, job_b])

                return jsonify(success=True, job=jid,
                               total_pages_a=total_a, total_pages_b=total_b,
                               pages_a=pages_a, pages_b=pages_b,
                               large=(len(pages_a) > cmp_module.LARGE_DOC_PAGES or
                                      len(pages_b) > cmp_module.LARGE_DOC_PAGES))
            except Exception as e:
                return jsonify(success=False, error=f'Could not start comparison: {e}'), 500
            finally:
                for c in cleanups:
                    try: c()
                    except Exception: pass

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/compare_pdf.html')


# -----------------------------------------------------------------------------
# Summarize
# -----------------------------------------------------------------------------


@app.route('/summarize_pdf', methods=['GET', 'POST'])
def summarize_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'start')

        # -------- what can this server actually do?
        if action == 'capabilities':
            caps = sum_module.capabilities()
            caps['success'] = True
            return jsonify(caps)

        # -------- prepare: convert upload, return PDF bytes for preview
        if action == 'prepare':
            return _prepare_preview()

        # -------- poll status
        if action == 'status':
            return _job_status(
                sum_module, (request.form.get('job') or '').strip(),
                done_fields=['summary', 'warning', 'pages_used'],
                url_fields={'txt_url': 'txt_name'})

        if action == 'cancel':
            jid = (request.form.get('job') or '').strip()
            ok = sum_module.cancel_job(jid)
            return jsonify(success=ok, cancelled=ok)

        # -------- start a summarisation job
        if action == 'start':
            upload = request.files.get('file')
            if not upload or not upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400

            stype = (request.form.get('summary_type') or 'short').strip()
            if stype not in sum_module.SUMMARY_TYPES:
                return jsonify(success=False,
                               error=f'Unknown summary type "{stype}".'), 400

            out_lang = (request.form.get('output_language') or 'same').strip()
            if out_lang not in sum_module.OUTPUT_LANGUAGES:
                return jsonify(success=False,
                               error=f'Unknown output language "{out_lang}".'), 400

            ocr_lang = (request.form.get('ocr_language') or 'eng').strip()
            scope = (request.form.get('scope') or 'all').strip()
            pages_raw = (request.form.get('pages') or '').strip()
            current_raw = (request.form.get('current_page') or '').strip()

            try:
                pdf_path, display_name, cleanup = _open_as_pdf(upload)
            except ConversionError as e:
                return jsonify(success=False, error=str(e)), 400

            try:
                try:
                    from PyPDF2 import PdfReader as _R
                    total_pages = len(_R(pdf_path).pages)
                except Exception as e:
                    cleanup()
                    return jsonify(success=False, error=f'Could not read PDF: {e}'), 400
                if total_pages == 0:
                    cleanup()
                    return jsonify(success=False, error='The PDF has no pages.'), 400

                try:
                    target_pages = _resolve_scope(scope, pages_raw, current_raw, total_pages)
                except ValueError as e:
                    cleanup()
                    return jsonify(success=False, error=str(e)), 400

                jid = sum_module.new_job()
                safe_base = os.path.splitext(secure_filename(display_name) or 'file.pdf')[0] or 'summary'
                job_src = os.path.join(WORK_DIR, f'sum_{jid}_{safe_base[:20]}.pdf')
                with open(pdf_path, 'rb') as fin, open(job_src, 'wb') as fout:
                    fout.write(fin.read())
                cleanup()

                sum_module.start_job(jid, job_src, target_pages, stype, out_lang,
                                     GENERATED_DIR, base_name=safe_base,
                                     ocr_lang=ocr_lang, cleanup_paths=[job_src])

                return jsonify(success=True, job=jid,
                               total_pages=total_pages,
                               targets=target_pages,
                               large=(len(target_pages) > sum_module.LARGE_DOC_PAGES))
            except Exception as e:
                try: cleanup()
                except Exception: pass
                return jsonify(success=False, error=f'Could not start summarisation: {e}'), 500

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/summarize_pdf.html')


# -----------------------------------------------------------------------------
# OCR (searchable PDF + extracted text)
# -----------------------------------------------------------------------------
@app.route('/ocr_pdf', methods=['GET', 'POST'])
def ocr_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'start')

        # -------- languages available on this server
        if action == 'languages':
            return jsonify(
                success=True,
                tesseract=ocr_module.tesseract_available(),
                poppler=ocr_module.poppler_available(),
                languages=ocr_module.available_languages(),
                auto=ocr_module.AUTO_CODE,
            )

        # -------- prepare: convert upload, return PDF bytes for preview
        if action == 'prepare':
            return _prepare_preview()

        # -------- status / cancel operate on an existing job by id
        if action == 'status':
            return _job_status(
                ocr_module, (request.form.get('job') or '').strip(),
                done_fields=['pages'],
                url_fields={'pdf_url': 'pdf_name', 'txt_url': 'txt_name'})

        if action == 'cancel':
            jid = (request.form.get('job') or '').strip()
            ok = ocr_module.cancel_job(jid)
            return jsonify(success=ok, cancelled=ok)

        # -------- start a new job
        if action == 'start':
            if not ocr_module.tesseract_available():
                return jsonify(success=False,
                               error='Tesseract OCR is not installed on the server. '
                                     'Please install tesseract-ocr and restart.'), 500
            if not ocr_module.poppler_available():
                return jsonify(success=False,
                               error='pdf2image/poppler is not available on the server. '
                                     'Install poppler-utils and `pip install pdf2image`.'), 500

            upload = request.files.get('file')
            if not upload or not upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400

            lang = (request.form.get('language') or 'eng').strip()

            # Scope of processing
            scope = (request.form.get('scope') or 'all').strip()
            pages_raw = (request.form.get('pages') or '').strip()

            try:
                pdf_path, display_name, cleanup = _open_as_pdf(upload)
            except ConversionError as e:
                return jsonify(success=False, error=str(e)), 400

            # Move the prepared PDF into a longer-lived location because the
            # background job runs after this request returns.
            try:
                reader_probe = None
                try:
                    from PyPDF2 import PdfReader as _R
                    reader_probe = _R(pdf_path)
                    total_pages = len(reader_probe.pages)
                except Exception as e:
                    cleanup()
                    return jsonify(success=False, error=f'Could not read PDF: {e}'), 400
                if total_pages == 0:
                    cleanup()
                    return jsonify(success=False, error='The PDF has no pages.'), 400

                # Resolve which pages to OCR.
                # Use the shared resolver (same as Summarize/Compare). The old
                # inline version preferred the stale `pages` field over
                # `current_page`, so switching the UI to "Current page" after
                # typing page numbers crashed with a raw int() error.
                try:
                    target_pages = _resolve_scope(
                        scope, pages_raw, request.form.get('current_page'), total_pages)
                except (ValueError, ocr_module.OCRError) as e:
                    cleanup()
                    return jsonify(success=False, error=str(e)), 400

                # Persist the PDF for the worker thread (cleanup runs now).
                jid = ocr_module.new_job()
                safe_base = os.path.splitext(secure_filename(display_name) or 'file.pdf')[0] or 'ocr'
                job_src = os.path.join(WORK_DIR, f'ocr_{jid}{safe_base[:20]}.pdf')
                with open(pdf_path, 'rb') as fin, open(job_src, 'wb') as fout:
                    fout.write(fin.read())
                cleanup()

                ocr_module.start_ocr_job(jid, job_src, target_pages, lang,
                                         GENERATED_DIR, base_name=safe_base,
                                         cleanup_path=job_src)

                return jsonify(success=True,
                               job=jid,
                               total_pages=total_pages,
                               targets=target_pages,
                               language=lang)
            except Exception as e:
                try: cleanup()
                except Exception: pass
                return jsonify(success=False, error=f'Could not start OCR: {e}'), 500

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/ocr_pdf.html')


# -----------------------------------------------------------------------------
# Organize (reorder / delete / duplicate / rotate / insert pages)
# -----------------------------------------------------------------------------
@app.route('/organize_pdf', methods=['GET', 'POST'])
def organize_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        # ---------------- prepare: convert an upload into a previewable PDF
        if action == 'prepare':
            return _prepare_preview()

        # ---------------- save: execute the page plan
        if action == 'save':
            plan_raw = request.form.get('plan', '')
            try:
                plan = json.loads(plan_raw) if plan_raw else None
            except json.JSONDecodeError:
                return jsonify(success=False, error='Invalid page plan submitted.'), 400
            if not plan:
                return jsonify(success=False,
                               error='The result would have no pages. Please keep at least one page.'), 400

            # 'main' plus any number of inserted documents (insert_0, insert_1, ...)
            main_upload = request.files.get('file')
            if not main_upload or not main_upload.filename:
                return jsonify(success=False, error='Please upload a file.'), 400

            cleanups = []
            sources = {}
            try:
                try:
                    main_path, display_name, cleanup = _open_as_pdf(main_upload)
                except ConversionError as e:
                    return jsonify(success=False, error=str(e)), 400
                cleanups.append(cleanup)
                sources['main'] = main_path

                # Collect inserted documents.
                for key in list(request.files.keys()):
                    if not key.startswith('insert_'):
                        continue
                    up = request.files.get(key)
                    if not up or not up.filename:
                        continue
                    try:
                        ins_path, _, ins_cleanup = _open_as_pdf(up)
                    except ConversionError as e:
                        return jsonify(success=False,
                                       error=f'"{up.filename}": {e}'), 400
                    cleanups.append(ins_cleanup)
                    sources[key] = ins_path

                out_name, out_path = _output_target('organized', display_name)

                try:
                    info = organize_pdf(plan, sources, output_path=out_path)
                except ValueError as e:
                    return jsonify(success=False, error=str(e)), 400
                except Exception as e:
                    return jsonify(success=False,
                                   error=f'Could not build the PDF: {e}'), 500

                return jsonify(success=True,
                               url=url_for('download_file', filename=out_name),
                               name=out_name,
                               pages=info['pages'])
            finally:
                for c in cleanups:
                    c()

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/organize_pdf.html')


# -----------------------------------------------------------------------------
# Edit (visual WYSIWYG editor)
# -----------------------------------------------------------------------------
@app.route('/edit_pdf', methods=['GET', 'POST'])
def edit_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        # ---------------- prepare: convert upload, return previewable PDF
        if action == 'prepare':
            return _prepare_preview()

        # ---------------- save: assemble PDF from per-page PNGs
        if action == 'save':
            try:
                page_count = int(request.form.get('page_count', '0'))
            except ValueError:
                return jsonify(success=False, error='Invalid page count.'), 400
            if page_count < 1 or page_count > 200:
                return jsonify(success=False,
                               error='Invalid page count (must be between 1 and 200).'), 400

            try:
                from PIL import Image
            except ImportError:
                return jsonify(success=False,
                               error='Pillow is required on the server (pip install Pillow).'), 500

            images = []
            try:
                for i in range(1, page_count + 1):
                    f = request.files.get(f'page_{i}')
                    if not f:
                        return jsonify(success=False,
                                       error=f'Missing image for page {i}.'), 400
                    im = Image.open(f.stream)
                    im.load()
                    if im.mode != 'RGB':
                        im = im.convert('RGB')
                    images.append(im)
            except Exception as e:
                return jsonify(success=False,
                               error=f'Could not read page images: {e}'), 400

            out_name, out_path = _output_target('edited')
            try:
                images[0].save(
                    out_path,
                    save_all=True,
                    append_images=images[1:] if len(images) > 1 else [],
                    resolution=150.0,
                )
            except Exception as e:
                return jsonify(success=False,
                               error=f'Could not write PDF: {e}'), 500

            return jsonify(success=True,
                           url=url_for('download_file', filename=out_name),
                           name=out_name,
                           pages=page_count)

        return jsonify(success=False, error='Unknown action.'), 400

    return render_template('pdf/edit_pdf.html')


# -----------------------------------------------------------------------------
# Crop
# -----------------------------------------------------------------------------
@app.route('/crop_pdf', methods=['GET', 'POST'])
def crop_pdf_view():
    if request.method == 'POST':
        action = request.form.get('action', 'crop')
        if action == 'prepare':
            return _prepare_preview()

        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False, error='Please upload a file.'), 400

        try:
            pdf_path, display_name, cleanup = _open_as_pdf(upload)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400

        # action == 'crop'
        mode = (request.form.get('mode') or 'all').strip()
        if mode not in ('all', 'per_page'):
            cleanup()
            return jsonify(success=False,
                           error='Crop mode must be "all" or "per_page".'), 400

        rects_raw = request.form.get('rects', '')
        try:
            rects = json.loads(rects_raw) if rects_raw else None
        except json.JSONDecodeError:
            cleanup()
            return jsonify(success=False, error='Invalid crop data submitted.'), 400

        if rects is None:
            cleanup()
            return jsonify(success=False,
                           error='Please draw a crop area before clicking Crop.'), 400

        out_name, out_path = _output_target('cropped', display_name)

        try:
            info = crop_pdf(pdf_path, mode, rects, output_path=out_path)
        except ValueError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not crop PDF: {e}'), 500
        finally:
            cleanup()

        return jsonify(success=True,
                       url=url_for('download_file', filename=out_name),
                       name=out_name,
                       pages_cropped=info['pages_cropped'],
                       total_pages=info['total_pages'])
    return render_template('pdf/crop_pdf.html')


# -----------------------------------------------------------------------------
# Convert to PDF
# -----------------------------------------------------------------------------
@app.route('/convert_pdf', methods=['GET', 'POST'])
def convert_pdf_view():
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(success=False, error='Please upload a file.'), 400
        ext = ext_of(upload.filename)
        if not is_supported(upload.filename):
            return jsonify(
                success=False,
                error=('Unsupported file type "%s". Supported: %s.'
                       % (ext or 'unknown', ', '.join(sorted(ALLOWED_EXTS))))
            ), 400
        if ext == '.pdf':
            return jsonify(success=False,
                           error='This file is already a PDF – no conversion needed.'), 400

        out_name, out_path = _output_target('converted', upload.filename)

        # Save to a temp source file, then convert. Reuse the unique part of
        # the output name so the scratch file is unique too.
        src_path = os.path.join(WORK_DIR, f'cv_src_{_new_token()}{ext}')
        try:
            upload.save(src_path)
            if os.path.getsize(src_path) == 0:
                return jsonify(success=False, error='The uploaded file is empty.'), 400
            convert_to_pdf(src_path, out_path, ext=ext)
        except ConversionError as e:
            return jsonify(success=False, error=str(e)), 400
        except Exception as e:
            return jsonify(success=False, error=f'Could not convert file: {e}'), 500
        finally:
            try:
                if os.path.isfile(src_path):
                    os.unlink(src_path)
            except OSError:
                pass

        return jsonify(success=True,
                       url=url_for('download_file', filename=out_name),
                       name=out_name,
                       original_name=upload.filename,
                       source_ext=ext)
    return render_template('pdf/convert_pdf.html')


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', '5001')),
        debug=os.environ.get('FLASK_DEBUG', '').lower() in {'1', 'true', 'yes'},
    )
