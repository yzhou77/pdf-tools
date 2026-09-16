"""
PDF Form Builder – generates *real* interactive AcroForm fields.

PyPDF2 3.0.1 can only fill pre-existing fields, and merging reportlab-generated
widgets into an existing document loses the annotations. So this module builds
the AcroForm structures directly with PyPDF2's generic objects, which keeps the
original vector pages untouched and attaches genuine widget annotations to them.

What gets written
-----------------
* ``/Root /AcroForm`` with ``/Fields``, ``/DR`` (font resources), ``/DA`` and
  ``/NeedAppearances true`` so viewers build text appearances themselves.
* One ``/Subtype /Widget`` annotation per field, appended to its page
  ``/Annots``.
* Explicit ``/AP`` appearance streams for checkboxes and radio buttons (drawn
  with vector path operators, so they need no font resources and render even in
  viewers that ignore ``/NeedAppearances``).

Supported field types
---------------------
text | checkbox | radio | dropdown | date | signature
"""

from __future__ import annotations

import re

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject,
    FloatObject, NameObject, NumberObject, TextStringObject,
)


class FormBuilderError(Exception):
    """User-facing form-building failure."""


FIELD_TYPES = {
    'text':      'Text Field',
    'checkbox':  'Checkbox',
    'radio':     'Radio Button',
    'dropdown':  'Dropdown',
    'date':      'Date Field',
    'signature': 'Signature Field',
}

# /Ff bit flags (1-based bit numbers from the PDF spec)
FF_READ_ONLY = 1 << 0        # bit 1
FF_REQUIRED  = 1 << 1        # bit 2
FF_MULTILINE = 1 << 12       # bit 13 (text)
FF_RADIO     = 1 << 15       # bit 16 (button)
FF_COMBO     = 1 << 17       # bit 18 (choice)

MAX_FIELDS = 500


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _num(v) -> FloatObject:
    return FloatObject(round(float(v), 4))


def _rect(r) -> ArrayObject:
    x1, y1, x2, y2 = r
    return ArrayObject([_num(min(x1, x2)), _num(min(y1, y2)),
                        _num(max(x1, x2)), _num(max(y1, y2))])


def _safe_field_name(name: str, fallback: str) -> str:
    """PDF field names must not contain '.' (it separates hierarchy levels)."""
    name = (name or '').strip()
    if not name:
        name = fallback
    name = name.replace('.', '_')
    return name[:120]


def _font_resources() -> DictionaryObject:
    helv = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
        NameObject('/Encoding'): NameObject('/WinAnsiEncoding'),
    })
    zadb = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/ZapfDingbats'),
    })
    return DictionaryObject({
        NameObject('/Font'): DictionaryObject({
            NameObject('/Helv'): helv,
            NameObject('/ZaDb'): zadb,
        })
    })


def _appearance_stream(writer, width, height, content: str):
    """Build a Form XObject used as a widget appearance (/AP) stream."""
    stream = DecodedStreamObject()
    stream.set_data(content.encode('latin-1', 'replace'))
    stream.update({
        NameObject('/Type'): NameObject('/XObject'),
        NameObject('/Subtype'): NameObject('/Form'),
        NameObject('/FormType'): NumberObject(1),
        NameObject('/BBox'): ArrayObject([_num(0), _num(0), _num(width), _num(height)]),
        NameObject('/Resources'): DictionaryObject({
            NameObject('/ProcSet'): ArrayObject([NameObject('/PDF')]),
        }),
    })
    return writer._add_object(stream)


def _box_content(w, h, fill=True) -> str:
    """Border (+ optional white fill) for a widget's normal appearance."""
    parts = ['q']
    if fill:
        parts.append('1 1 1 rg')
        parts.append(f'0 0 {w:.2f} {h:.2f} re f')
    parts.append('0.2 0.2 0.2 RG 0.8 w')
    parts.append(f'0.4 0.4 {max(w - 0.8, 0):.2f} {max(h - 0.8, 0):.2f} re S')
    parts.append('Q')
    return '\n'.join(parts)


def _check_content(w, h) -> str:
    """Check mark drawn with path ops (no font dependency)."""
    m = min(w, h)
    pad = m * 0.22
    return '\n'.join([
        _box_content(w, h),
        'q 0 0 0 RG', f'{max(m * 0.14, 0.8):.2f} w 1 J 1 j',
        f'{pad:.2f} {h / 2:.2f} m',
        f'{w * 0.42:.2f} {pad:.2f} l',
        f'{w - pad:.2f} {h - pad:.2f} l',
        'S Q',
    ])


