"""
PDF summarisation.

Two back-ends
-------------
1. ``llm``        – optional. Used when the operator configures an LLM via
                    environment variables (see ``llm_config()``). Produces real
                    abstractive summaries AND can translate into the requested
                    output language.
2. ``extractive`` – always available, offline, no model downloads. Ranks the
                    document's own sentences with TF-IDF centrality and returns
                    the most central ones, formatted per summary type.

IMPORTANT LIMITATION
--------------------
The extractive back-end selects sentences that already exist in the document,
so it **cannot translate**. When the user asks for an output language that
differs from the detected source language and no LLM is configured, the job
still succeeds but returns a clear warning instead of silently pretending the
text was translated.

Large documents are handled map-reduce style: the text is split into chunks,
each chunk is summarised, then the chunk summaries are summarised again.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import jobs

from PyPDF2 import PdfReader

# sklearn/numpy are heavy (~13 s import) and only needed when an extractive
# summary is actually scored, so they are imported lazily on first use. Eager
# import made every Flask start-up take ~20 s.
TfidfVectorizer = None
np = None
_SK_STATE = None          # None = not probed yet, True/False = available or not


def _ensure_sklearn() -> bool:
    global TfidfVectorizer, np, _SK_STATE
    if _SK_STATE is None:
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer as _T
            import numpy as _np
            TfidfVectorizer, np = _T, _np
            _SK_STATE = True
        except Exception:
            _SK_STATE = False
    return _SK_STATE

try:
    import ocr as ocr_module
    _HAS_OCR = True
except Exception:
    ocr_module = None
    _HAS_OCR = False


class SummarizeError(Exception):
    """User-facing summarisation failure."""


# ---------------------------------------------------------------------------
# Options exposed to the UI
# ---------------------------------------------------------------------------
SUMMARY_TYPES = {
    'short':     'Short Summary',
    'detailed':  'Detailed Summary',
    'key_points': 'Key Points',
    'executive': 'Executive Summary',
    'study_notes': 'Study Notes',
}

OUTPUT_LANGUAGES = {
    'same':    'Same as Original Document',
    'en':      'English',
    'zh-Hans': 'Chinese (Simplified)',
    'zh-Hant': 'Chinese (Traditional)',
    'ja':      'Japanese',
    'ko':      'Korean',
    'es':      'Spanish',
    'fr':      'French',
    'de':      'German',
}

LANG_DISPLAY = {
    'en': 'English', 'zh-Hans': 'Chinese (Simplified)', 'zh-Hant': 'Chinese (Traditional)',
    'ja': 'Japanese', 'ko': 'Korean', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
    'ru': 'Russian', 'unknown': 'Unknown',
}

LARGE_DOC_PAGES = 100
CHUNK_CHARS = 12000
MAX_TOTAL_CHARS = 2_000_000


# ---------------------------------------------------------------------------
# LLM configuration (entirely optional)
# ---------------------------------------------------------------------------
def llm_config() -> dict | None:
    """
    Read LLM settings from the environment. Returns None when not configured,
    in which case the extractive back-end is used.

      SUMMARIZER_PROVIDER = openai | anthropic
      OPENAI_API_KEY, OPENAI_BASE_URL (default https://api.openai.com/v1),
      OPENAI_MODEL (default gpt-4o-mini)
      ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL (default https://api.anthropic.com),
      ANTHROPIC_MODEL (default claude-3-5-haiku-latest)
    """
    provider = (os.environ.get('SUMMARIZER_PROVIDER') or '').strip().lower()
    if provider == 'openai':
        key = os.environ.get('OPENAI_API_KEY')
        if not key:
            return None
        return {
            'provider': 'openai',
            'key': key,
            'base': (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').rstrip('/'),
            'model': os.environ.get('OPENAI_MODEL') or 'gpt-4o-mini',
        }
    if provider == 'anthropic':
        key = os.environ.get('ANTHROPIC_API_KEY')
        if not key:
            return None
        return {
            'provider': 'anthropic',
            'key': key,
            'base': (os.environ.get('ANTHROPIC_BASE_URL') or 'https://api.anthropic.com').rstrip('/'),
            'model': os.environ.get('ANTHROPIC_MODEL') or 'claude-3-5-haiku-latest',
        }
    return None


def backend_name() -> str:
    return 'llm' if llm_config() else 'extractive'


def capabilities() -> dict:
    """What the server can actually do – the UI uses this to set expectations."""
    cfg = llm_config()
    return {
        'backend': 'llm' if cfg else 'extractive',
        'provider': cfg['provider'] if cfg else None,
        'model': cfg['model'] if cfg else None,
        'can_translate': bool(cfg),
        'can_abstract': bool(cfg),
        'ocr_available': bool(_HAS_OCR and ocr_module.tesseract_available()
                              and ocr_module.poppler_available()),
        'summary_types': SUMMARY_TYPES,
        'output_languages': OUTPUT_LANGUAGES,
    }


def _llm_complete(cfg: dict, system: str, user: str, max_tokens: int = 1400) -> str:
    """Single blocking chat completion via stdlib urllib (no SDK required)."""
    try:
        if cfg['provider'] == 'openai':
            url = cfg['base'] + '/chat/completions'
            payload = {
                'model': cfg['model'],
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                'max_tokens': max_tokens,
                'temperature': 0.3,
            }
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + cfg['key'],
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                        headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            return (body['choices'][0]['message']['content'] or '').strip()

        url = cfg['base'] + '/v1/messages'
        payload = {
            'model': cfg['model'],
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': user}],
        }
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': cfg['key'],
            'anthropic-version': '2023-06-01',
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                    headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        parts = body.get('content') or []
        return ''.join(p.get('text', '') for p in parts).strip()
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:300]
        except Exception:
            pass
        raise SummarizeError(f'LLM request failed (HTTP {e.code}). {detail}')
    except urllib.error.URLError as e:
        raise SummarizeError(f'Could not reach the LLM endpoint: {e.reason}')
    except (KeyError, IndexError, ValueError) as e:
        raise SummarizeError(f'Unexpected LLM response format: {e}')


# ---------------------------------------------------------------------------
# Language detection (heuristic, dependency-free)
# ---------------------------------------------------------------------------
_LATIN_PROFILES = {
    'en': {'the', 'and', 'of', 'to', 'in', 'is', 'that', 'for', 'it', 'with', 'as', 'was', 'on', 'are', 'by'},
    'es': {'de', 'la', 'que', 'el', 'en', 'y', 'los', 'del', 'las', 'por', 'con', 'una', 'para', 'es', 'se'},
    'fr': {'le', 'de', 'et', 'la', 'les', 'des', 'en', 'un', 'une', 'du', 'est', 'que', 'pour', 'dans', 'qui'},
    'de': {'der', 'die', 'und', 'den', 'das', 'von', 'zu', 'mit', 'des', 'ist', 'im', 'für', 'auf', 'nicht', 'eine'},
}


def detect_language(text: str) -> str:
    """Return a best-guess language code; 'unknown' when we can't tell."""
    if not text or not text.strip():
        return 'unknown'
    sample = text[:6000]

    counts = {
        'han':      len(re.findall(r'[\u4e00-\u9fff]', sample)),
        'hiragana': len(re.findall(r'[\u3040-\u309f]', sample)),
        'katakana': len(re.findall(r'[\u30a0-\u30ff]', sample)),
        'hangul':   len(re.findall(r'[\uac00-\ud7af\u1100-\u11ff]', sample)),
        'cyrillic': len(re.findall(r'[\u0400-\u04ff]', sample)),
        'latin':    len(re.findall(r'[A-Za-z]', sample)),
    }

    if counts['hiragana'] + counts['katakana'] >= 5:
        return 'ja'
    if counts['hangul'] >= 5:
        return 'ko'
    if counts['han'] >= 5:
        simp = len(re.findall(r'[国对学会说个见时从这么无为电话开关门东车长风飞马鸟鱼]', sample))
        trad = len(re.findall(r'[國對學會說個見時從這麼無為電話開關門東車長風飛馬鳥魚]', sample))
        return 'zh-Hant' if trad > simp else 'zh-Hans'
    if counts['cyrillic'] >= 5:
        return 'ru'

    if counts['latin'] >= 20:
        words = re.findall(r"[a-zà-öø-ÿ']+", sample.lower())
        if not words:
            return 'en'
        wordset = set(words)
        best, best_score = 'en', -1
        for code, profile in _LATIN_PROFILES.items():
            score = len(wordset & profile)
            if score > best_score:
                best, best_score = code, score
        return best if best_score > 0 else 'en'

    return 'unknown'


# ---------------------------------------------------------------------------
# Sentence handling
# ---------------------------------------------------------------------------
_CJK_LANGS = {'zh-Hans', 'zh-Hant', 'ja', 'ko'}


def normalize_text(text: str, lang: str = 'en') -> str:
    """
    Un-wrap PDF line breaks so sentences stay intact.

    PDF text extraction hard-wraps lines mid-sentence. If we split on newlines
    we slice sentences in half, so we first rejoin wrapped lines and keep only
    blank lines as real paragraph boundaries.
    """
    if not text:
        return ''
    t = text.replace('\r\n', '\n').replace('\r', '\n')
    t = t.replace('\u00ad', '')
    t = re.sub(r'(\w)-\n(\w)', r'\1\2', t)
    t = re.sub(r'\n[ \t]*\n+', '\u241E', t)
    t = t.replace('\n', '' if lang in _CJK_LANGS else ' ')
    t = t.replace('\u241E', '\n')
    t = re.sub(r'[ \t]{2,}', ' ', t)
    return t.strip()


def split_sentences(text: str, lang: str = 'en') -> list[str]:
    """Split text into sentences; works for Latin and CJK punctuation."""
    text = normalize_text(text, lang)
    if not text:
        return []

    if lang in _CJK_LANGS:
        raw = re.split(r'(?<=[。！？；!?])\s*|\n+', text)
    else:
        DOT = '\u2024'                              # ONE DOT LEADER placeholder
        protected = re.sub(r'\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|Inc|Ltd|Co|Corp|vs|etc|al|eg|ie|No|Fig)\.',
                           lambda m: m.group(0).replace('.', DOT), text)
        protected = re.sub(r'\b(e\.g|i\.e)\.',
                           lambda m: m.group(0).replace('.', DOT), protected)
        protected = re.sub(r'(\d)\.(\d)',
                           lambda m: m.group(1) + DOT + m.group(2), protected)
        raw = re.split(r'(?<=[.!?])["\')\]]*\s+|\n+', protected)
        raw = [s.replace(DOT, '.') for s in raw]

    out = []
    for s in raw:
        s = ' '.join(s.split()) if lang not in _CJK_LANGS else s.strip()
        if not s:
            continue
        if sum(1 for c in s if c.isalnum()) < 8:
            continue
        out.append(s)
    return out


def _word_tokens(s: str, lang: str) -> list[str]:
    if lang in _CJK_LANGS:
        chars = re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', s)
        return [''.join(pair) for pair in zip(chars, chars[1:])] or chars
    return re.findall(r"[a-zA-Z0-9à-öø-ÿ']{2,}", s.lower())


_EXEC_KEYWORDS = {
    'revenue', 'profit', 'cost', 'costs', 'margin', 'growth', 'market', 'customer',
    'customers', 'strategy', 'strategic', 'risk', 'risks', 'forecast', 'budget',
    'investment', 'roi', 'kpi', 'quarter', 'fiscal', 'sales', 'earnings', 'cash',
    'stakeholder', 'competitive', 'opportunity', 'recommendation', 'objective',
    'performance', 'efficiency', 'loss', 'pricing', 'demand', 'supply', 'million',
    'billion', 'percent', 'target', 'priority', 'initiative',
}


# ---------------------------------------------------------------------------
# Extractive scoring
# ---------------------------------------------------------------------------
def _score_sentences(sentences: list[str], lang: str, exec_bias: bool = False) -> list[float]:
    """Return a relevance score per sentence (higher = more central)."""
    n = len(sentences)
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    scores = [0.0] * n

    if _ensure_sklearn():
        try:
            analyzer = (lambda s: _word_tokens(s, lang))
            vec = TfidfVectorizer(analyzer=analyzer, min_df=1)
            matrix = vec.fit_transform(sentences)
            if matrix.shape[1] > 0:
                centroid = np.asarray(matrix.mean(axis=0)).ravel()
                cnorm = float(np.linalg.norm(centroid)) or 1.0
                rows = matrix.toarray()
                for i in range(n):
                    rn = float(np.linalg.norm(rows[i])) or 1.0
                    scores[i] = float(rows[i].dot(centroid) / (rn * cnorm))
        except Exception:
            scores = [0.0] * n

    if not any(scores):
        freq: dict[str, int] = {}
        for s in sentences:
            for w in _word_tokens(s, lang):
                freq[w] = freq.get(w, 0) + 1
        if freq:
            peak = max(freq.values())
            for i, s in enumerate(sentences):
                toks = _word_tokens(s, lang)
                if toks:
                    scores[i] = sum(freq.get(w, 0) / peak for w in toks) / (len(toks) ** 0.5)

    for i in range(n):
        scores[i] *= 1.0 + max(0.0, (1.0 - i / max(1, n)) * 0.15)

    if exec_bias:
        for i, s in enumerate(sentences):
            hits = len(set(_word_tokens(s, lang)) & _EXEC_KEYWORDS)
            if hits:
                scores[i] *= 1.0 + min(0.6, hits * 0.15)

    return scores


def _norm_key(s: str, lang: str) -> str:
    """Normalised form used to spot duplicate / near-duplicate sentences."""
    if lang in _CJK_LANGS:
        return re.sub(r'\s+', '', s)
    return ' '.join(re.findall(r'[a-z0-9]+', s.lower()))


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _top_sentences(sentences: list[str], lang: str, count: int,
                   exec_bias: bool = False, redundancy: float = 0.72) -> list[str]:
    """
    Pick the `count` highest-scoring sentences, greedily skipping ones that are
    near-duplicates of an already-selected sentence (important for documents
    with repeated headers/boilerplate or repeated pages).
    """
    if not sentences:
        return []
    count = max(1, min(count, len(sentences)))
    scores = _score_sentences(sentences, lang, exec_bias=exec_bias)
    order = sorted(range(len(sentences)), key=lambda i: scores[i], reverse=True)

    chosen: list[int] = []
    seen_exact: set[str] = set()
    seen_tokens: list[set] = []

    for i in order:
        if len(chosen) >= count:
            break
        key = _norm_key(sentences[i], lang)
        if not key or key in seen_exact:
            continue
        toks = set(_word_tokens(sentences[i], lang))
        if any(_overlap(toks, prev) >= redundancy for prev in seen_tokens):
            continue
        chosen.append(i)
        seen_exact.add(key)
        seen_tokens.append(toks)

    if len(chosen) < min(count, len(sentences)):
        for i in order:
            if len(chosen) >= count:
                break
            if i not in chosen and _norm_key(sentences[i], lang) not in seen_exact:
                chosen.append(i)
                seen_exact.add(_norm_key(sentences[i], lang))

    chosen.sort()
    return [sentences[i] for i in chosen]


def _key_terms(sentences: list[str], lang: str, count: int = 10) -> list[str]:
    freq: dict[str, int] = {}
    stop = set()
    for profile in _LATIN_PROFILES.values():
        stop |= profile
    for s in sentences:
        for w in _word_tokens(s, lang):
            if w in stop or len(w) < 3:
                continue
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:count]]


