"""Opt-in, paper-sized native reading for fresh issues. Legacy issues are untouched.

Two normal model operations per paper: integrated reading/drafting, then an
independent review of claims and selected pixels. One repair round is bounded.
This module prepares HTML; it never publishes, sends, or changes legacy seals.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from daily_agent.discovery_window import calendar_month_start
from daily_agent import parent_writer
from daily_agent.workflow_state import atomic_json, exclusive_lock, read_json, StateCorrupt

PROTOCOL = 'paper-first-v3'
SECTIONS = ('problem', 'method', 'insight', 'results', 'limits', 'reuse')
LABELS = dict(zip(SECTIONS, ('研究问题', '方法与机制', '核心洞见', '关键结果', '边界与不足', '值得借鉴')))
MAX_PAPERS = 12
MAX_TEXT = 360000
MAX_FIGURES = 3


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _put(root, content):
    digest = _hash(content)
    target = root / 'objects' / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != content:
            raise StateCorrupt('Immutable object collision')
    else:
        with target.open('xb') as handle:
            handle.write(content)
    return 'objects/' + digest


def _get(root, ref):
    if not isinstance(ref, str) or not re.fullmatch(r'objects/[0-9a-f]{64}', ref):
        raise StateCorrupt('Invalid object reference')
    target = root / ref
    if target.is_symlink() or target.parent.is_symlink():
        raise StateCorrupt('Symlink object refused')
    content = target.read_bytes()
    if _hash(content) != ref.split('/')[1]:
        raise StateCorrupt('Immutable object changed')
    return content


def _json(root, ref):
    return json.loads(_get(root, ref))


def _url(value):
    if not isinstance(value, str) or urlsplit(value).scheme != 'https' or not urlsplit(value).netloc:
        raise ValueError('An HTTPS primary source URL is required')
    return value


def _load(root):
    state = read_json(root / 'state.json')
    if not isinstance(state, dict) or state.get('protocol') != PROTOCOL:
        raise StateCorrupt('Not a paper-first issue')
    if state.get('binding') != _hash(_bytes({key: state[key] for key in ('protocol', 'issue_date', 'diagnostic', 'target', 'inputs', 'implementation_sha256')})):
        raise StateCorrupt('Issue inputs changed')
    if state['implementation_sha256'] != _hash(Path(__file__).read_bytes()):
        raise StateCorrupt('Issue implementation changed; use the original source')
    if set(state['papers']) != {p['key'] for p in state['inputs']}:
        raise StateCorrupt('Issue paper set changed')
    for record in state['papers'].values():
        if type(record.get('round')) is not int or record['round'] not in (0, 1) or len(record.get('calls', {})) > 4 or not set(record['calls']) <= {'draft:0', 'review:0', 'draft:1', 'review:1'}:
            raise StateCorrupt('Paper operation budget changed')
        if record.get('status') not in {'ready', 'reviewing', 'qualified', 'rejected'}:
            raise StateCorrupt('Unknown paper state')
    return state


def _save(root, state):
    atomic_json(root / 'state.json', state)


def _source(pdf):
    """Native PDF text once; no OCR, chunk inference, or render-dependent hashes."""
    import fitz
    from daily_agent.page_evidence import ordered_text
    pages = []
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        if not 1 <= len(doc) <= 100:
            raise ValueError('PDF outside bounded page capacity')
        for i, page in enumerate(doc):
            pages.append({'page': i + 1, 'text': ordered_text(page),
                          'width': page.rect.width, 'height': page.rect.height})
    count = sum(len(p['text']) for p in pages)
    if count < 1500 or count > MAX_TEXT:
        raise ValueError('Native text unavailable or beyond capacity; no silent truncation')
    return {'pdf_sha256': _hash(pdf), 'pages': pages, 'character_count': count,
            'extraction': 'native PDF text; no OCR; all pages retained'}


def create_issue(root, issue_date, papers, *, diagnostic=False, target=None):
    """Explicit fresh-root enrollment. Metadata is not formal-venue verification."""
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Use an empty new root; no migration or reopening of old issues')
    day = date.fromisoformat(str(issue_date))
    if day < datetime.now(timezone.utc).date() and not diagnostic:
        raise ValueError('Past-date enrollment is diagnostic only')
    if not isinstance(papers, list) or not 1 <= len(papers) <= MAX_PAPERS:
        raise ValueError('A bounded nonempty paper manifest is required')
    if len({p['key'] for p in papers}) != len(papers):
        raise ValueError('Duplicate paper key')
    prepared, records = [], {}
    # Validate all files before creating the issue. Never copy prior model output.
    for paper in papers:
        published = date.fromisoformat(paper['source_date'])
        if not calendar_month_start(day, 3) <= published <= day:
            raise ValueError('Paper source date is outside three calendar months')
        pdf = Path(paper['pdf_path']).read_bytes()
        if not pdf.startswith(b'%PDF') or len(pdf) > 30 * 1024 * 1024:
            raise ValueError('Invalid or oversized PDF')
        if paper.get('pdf_sha256') and paper['pdf_sha256'] != _hash(pdf):
            raise ValueError('PDF hash does not match manifest')
        source = _source(pdf)
        title = str(paper['title']).strip()
        words = set(re.findall(r'[a-z0-9]{3,}', title.lower()))
        head = set(re.findall(r'[a-z0-9]{3,}', source['pages'][0]['text'].lower()))
        if not title or (words and len(words & head) / len(words) < .5):
            raise ValueError('PDF first page does not match paper title')
        prepared.append((paper, pdf, source))
    if len({_hash(pdf) for _, pdf, _ in prepared}) != len(prepared):
        raise ValueError('Duplicate PDF source identity')
    root.mkdir(parents=True, exist_ok=True)
    inputs = []
    for paper, pdf, source in prepared:
        metadata = {key: str(paper[key]) for key in ('key', 'title', 'version', 'source_date')}
        metadata.update(url=_url(paper['url']), pdf_url=_url(paper['pdf_url']))
        # Default honest label; a separately verified formal venue may be supplied
        # as a preserved primary page snapshot, never a metadata-only label.
        publication = paper.get('publication')
        if publication:
            if set(publication) != {'venue', 'url', 'evidence_path', 'quote'}:
                raise ValueError('Formal venue needs a primary snapshot and exact quote')
            snapshot = Path(publication['evidence_path']).read_bytes()
            if len(snapshot) > 100000:
                raise ValueError('Formal venue evidence exceeds bounded source capacity')
            if publication['quote'] not in snapshot.decode('utf-8'):
                raise ValueError('Formal venue quote absent from snapshot')
            metadata['publication'] = {'venue': publication['venue'], 'url': _url(publication['url']),
                                       'quote': publication['quote'], 'snapshot': _put(root, snapshot)}
        metadata.update(pdf=_put(root, pdf), document=_put(root, _bytes(source)))
        inputs.append(metadata)
        records[paper['key']] = {'status': 'ready', 'round': 0, 'calls': {}, 'failures': [], 'assets': []}
    state = {'protocol': PROTOCOL, 'issue_date': str(day), 'diagnostic': bool(diagnostic),
             'created_at': _now(), 'target': target or len(papers), 'inputs': inputs,
             'implementation_sha256': _hash(Path(__file__).read_bytes()), 'papers': records, 'sealed': None}
    state['binding'] = _hash(_bytes({key: state[key] for key in ('protocol', 'issue_date', 'diagnostic', 'target', 'inputs', 'implementation_sha256')}))
    _save(root, state)
    return status(root)


def _reader_prompt(root, meta, record):
    source = _json(root, meta['document'])
    contract = '''Read the COMPLETE native PDF text below and produce ONE concise Chinese scientific report.
Do not transcribe pages/chunks. Integrate problem, method, insight, results, limits and reuse.
Every published scientific sentence belongs in claims. Separate author_claim from editor_inference.
Keep numeric units, evaluation conditions, denominators, comparison baselines and caveats.
Read appendices too; distinguish evidence from conjecture. No facts from outside this source.
Author context: use names/affiliations from this PDF only, and label group history or correspondence unknown unless explicit.
Select 1-3 useful ORIGINAL visual regions when available: framework, decisive experiment/table, and only a central formula.
No fixed figure count. Prefer fewer legible figures. Coordinates are normalized [x0,y0,x1,y1] within the PDF page.
You may inspect this local source PDF visually to determine accurate crops. Preserve labels, axes, legends and units.
A subpanel must have its exact subpanel label, not the whole-figure label. Do not redraw.
Return a JSON object (no Markdown):
{"read_all_pages":true,"claims":[{"id":"c1","section":"problem|method|insight|results|limits|reuse","text":"Chinese paragraph", "basis":"author_claim|editor_inference","anchors":[{"page":1,"quote":"short exact native-text substring"}],"conditions":"conditions or why not applicable"}],
"figures":[{"id":"f1","role":"framework|result|formula|other","label":"Figure 1(d)","page":2,"bbox":[0.1,0.1,0.9,0.6],"caption":"Chinese explanation with scope/conditions","claim_ids":["c1"]}],
"no_figure_reason":"only if no useful visual exists",
"author_context":{"text":"bounded supported author/context statement; unknowns explicit","anchors":[{"page":1,"quote":"exact source names/affiliations"}]}}
All six claim sections are required. Use 6-12 short paragraphs, usually 1-2 per section. Each claim and author_context must have 1-8 exact page anchors, each 5-1200 characters. Combine nearby evidence into one exact quote when useful; do not create per-sentence evidence logs. Quotes are anchors, not report prose.
'''
    if record['round']:
        contract += '\nREPAIR: Preserve correct reading; fix only failed statements/captions/crops. Return the complete corrected object.\n'
        contract += 'Previous draft: ' + _get(root, record['draft']).decode() + '\n'
        contract += 'Review/mechanical problems: ' + json.dumps(record['failures'][-1], ensure_ascii=False) + '\n'
    return contract + '\nMetadata: ' + json.dumps(meta, ensure_ascii=False) + '\nLocal PDF: ' + meta['pdf'] + '\nFULL SOURCE: ' + json.dumps(source, ensure_ascii=False)


def _anchors(value, pages):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError('Each claim needs 1-8 exact page anchors')
    for anchor in value:
        if not isinstance(anchor, dict):
            raise ValueError('Anchor must be an object')
        page, quote = anchor.get('page'), anchor.get('quote')
        if type(page) is not int or page not in pages or not isinstance(quote, str) or not 5 <= len(quote) <= 1200:
            raise ValueError('Invalid page/quote anchor')
        if ' '.join(quote.split()) not in ' '.join(pages[page].split()):
            raise ValueError(f'Quote not found on page {page}: {quote[:65]}')


def _validate_draft(draft, source):
    if not isinstance(draft, dict) or draft.get('read_all_pages') is not True:
        raise ValueError('Full-paper reading acknowledgment missing')
    claims = draft.get('claims')
    if not isinstance(claims, list) or not 6 <= len(claims) <= 18:
        raise ValueError('Expected 6-18 concise supported paragraphs')
    pages = {p['page']: p['text'] for p in source['pages']}
    ids = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError('Claim must be an object')
        cid = claim.get('id')
        if not isinstance(cid, str) or not re.fullmatch(r'c[1-9][0-9]*', cid) or cid in ids:
            raise ValueError('Invalid/duplicate claim ID')
        ids.add(cid)
        if claim.get('section') not in SECTIONS or claim.get('basis') not in {'author_claim', 'editor_inference'}:
            raise ValueError('Invalid claim section or provenance')
        if not isinstance(claim.get('text'), str) or not 20 <= len(claim['text']) <= 1400:
            raise ValueError('Claim paragraph outside length bound')
        if not isinstance(claim.get('conditions'), str) or not claim['conditions'].strip():
            raise ValueError('Conditions must be explicit')
        _anchors(claim.get('anchors'), pages)
    if {c['section'] for c in claims} != set(SECTIONS):
        raise ValueError('All six explanation sections required')
    figures = draft.get('figures')
    if not isinstance(figures, list) or len(figures) > MAX_FIGURES:
        raise ValueError('At most three useful original figures')
    if not figures and not str(draft.get('no_figure_reason', '')).strip():
        raise ValueError('Missing explanation for no useful figure')
    figure_ids = set()
    for figure in figures:
        if not isinstance(figure, dict):
            raise ValueError('Figure must be an object')
        fid = figure.get('id')
        if not isinstance(fid, str) or not re.fullmatch(r'f[1-9][0-9]*', fid) or fid in figure_ids:
            raise ValueError('Invalid/duplicate figure ID')
        figure_ids.add(fid)
        if figure.get('page') not in pages or type(figure.get('page')) is not int:
            raise ValueError('Invalid figure page')
        box = figure.get('bbox')
        if not isinstance(box, list) or len(box) != 4 or any(type(n) not in (int, float) or not 0 <= n <= 1 for n in box):
            raise ValueError('Invalid normalized crop coordinates')
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError('Empty crop')
        if figure.get('role') not in {'framework', 'result', 'formula', 'other'}:
            raise ValueError('Invalid figure role')
        if not isinstance(figure.get('caption'), str) or not 10 <= len(figure['caption']) <= 1000 or not isinstance(figure.get('label'), str) or not figure['label'].strip():
            raise ValueError('Figure needs a label and bounded explanation')
        if not isinstance(figure.get('claim_ids'), list) or not figure['claim_ids'] or not set(figure['claim_ids']) <= ids:
            raise ValueError('Figure must support known claims')
    author = draft.get('author_context')
    if not isinstance(author, dict) or not isinstance(author.get('text'), str) or not 5 <= len(author['text']) <= 1200:
        raise ValueError('Bounded author context required')
    _anchors(author.get('anchors'), pages)


def _assets(root, meta, draft):
    import fitz
    assets = []
    pdf = _get(root, meta['pdf'])
    for fig in draft['figures']:
        # A new PDF handle for each rendering makes full-page/crop ordering irrelevant.
        with fitz.open(stream=pdf, filetype='pdf') as doc:
            page = doc[fig['page'] - 1]
            full = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).tobytes('png')
        with fitz.open(stream=pdf, filetype='pdf') as doc:
            page = doc[fig['page'] - 1]
            x0, y0, x1, y1 = fig['bbox']
            clip = fitz.Rect(x0 * page.rect.width, y0 * page.rect.height, x1 * page.rect.width, y1 * page.rect.height)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
            if pixmap.width < 24 or pixmap.height < 24:
                raise ValueError('Selected crop is too small to inspect; select a meaningful region')
            crop = pixmap.tobytes('png')
        assets.append({'id': fig['id'], 'page': fig['page'], 'bbox': fig['bbox'],
                       'page_image': _put(root, full), 'crop': _put(root, crop)})
    return assets


def _review_prompt(root, meta, record):
    draft = _json(root, record['draft'])
    claims = [c['id'] for c in draft['claims']]
    figs = [f['id'] for f in draft['figures']]
    prompt = '''Independent reviewer: verify the complete Chinese draft against this full native source.
Focus on scientific meaning, key claims, ALL numbers/units/denominators/baselines/conditions,
author-claim versus editor inference, limitations, scientific insight and bounded author context.
Inspect every supplied full source-page image AND corresponding selected crop. Verify exact label/subpanel,
axes/legend/units are legible and complete; crop and caption must match. No need to transcribe pages.
Check all pages were available and the draft reflects the complete paper, not only the abstract.
A harmless local caption/asset problem means REPAIR, not discarding otherwise valid reading.
Return JSON only:
{"verdict":"PASS|REPAIR|REJECT","full_source_checked":true,
"claims":[{"id":"c1","supported":true,"numbers_and_conditions_checked":true,"basis_correct":true,"reason":"brief source-grounded check"}],
"figures":[{"id":"f1","pixels_inspected":true,"label_matches":true,"crop_complete":true,"caption_supported":true,"reason":"brief visual check"}],
"author_context_supported":true,"figure_selection_appropriate":true,"problems":["specific repair instructions if any"]}
PASS requires all listed checks true, all requested IDs covered once, and no problems.
Do not rubber-stamp; preserve failures. Do not rewrite prose under PASS.
'''
    prompt += '\nRequired claim IDs: ' + json.dumps(claims) + '\nRequired figure IDs: ' + json.dumps(figs)
    prompt += '\nImage order (full page then crop per figure): ' + json.dumps(record['assets'])
    prompt += '\nMetadata: ' + json.dumps(meta, ensure_ascii=False)
    if meta.get('publication'):
        prompt += '\nFORMAL VENUE PRIMARY SNAPSHOT: ' + _get(root, meta['publication']['snapshot']).decode()
        prompt += '\nAlso return publication_supported=true only if this primary publisher/proceedings URL and snapshot establish the claimed formal venue for this paper. Metadata alone is insufficient; otherwise REPAIR/REJECT.\n'
    prompt += '\nDRAFT: ' + json.dumps(draft, ensure_ascii=False)
    prompt += '\nFULL SOURCE: ' + _get(root, meta['document']).decode()
    return prompt


def _review_ok(review, draft, publication_required=False):
    if not isinstance(review, dict) or review.get('verdict') not in {'PASS', 'REPAIR', 'REJECT'}:
        raise ValueError('Invalid independent review')
    if review['verdict'] != 'PASS':
        if not isinstance(review.get('problems'), list) or not review['problems']:
            raise ValueError('Non-PASS review must state problems')
        return False
    if publication_required and review.get('publication_supported') is not True:
        raise ValueError('Formal venue not supported by independent primary-source review')
    if review.get('full_source_checked') is not True or review.get('author_context_supported') is not True or review.get('figure_selection_appropriate') is not True or review.get('problems') != []:
        raise ValueError('PASS lacks required full-source/author/figure checks')
    for field, expected, checks in (
        ('claims', [c['id'] for c in draft['claims']], ('supported', 'numbers_and_conditions_checked', 'basis_correct')),
        ('figures', [f['id'] for f in draft['figures']], ('pixels_inspected', 'label_matches', 'crop_complete', 'caption_supported'))):
        rows = review.get(field)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) or sorted(r.get('id', '') for r in rows) != sorted(expected):
            raise ValueError('Review ID coverage mismatch')
        if any(any(row.get(check) is not True for check in checks) or not str(row.get('reason', '')).strip() for row in rows):
            raise ValueError('PASS contradicts claim/visual check')
    return True


def _request(root, record, role, prompt, images=()):
    slot = f'{role}:{record["round"]}'
    try:
        response = parent_writer.request(root, prompt, 900, image_path=list(images), stage=role)
    except parent_writer.PendingResponse as pending:
        if not pending.job_id:
            raise
        old = record['calls'].get(slot)
        if old and old != pending.job_id:
            raise StateCorrupt('Operation identity changed')
        record['calls'][slot] = pending.job_id
        raise
    # Deterministic request identity; validate provenance stored by real transport.
    contract = parent_writer._request_contract(root, prompt, list(images), role)
    jid = parent_writer.digest(contract)
    if record['calls'].get(slot, jid) != jid:
        raise StateCorrupt('Operation changed after response')
    record['calls'][slot] = jid
    answer = read_json(root / 'data' / 'writer-queue' / f'{jid}.answer.json')
    if not answer or answer.get('response_sha256') != parent_writer.digest(response) or not answer.get('worker_id'):
        raise StateCorrupt('Missing immutable worker provenance')
    return response, answer


def _fail(record, failure, *, terminal=False):
    record['failures'].append(failure)
    if record['round'] == 0 and not terminal:
        record['round'] = 1
        record['status'] = 'ready'
    else:
        record['status'] = 'rejected'


def _advance_paper(root, state, meta):
    record = state['papers'][meta['key']]
    if record['status'] in {'qualified', 'rejected'}:
        return
    try:
        if record['status'] == 'ready':
            response, writer = _request(root, record, 'draft', _reader_prompt(root, meta, record))
            record['draft'] = _put(root, _bytes(response))
            record['writer'] = {k: writer[k] for k in ('worker_id', 'job_id', 'response_sha256')}
            try:
                _validate_draft(response, _json(root, meta['document']))
                record['assets'] = _assets(root, meta, response)
            except (ValueError, KeyError, TypeError) as exc:
                _fail(record, {'stage': 'mechanical', 'problems': [str(exc)]})
                return
            record['status'] = 'reviewing'
            _save(root, state)
        if record['status'] == 'reviewing':
            images = [root / asset[key] for asset in record['assets'] for key in ('page_image', 'crop')]
            review, reviewer = _request(root, record, 'review', _review_prompt(root, meta, record), images)
            record['review'] = _put(root, _bytes(review))
            draft = _json(root, record['draft'])
            try:
                accepted = _review_ok(review, draft, bool(meta.get('publication')))
            except (ValueError, KeyError, TypeError) as exc:
                _fail(record, {'stage': 'review_schema', 'problems': [str(exc)]})
                return
            if not accepted:
                _fail(record, {'stage': 'independent_review', 'review': record['review'], 'problems': review['problems']},
                      terminal=review['verdict'] == 'REJECT')
                return
            if reviewer['worker_id'] == record['writer']['worker_id']:
                raise StateCorrupt('Independent reviewer must differ from reader')
            receipt = {'protocol': PROTOCOL, 'metadata': meta, 'draft': record['draft'],
                       'review': record['review'], 'assets': record['assets'],
                       'writer': record['writer'], 'reviewer': {k: reviewer[k] for k in ('worker_id', 'job_id', 'response_sha256')},
                       'accepted_at': _now(), 'round': record['round'], 'checks': 'mechanical anchors + independent full-source and selected-pixel review'}
            record['receipt'] = _put(root, _bytes(receipt))
            record['status'] = 'qualified'
    except parent_writer.ExpiredResponse as exc:
        _fail(record, {'stage': 'transport', 'problems': ['Immutable job expired'], 'job_id': exc.job_id}, terminal=True)
    except parent_writer.PendingResponse:
        pass
    finally:
        _save(root, state)


def advance(root):
    """One fair pass: every paper progresses independently; no batch barrier."""
    root = Path(root).resolve()
    with exclusive_lock(root / 'issue.lock'):
        state = _load(root)
        if state['sealed']:
            return status(root)
        for meta in state['inputs']:
            _advance_paper(root, state, meta)
    return status(root)


def status(root):
    root = Path(root).resolve()
    state = _load(root)
    records = state['papers']
    return {'protocol': PROTOCOL, 'issue_date': state['issue_date'], 'target': state['target'],
            'qualified': sum(r['status'] == 'qualified' for r in records.values()),
            'rejected': sum(r['status'] == 'rejected' for r in records.values()),
            'pending': sum(r['status'] not in {'qualified', 'rejected'} for r in records.values()),
            'model_jobs': sum(len(r['calls']) for r in records.values()),
            'papers': {k: {'status': v['status'], 'round': v['round'], 'failures': v['failures']} for k, v in records.items()},
            'sealed': state['sealed']}


def _verified_receipt(root, meta, record):
    """Cheap final hash/binding check. No repeated PDF render or semantic review."""
    receipt = _json(root, record['receipt'])
    if receipt['protocol'] != PROTOCOL or receipt['metadata'] != meta or receipt['draft'] != record['draft'] or receipt['review'] != record['review'] or receipt['assets'] != record['assets']:
        raise StateCorrupt('Qualification receipt binding changed')
    if receipt['writer']['worker_id'] == receipt['reviewer']['worker_id']:
        raise StateCorrupt('Independent provenance changed')
    for name in ('pdf', 'document'):
        _get(root, meta[name])
    if meta.get('publication'):
        _get(root, meta['publication']['snapshot'])
    draft, review = _json(root, receipt['draft']), _json(root, receipt['review'])
    if review.get('verdict') != 'PASS':
        raise StateCorrupt('Qualification lost independent PASS')
    for asset in receipt['assets']:
        for key in ('page_image', 'crop'):
            _get(root, asset[key])
    # Verify archived actual queue answers, not just caller-written PASS labels.
    for key, value in (('writer', draft), ('reviewer', review)):
        proof = receipt[key]
        jid = proof['job_id']
        if not isinstance(jid, str) or not re.fullmatch(r'[0-9a-f]{64}', jid):
            raise StateCorrupt('Invalid qualification job identity')
        job = read_json(root / 'data' / 'writer-queue' / f'{jid}.job.json')
        parent_writer.validate_job(root, job)
        if job['stage'] != ('draft' if key == 'writer' else 'review'):
            raise StateCorrupt('Qualification job role changed')
        answer = read_json(root / 'data' / 'writer-queue' / f'{jid}.answer.json')
        if not answer or answer.get('job_id') != jid or answer.get('input_sha256') != jid or answer['worker_id'] != proof['worker_id'] or answer['response_sha256'] != proof['response_sha256'] or parent_writer.digest(value) != answer['response_sha256'] or answer['response'] != value:
            raise StateCorrupt('Qualification worker answer changed')
    return receipt, draft


def render(root, *, seal=False):
    root = Path(root).resolve()
    with exclusive_lock(root / 'issue.lock'):
        state = _load(root)
        if state['sealed']:
            path = root / state['sealed']['path']
            if _hash(path.read_bytes()) != state['sealed']['sha256']:
                raise StateCorrupt('Sealed HTML changed')
            return path
        sections = []
        qualified = 0
        for meta in state['inputs']:
            record = state['papers'][meta['key']]
            if record['status'] != 'qualified':
                continue
            receipt, draft = _verified_receipt(root, meta, record)
            qualified += 1
            venue = (meta.get('publication') or {}).get('venue', '预印本／正式发表状态未独立确认')
            parts = [f'<article id="paper-{qualified}"><h2>{escape(meta["title"])}</h2>',
                     f'<p class="meta">{escape(meta["source_date"])} · {escape(meta["version"])} · {escape(venue)}</p>',
                     f'<p><a href="{escape(meta["url"], quote=True)}">论文原文</a> · <a href="{escape(meta["pdf_url"], quote=True)}">PDF</a></p>',
                     f'<p class="authors">{escape(draft["author_context"]["text"])}</p>']
            for role in SECTIONS:
                parts.append(f'<h3>{LABELS[role]}</h3>')
                for claim in draft['claims']:
                    if claim['section'] != role:
                        continue
                    links = ' '.join(f'<a href="{escape(meta["pdf_url"], quote=True)}#page={a["page"]}">p.{a["page"]}</a>' for a in claim['anchors'])
                    prefix = '编者推断：' if claim['basis'] == 'editor_inference' else ''
                    parts.append(f'<p>{prefix}{escape(claim["text"])} <span class="refs">{links}</span></p>')
                    if ' '.join(claim['conditions'].split()) not in ' '.join(claim['text'].split()):
                        parts.append(f'<p class="conditions">适用条件：{escape(claim["conditions"])}</p>')
            assets = {a['id']: a for a in receipt['assets']}
            for figure in draft['figures']:
                image = base64.b64encode(_get(root, assets[figure['id']]['crop'])).decode()
                parts.append(f'<figure><img alt="{escape(figure["label"], quote=True)}" src="data:image/png;base64,{image}"><figcaption>{escape(figure["label"])} · {escape(figure["caption"])} <a href="{escape(meta["pdf_url"], quote=True)}#page={figure["page"]}">原文 p.{figure["page"]}</a></figcaption></figure>')
            parts.append('</article>')
            sections.append(''.join(parts))
        if seal and not qualified:
            raise ValueError('Cannot seal a zero-qualified issue')
        st = status(root)
        title = f'{state["issue_date"]} 研究简报' + (' · 隔离验证' if state['diagnostic'] else '')
        html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        html += f'<title>{escape(title)}</title><style>body{{margin:0;background:#f4f6f8;color:#1d2939;font:17px/1.85 system-ui,sans-serif}}main{{max-width:960px;margin:auto;padding:36px 20px}}article{{background:white;border-radius:14px;padding:32px;margin:28px 0}}h1{{font-size:32px}}h2{{font-size:25px;line-height:1.4}}h3{{font-size:18px;margin-bottom:6px}}p{{margin:10px 0 20px}}a{{color:#245c9e}}.meta,.authors,figcaption,.refs{{color:#667085;font-size:14px}}img{{max-width:100%;height:auto}}figure{{margin:28px 0}}.coverage{{background:#e9eef5;padding:14px 20px;border-radius:10px}}@media(max-width:600px){{article{{padding:20px}}main{{padding:20px 12px}}}}</style><main>'
        html += f'<h1>{escape(title)}</h1><p class="coverage">已独立核验 {qualified} 篇论文；目标 {state["target"]} 篇。另有 {st["pending"]} 篇待完成、{st["rejected"]} 篇未通过。本页仅呈现合格内容。</p>'
        html += ''.join(sections) + '</main></html>'
        path = root / ('report.html' if seal else 'preview.html')
        path.write_text(html, encoding='utf-8')
        if seal:
            state['sealed'] = {'path': path.name, 'sha256': _hash(html.encode()), 'qualified': qualified, 'sealed_at': _now()}
            _save(root, state)
        return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--root', required=True)
    init.add_argument('--date', required=True)
    init.add_argument('--manifest', required=True)
    init.add_argument('--diagnostic', action='store_true')
    for name in ('advance', 'status', 'render', 'seal'):
        command = sub.add_parser(name)
        command.add_argument('--root', required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    if args.command == 'init':
        result = create_issue(args.root, args.date, json.loads(Path(args.manifest).read_text()), diagnostic=args.diagnostic)
    elif args.command == 'advance':
        result = advance(args.root)
    elif args.command == 'status':
        result = status(args.root)
    else:
        result = {'html': str(render(args.root, seal=args.command == 'seal'))}
    print(json.dumps({'result': result, 'local_seconds': round(time.monotonic() - started, 3)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