def _dot_content(w, h) -> str:
    """Filled circle for a selected radio button."""
    cx, cy = w / 2.0, h / 2.0
    r = min(w, h) * 0.26
    k = r * 0.5523
    return '\n'.join([
        'q 1 1 1 rg', f'0 0 {w:.2f} {h:.2f} re f',
        '0.2 0.2 0.2 RG 0.8 w',
        f'{cx:.2f} {cy - r * 1.9:.2f} m',     # outline circle
        f'{cx + r * 1.9:.2f} {cy:.2f} {cx:.2f} {cy + r * 1.9:.2f} {cx - r * 1.9:.2f} {cy:.2f} c',
        'S',
        '0 0 0 rg',
        f'{cx - r:.2f} {cy:.2f} m',
        f'{cx - r:.2f} {cy + k:.2f} {cx - k:.2f} {cy + r:.2f} {cx:.2f} {cy + r:.2f} c',
        f'{cx + k:.2f} {cy + r:.2f} {cx + r:.2f} {cy + k:.2f} {cx + r:.2f} {cy:.2f} c',
        f'{cx + r:.2f} {cy - k:.2f} {cx + k:.2f} {cy - r:.2f} {cx:.2f} {cy - r:.2f} c',
        f'{cx - k:.2f} {cy - r:.2f} {cx - r:.2f} {cy - k:.2f} {cx - r:.2f} {cy:.2f} c',
        'f Q',
    ])


def _radio_off_content(w, h) -> str:
    cx, cy = w / 2.0, h / 2.0
    r = min(w, h) * 0.26
    return '\n'.join([
        'q 1 1 1 rg', f'0 0 {w:.2f} {h:.2f} re f',
        '0.2 0.2 0.2 RG 0.8 w',
        f'{cx:.2f} {cy - r * 1.9:.2f} m',
        f'{cx + r * 1.9:.2f} {cy:.2f} {cx:.2f} {cy + r * 1.9:.2f} {cx - r * 1.9:.2f} {cy:.2f} c',
        'S Q',
    ])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate(fields, total_pages):
    if not isinstance(fields, list):
        raise FormBuilderError('Form definition must be a list of fields.')
    if not fields:
        raise FormBuilderError('Please add at least one form field before saving.')
    if len(fields) > MAX_FIELDS:
        raise FormBuilderError(f'Too many fields (limit is {MAX_FIELDS}).')

    seen_names = {}
    for idx, f in enumerate(fields, start=1):
        if not isinstance(f, dict):
            raise FormBuilderError(f'Field {idx}: invalid entry.')
        ftype = str(f.get('type', ''))
        if ftype not in FIELD_TYPES:
            raise FormBuilderError(f'Field {idx}: unknown field type "{ftype}".')
        try:
            page = int(f.get('page', 0))
        except (TypeError, ValueError):
            raise FormBuilderError(f'Field {idx}: page must be an integer.')
        if not (1 <= page <= total_pages):
            raise FormBuilderError(
                f'Field {idx}: page {page} is out of range (document has {total_pages}).')
        rect = f.get('rect')
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise FormBuilderError(f'Field {idx}: rect must be [x1, y1, x2, y2].')
        try:
            x1, y1, x2, y2 = (float(v) for v in rect)
        except (TypeError, ValueError):
            raise FormBuilderError(f'Field {idx}: rect values must be numbers.')
        if abs(x2 - x1) < 1 or abs(y2 - y1) < 1:
            raise FormBuilderError(f'Field {idx}: the field area is too small.')

        try:
            fs = float(f.get('fontSize', 11) or 11)
        except (TypeError, ValueError):
            raise FormBuilderError(f'Field {idx}: font size must be a number.')
        if not (0 <= fs <= 72):
            raise FormBuilderError(f'Field {idx}: font size must be between 0 and 72.')

        if ftype == 'dropdown':
            opts = f.get('options') or []
            if not isinstance(opts, list) or len([o for o in opts if str(o).strip()]) == 0:
                raise FormBuilderError(
                    f'Field {idx} ("{f.get("name") or ftype}"): a dropdown needs at '
                    f'least one option.')

        # Radio buttons share a name on purpose; everything else must be unique.
        name = _safe_field_name(f.get('name'), f'{ftype}_{idx}')
        if ftype == 'radio':
            group = _safe_field_name(f.get('radioGroup') or f.get('name'), f'radio_{idx}')
            f['_group'] = group
        else:
            if name in seen_names:
                raise FormBuilderError(
                    f'Duplicate field name "{name}" (fields {seen_names[name]} and {idx}). '
                    f'Field names must be unique.')
            seen_names[name] = idx
        f['_name'] = name
    return fields