TARGETS = {
    'short':       {'ratio': 0.08, 'min': 3,  'max': 6},
    'detailed':    {'ratio': 0.25, 'min': 8,  'max': 28},
    'key_points':  {'ratio': 0.12, 'min': 5,  'max': 12},
    'executive':   {'ratio': 0.12, 'min': 5,  'max': 10},
    'study_notes': {'ratio': 0.20, 'min': 8,  'max': 18},
}


def _target_count(n_sentences: int, stype: str) -> int:
    t = TARGETS.get(stype, TARGETS['short'])
    return max(t['min'], min(t['max'], int(round(n_sentences * t['ratio'])) or t['min']))


def extractive_summary(text: str, stype: str, lang: str) -> str:
    """Format an extractive summary according to the requested summary type."""
    sentences = split_sentences(text, lang)
    if not sentences:
        raise SummarizeError(
            'No usable sentences were found in the selected pages. '
            'The document may be empty or contain only images.')

    n = _target_count(len(sentences), stype)

    if stype == 'key_points':
        picks = _top_sentences(sentences, lang, n)
        return '\n'.join('• ' + s.strip() for s in picks)

    if stype == 'executive':
        picks = _top_sentences(sentences, lang, n, exec_bias=True)
        terms = _key_terms(sentences, lang, 8)
        out = ['EXECUTIVE SUMMARY', '']
        out.append(' '.join(s.strip() for s in picks))
        if terms:
            out += ['', 'Key topics: ' + ', '.join(terms)]
        return '\n'.join(out)

    if stype == 'study_notes':
        picks = _top_sentences(sentences, lang, n)
        terms = _key_terms(sentences, lang, 12)
        out = ['STUDY NOTES', '']
        if terms:
            out += ['Key terms', ', '.join(terms), '']
        out += ['Main points']
        out += ['  ' + str(i + 1) + '. ' + s.strip() for i, s in enumerate(picks)]
        overview = _top_sentences(sentences, lang, min(3, len(sentences)))
        out += ['', 'One-paragraph overview', ' '.join(s.strip() for s in overview)]
        return '\n'.join(out)

    picks = _top_sentences(sentences, lang, n)
    joiner = '' if lang in _CJK_LANGS else ' '
    return joiner.join(s.strip() for s in picks)


# ---------------------------------------------------------------------------
# LLM prompting
# ---------------------------------------------------------------------------
_TYPE_INSTRUCTIONS = {
    'short': 'Write a concise summary of at most 6 sentences covering only the most important information.',
    'detailed': 'Write a comprehensive summary covering all major details, organised into short paragraphs.',
    'key_points': 'Produce a bullet-point list (use "• ") of the most important points. No preamble.',
    'executive': 'Write an executive summary aimed at business decision makers: objectives, findings, '
                 'financial/impact highlights, risks and recommended actions. Use short labelled sections.',
    'study_notes': 'Write study-friendly notes: a "Key terms" list, a numbered "Main points" list, '
                   'and a short "Overview" paragraph.',
}


def _llm_summary(cfg: dict, text: str, stype: str, out_lang: str, src_lang: str,
                 reduce_stage: bool = False) -> str:
    instruction = _TYPE_INSTRUCTIONS.get(stype, _TYPE_INSTRUCTIONS['short'])
    if out_lang == 'same':
        lang_line = (f'Write the output in the same language as the source document '
                     f'(detected: {LANG_DISPLAY.get(src_lang, src_lang)}).')
    else:
        lang_line = f'Write the output in {OUTPUT_LANGUAGES.get(out_lang, out_lang)}.'

    system = ('You are a precise document summarisation assistant. You never invent facts that are '
              'not present in the provided text. If the text is fragmentary, summarise only what is there.')
    stage = ('The following text is a concatenation of partial summaries of a long document. '
             'Merge them into one coherent final summary, removing redundancy.\n\n'
             if reduce_stage else
             'Summarise the following document text.\n\n')
    user = f'{stage}{instruction}\n{lang_line}\n\n--- TEXT START ---\n{text}\n--- TEXT END ---'
    return _llm_complete(cfg, system, user)