# ---------------------------------------------------------------------------
# Widget construction
# ---------------------------------------------------------------------------
def _base_widget(page_ref, rect, da, flags) -> DictionaryObject:
    w = DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'),
        NameObject('/Subtype'): NameObject('/Widget'),
        NameObject('/Rect'): _rect(rect),
        NameObject('/F'): NumberObject(4),          # print
        NameObject('/DA'): TextStringObject(da),
        NameObject('/MK'): DictionaryObject({
            NameObject('/BC'): ArrayObject([_num(0.2), _num(0.2), _num(0.2)]),
            NameObject('/BG'): ArrayObject([_num(1), _num(1), _num(1)]),
        }),
        NameObject('/BS'): DictionaryObject({
            NameObject('/W'): NumberObject(1),
            NameObject('/S'): NameObject('/S'),
        }),
    })
    if flags:
        w[NameObject('/Ff')] = NumberObject(flags)
    if page_ref is not None:
        w[NameObject('/P')] = page_ref
    return w


def build_form(src_pdf, fields, output_path):
    """
    Write `src_pdf` to `output_path` with interactive AcroForm fields added.

    `fields` is a list of dicts:
        {type, page, rect:[x1,y1,x2,y2], name, label, placeholder, required,
         defaultValue, fontSize, options[], radioGroup, exportValue}

    Returns a summary dict.
    """
    reader = PdfReader(src_pdf)
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise FormBuilderError('The PDF has no pages.')

    fields = _validate(fields, total_pages)

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    acro_fields = ArrayObject()
    radio_parents: dict[str, DictionaryObject] = {}
    radio_parent_refs: dict[str, object] = {}
    counts = {k: 0 for k in FIELD_TYPES}

    for idx, f in enumerate(fields, start=1):
        ftype = f['type']
        page_index = int(f['page']) - 1
        page_obj = writer.pages[page_index]
        page_ref = page_obj.indirect_reference
        rect = [float(v) for v in f['rect']]
        w_pt = abs(rect[2] - rect[0])
        h_pt = abs(rect[3] - rect[1])

        font_size = float(f.get('fontSize', 11) or 11)
        da = f'/Helv {font_size:g} Tf 0 g'
        flags = FF_REQUIRED if f.get('required') else 0

        name = f['_name']
        label = str(f.get('label') or '').strip()
        placeholder = str(f.get('placeholder') or '').strip()
        default = f.get('defaultValue')

        # ---------------- text / date ------------------------------------
        if ftype in ('text', 'date'):
            widget = _base_widget(page_ref, rect, da, flags)
            widget[NameObject('/FT')] = NameObject('/Tx')
            widget[NameObject('/T')] = TextStringObject(name)
            tooltip = label or placeholder
            if ftype == 'date':
                fmt = str(f.get('dateFormat') or 'mm/dd/yyyy')
                tooltip = (tooltip + f' ({fmt})').strip()
            if tooltip:
                widget[NameObject('/TU')] = TextStringObject(tooltip)
            if default:
                widget[NameObject('/V')] = TextStringObject(str(default))
                widget[NameObject('/DV')] = TextStringObject(str(default))
            if ftype == 'date':
                fmt = str(f.get('dateFormat') or 'mm/dd/yyyy').replace('"', '')
                widget[NameObject('/AA')] = DictionaryObject({
                    NameObject('/F'): DictionaryObject({
                        NameObject('/S'): NameObject('/JavaScript'),
                        NameObject('/JS'): TextStringObject(f'AFDate_FormatEx("{fmt}");'),
                    }),
                    NameObject('/K'): DictionaryObject({
                        NameObject('/S'): NameObject('/JavaScript'),
                        NameObject('/JS'): TextStringObject(f'AFDate_KeystrokeEx("{fmt}");'),
                    }),
                })
            ref = writer._add_object(widget)
            acro_fields.append(ref)
            _attach(writer, page_obj, ref)
            counts[ftype] += 1
            continue

        # ---------------- checkbox ---------------------------------------
        if ftype == 'checkbox':
            on_state = '/Yes'
            checked = bool(default) and str(default).lower() not in ('0', 'false', 'off', '')
            widget = _base_widget(page_ref, rect, '/ZaDb 0 Tf 0 g', flags)
            widget[NameObject('/FT')] = NameObject('/Btn')
            widget[NameObject('/T')] = TextStringObject(name)
            if label:
                widget[NameObject('/TU')] = TextStringObject(label)
            widget[NameObject('/V')] = NameObject(on_state if checked else '/Off')
            widget[NameObject('/DV')] = NameObject(on_state if checked else '/Off')
            widget[NameObject('/AS')] = NameObject(on_state if checked else '/Off')
            widget[NameObject('/AP')] = DictionaryObject({
                NameObject('/N'): DictionaryObject({
                    NameObject(on_state): _appearance_stream(writer, w_pt, h_pt,
                                                             _check_content(w_pt, h_pt)),
                    NameObject('/Off'): _appearance_stream(writer, w_pt, h_pt,
                                                           _box_content(w_pt, h_pt)),
                })
            })
            ref = writer._add_object(widget)
            acro_fields.append(ref)
            _attach(writer, page_obj, ref)
            counts['checkbox'] += 1
            continue

        # ---------------- radio (grouped under one parent field) ---------
        if ftype == 'radio':
            group = f['_group']
            export = _safe_field_name(f.get('exportValue'), f'choice{idx}')
            on_state = '/' + re.sub(r'[^A-Za-z0-9_\-]', '_', export)

            if group not in radio_parents:
                parent = DictionaryObject({
                    NameObject('/FT'): NameObject('/Btn'),
                    NameObject('/T'): TextStringObject(group),
                    NameObject('/Ff'): NumberObject(FF_RADIO | flags),
                    NameObject('/V'): NameObject('/Off'),
                    NameObject('/DV'): NameObject('/Off'),
                    NameObject('/DA'): TextStringObject('/ZaDb 0 Tf 0 g'),
                    NameObject('/Kids'): ArrayObject(),
                })
                p_ref = writer._add_object(parent)
                radio_parents[group] = parent
                radio_parent_refs[group] = p_ref
                acro_fields.append(p_ref)
            parent = radio_parents[group]
            p_ref = radio_parent_refs[group]

            selected = bool(default) and str(default).lower() not in ('0', 'false', 'off', '')
            kid = _base_widget(page_ref, rect, '/ZaDb 0 Tf 0 g', 0)
            kid[NameObject('/Parent')] = p_ref
            if label:
                kid[NameObject('/TU')] = TextStringObject(label)
            kid[NameObject('/AS')] = NameObject(on_state if selected else '/Off')
            kid[NameObject('/AP')] = DictionaryObject({
                NameObject('/N'): DictionaryObject({
                    NameObject(on_state): _appearance_stream(writer, w_pt, h_pt,
                                                             _dot_content(w_pt, h_pt)),
                    NameObject('/Off'): _appearance_stream(writer, w_pt, h_pt,
                                                           _radio_off_content(w_pt, h_pt)),
                })
            })
            k_ref = writer._add_object(kid)
            parent[NameObject('/Kids')].append(k_ref)
            if selected:
                parent[NameObject('/V')] = NameObject(on_state)
                parent[NameObject('/DV')] = NameObject(on_state)
            _attach(writer, page_obj, k_ref)
            counts['radio'] += 1
            continue

        # ---------------- dropdown ---------------------------------------
        if ftype == 'dropdown':
            opts = [str(o) for o in (f.get('options') or []) if str(o).strip()]
            widget = _base_widget(page_ref, rect, da, FF_COMBO | flags)
            widget[NameObject('/FT')] = NameObject('/Ch')
            widget[NameObject('/T')] = TextStringObject(name)
            if label:
                widget[NameObject('/TU')] = TextStringObject(label)
            widget[NameObject('/Opt')] = ArrayObject(
                [TextStringObject(o) for o in opts])
            chosen = str(default) if default else ''
            if chosen and chosen in opts:
                widget[NameObject('/V')] = TextStringObject(chosen)
                widget[NameObject('/DV')] = TextStringObject(chosen)
            ref = writer._add_object(widget)
            acro_fields.append(ref)
            _attach(writer, page_obj, ref)
            counts['dropdown'] += 1
            continue

        # ---------------- signature --------------------------------------
        if ftype == 'signature':
            widget = _base_widget(page_ref, rect, da, flags)
            widget[NameObject('/FT')] = NameObject('/Sig')
            widget[NameObject('/T')] = TextStringObject(name)
            if label:
                widget[NameObject('/TU')] = TextStringObject(label)
            ref = writer._add_object(widget)
            acro_fields.append(ref)
            _attach(writer, page_obj, ref)
            counts['signature'] += 1
            continue

    # ---------------- AcroForm catalog entry ------------------------------
    acro = DictionaryObject({
        NameObject('/Fields'): acro_fields,
        NameObject('/DA'): TextStringObject('/Helv 0 Tf 0 g'),
        NameObject('/DR'): _font_resources(),
        NameObject('/NeedAppearances'): BooleanObject(True),
    })
    if counts['signature']:
        # Tell viewers the document contains signature fields.
        acro[NameObject('/SigFlags')] = NumberObject(3)
    writer._root_object[NameObject('/AcroForm')] = writer._add_object(acro)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return {
        'fields': len(fields),
        'counts': {k: v for k, v in counts.items() if v},
        'pages': total_pages,
        'radio_groups': len(radio_parents),
    }


def _attach(writer, page_obj, annot_ref):
    """Append a widget reference to a page's /Annots array."""
    if '/Annots' in page_obj:
        annots = page_obj['/Annots']
        try:
            annots = annots.get_object()
        except Exception:
            pass
        if isinstance(annots, ArrayObject):
            annots.append(annot_ref)
            return
        page_obj[NameObject('/Annots')] = ArrayObject([annot_ref])
    else:
        page_obj[NameObject('/Annots')] = ArrayObject([annot_ref])