# ---------------------------------------------------------------------------
# Chunking (map-reduce for long documents)
# ---------------------------------------------------------------------------
def chunk_text(text: str, lang: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Split text into chunks on sentence boundaries, each <= max_chars."""
    sentences = split_sentences(text, lang)
    if not sentences:
        return [text[:max_chars]] if text.strip() else []
    chunks, cur, cur_len = [], [], 0
    for s in sentences:
        slen = len(s) + 1
        if cur and cur_len + slen > max_chars:
            chunks.append(' '.join(cur))
            cur, cur_len = [s], slen
        else:
            cur.append(s)
            cur_len += slen
    if cur:
        chunks.append(' '.join(cur))
    return chunks


# ---------------------------------------------------------------------------
# Text extraction (with OCR fallback)
# ---------------------------------------------------------------------------
MIN_CHARS_PER_PAGE = 40


def extract_pages_text(pdf_path: str, pages: list[int], lang_for_ocr: str = 'eng',
                       progress=None, cancelled=None) -> tuple[dict, list[int]]:
    """
    Return ({page_no: text}, ocr_pages). Pages without enough embedded text are
    OCR'd when OCR is available.
    """
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    out: dict[int, str] = {}
    ocr_used: list[int] = []

    ocr_ready = bool(_HAS_OCR and ocr_module.tesseract_available()
                     and ocr_module.poppler_available())

    for i, pno in enumerate(pages, start=1):
        if cancelled and cancelled():
            raise SummarizeError('Cancelled by user.')
        if not (1 <= pno <= total):
            continue
        if progress:
            progress(i, len(pages), f'Extracting text from page {pno}…')
        page = reader.pages[pno - 1]
        try:
            txt = page.extract_text() or ''
        except Exception:
            txt = ''

        if sum(1 for c in txt if not c.isspace()) < MIN_CHARS_PER_PAGE and ocr_ready:
            if progress:
                progress(i, len(pages), f'Page {pno} has little text – running OCR…')
            try:
                from pdf2image import convert_from_path
                import pytesseract
                imgs = convert_from_path(pdf_path, dpi=200, first_page=pno, last_page=pno)
                if imgs:
                    txt = pytesseract.image_to_string(imgs[0], lang=lang_for_ocr) or ''
                    ocr_used.append(pno)
            except Exception:
                pass

        out[pno] = txt
        if sum(len(v) for v in out.values()) > MAX_TOTAL_CHARS:
            raise SummarizeError('The selected pages contain too much text to summarise safely.')

    return out, ocr_used


# ---------------------------------------------------------------------------
# Job registry (shared implementation – see jobs.py)
# ---------------------------------------------------------------------------
_REGISTRY = jobs.JobRegistry({
    'warning': None,
    'summary': '',
    'source_language': None,
    'backend': None,
    'pages_used': [],
    'ocr_pages': [],
    'chunks': 0,
    'txt_name': None,
})


def new_job() -> str:
    jid = _REGISTRY.new()
    _REGISTRY.patch(jid, backend=backend_name())
    return jid


def get_job(jid: str):
    return _REGISTRY.get(jid)


def cancel_job(jid: str) -> bool:
    return _REGISTRY.cancel(jid)


_patch = _REGISTRY.patch
_cancelled = _REGISTRY.is_cancelled


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def start_job(jid: str, pdf_path: str, pages: list[int], stype: str,
              out_lang: str, generated_dir: str, base_name: str = 'summary',
              ocr_lang: str = 'eng', cleanup_paths=None) -> None:
    t = threading.Thread(
        target=_worker,
        args=(jid, pdf_path, list(pages), stype, out_lang, generated_dir, base_name,
              ocr_lang, list(cleanup_paths or [])),
        daemon=True,
    )
    t.start()

def _worker(jid, pdf_path, pages, stype, out_lang, generated_dir, base_name, ocr_lang,
            cleanup_paths=None):
    try:
        if stype not in SUMMARY_TYPES:
            raise SummarizeError(f'Unknown summary type "{stype}".')
        if out_lang not in OUTPUT_LANGUAGES:
            raise SummarizeError(f'Unknown output language "{out_lang}".')

        cfg = llm_config()
        _patch(jid, status='running', progress=2, message='Reading document…',
               backend='llm' if cfg else 'extractive')

        def prog(i, total, msg):
            pct = 2 + int(43 * (i / max(1, total)))
            _patch(jid, progress=pct, message=msg)

        page_texts, ocr_pages = extract_pages_text(
            pdf_path, pages, lang_for_ocr=ocr_lang,
            progress=prog, cancelled=lambda: _cancelled(jid))

        combined = '\n'.join(page_texts.get(p, '') for p in sorted(page_texts))
        if sum(1 for c in combined if not c.isspace()) < 80:
            raise SummarizeError(
                'Not enough readable text was found in the selected pages. '
                'If this is a scanned document, run the OCR PDF tool first '
                '(or install Tesseract so OCR can run automatically).')

        src_lang = detect_language(combined)
        _patch(jid, source_language=src_lang, ocr_pages=ocr_pages,
               progress=48, message='Analysing text…')

        reader_pages = len(pages)
        need_chunking = (reader_pages > LARGE_DOC_PAGES) or (len(combined) > CHUNK_CHARS)
        warning = None

        if out_lang != 'same' and not cfg:
            target = OUTPUT_LANGUAGES.get(out_lang, out_lang)
            warning = (
                f'This server has no LLM configured, so it used the offline extractive '
                f'summariser. That method selects sentences from the document itself and '
                f'therefore cannot translate. The summary below is in the document\'s '
                f'original language ({LANG_DISPLAY.get(src_lang, src_lang)}), not '
                f'{target}. To get {target} output, configure an LLM '
                f'(SUMMARIZER_PROVIDER + API key) and run again.')

        if not need_chunking:
            _patch(jid, chunks=1, progress=60, message='Generating summary…')
            if cfg:
                summary = _llm_summary(cfg, combined, stype, out_lang, src_lang)
            else:
                summary = extractive_summary(combined, stype, src_lang)
        else:
            chunks = chunk_text(combined, src_lang, CHUNK_CHARS)
            _patch(jid, chunks=len(chunks), progress=52,
                   message=f'Large document – summarising in {len(chunks)} chunk(s)…')
            partials = []
            for idx, ch in enumerate(chunks, start=1):
                if _cancelled(jid):
                    raise SummarizeError('Cancelled by user.')
                _patch(jid,
                       progress=52 + int(33 * (idx - 1) / max(1, len(chunks))),
                       message=f'Summarising chunk {idx}/{len(chunks)}…')
                if cfg:
                    partials.append(_llm_summary(cfg, ch, 'detailed', 'same', src_lang))
                else:
                    partials.append(extractive_summary(ch, 'detailed', src_lang))

            merged = '\n'.join(partials)
            _patch(jid, progress=88, message='Combining chunk summaries…')
            if cfg:
                summary = _llm_summary(cfg, merged, stype, out_lang, src_lang, reduce_stage=True)
            else:
                summary = extractive_summary(merged, stype, src_lang)

        if not (summary or '').strip():
            raise SummarizeError('The summariser produced no output. Please try different pages.')

        _patch(jid, progress=95, message='Writing summary file…')
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base_name or 'summary')[:40] or 'summary'
        txt_name = f'summary_{jid}_{safe}.txt'
        os.makedirs(generated_dir, exist_ok=True)
        with open(os.path.join(generated_dir, txt_name), 'w', encoding='utf-8') as fh:
            fh.write(f'Summary type   : {SUMMARY_TYPES.get(stype, stype)}\n')
            fh.write(f'Output language: {OUTPUT_LANGUAGES.get(out_lang, out_lang)}\n')
            fh.write(f'Source language: {LANG_DISPLAY.get(src_lang, src_lang)}\n')
            fh.write(f'Pages          : {", ".join(str(p) for p in pages)}\n')
            fh.write(f'Method         : {"LLM (" + cfg["model"] + ")" if cfg else "offline extractive"}\n')
            if ocr_pages:
                fh.write(f'OCR was used on: {", ".join(str(p) for p in ocr_pages)}\n')
            if warning:
                fh.write(f'\nNOTE: {warning}\n')
            fh.write('\n' + '-' * 60 + '\n\n')
            fh.write(summary.rstrip() + '\n')

        _patch(jid, status='done', progress=100, summary=summary.rstrip(),
               warning=warning, txt_name=txt_name, pages_used=pages,
               finished_at=time.time(), message='Summary complete.')
    except SummarizeError as e:
        _REGISTRY.fail(jid, str(e), 'Cancelled. No summary was produced.')
    except Exception as e:                           # pragma: no cover
        _REGISTRY.fail(jid, f'Unexpected error: {e}',
                       'Cancelled. No summary was produced.')
    finally:
        jobs.discard(cleanup_paths)