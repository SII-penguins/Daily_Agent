"""Versioned, position-preserving paper evidence. No model claims in this layer."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from html.parser import HTMLParser

SCHEMA = 7
NUMBERED_HEADING = re.compile(r'^\s*(?:[1-9](?:\.\d+)*|[IVX]+)[.\s]+[A-Za-z][A-Za-z ,:/()–—-]{2,90}$')
HEADINGS = re.compile(r'^\s*(?:(?:\d+(?:\.\d+)*|[IVX]+)[.\s]+)?(abstract|introduction|background|related work|methods?|methodology|approach|experimental setup|experiments?|evaluation|results?(?: and discussion)?|discussion(?: and conclusions?)?|limitations?|conclusions?(?: and (?:outlook|discussion))?|references|appendix|supplementary material)\s*[:.]?\s*$', re.I)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def evidence_settings(settings):
    """Scheduling changes must not invalidate already verified evidence."""
    return {key:value for key, value in settings.items()
            if not key.endswith(('_budget_seconds', '_timeout_seconds'))
            and key not in {'timeout_seconds', 'concurrent_reads'}}


def version_identity(record) -> str:
    urls = ' '.join(str(x or '') for x in [record.url, record.pdf_url])
    match = re.search(r'(\d{4}\.\d{4,5})v(\d+)', urls)
    version = match.group(0) if match else record.raw.get('arxiv_version')
    return digest([record.key, version, record.doi])


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def section_kind(title: str) -> str:
    t = title.lower()
    if re.search(r'method|approach|architecture|framework|algorithm|protocol|implementation', t): return 'method'
    if re.search(r'result|experiment|evaluation|scaling|performance|benchmark', t): return 'results'
    if 'limitation' in t: return 'limitations'
    if 'discussion' in t: return 'discussion'
    if 'conclusion' in t: return 'conclusion'
    return t


def build_document(record, pages: list[dict], source_url: str | None, source_type: str,
                   settings: dict | None = None, problems: list[str] | None = None) -> dict:
    cfg = settings or {}
    cap = int(cfg.get('max_raw_text_chars', 300000))
    chunk_size = max(256, int(cfg.get('chunk_chars', 10000)))
    issues = list(problems or [])
    kept, used = [], 0
    for page in pages:
        text = page.get('text', '')
        if not text.strip(): issues.append(f"第 {page.get('page', '?')} 页无可提取文本")
        portion = text[:max(0, cap-used)]
        if len(portion) < len(text): issues.append('正文超过字符上限，尾部未解析')
        kept.append({**page, 'text': portion}); used += len(portion)
    text = '\n'.join(p['text'] for p in kept)
    sections, chunks = [], []
    title, kind = '正文开头', 'preamble'
    for page in kept:
        offset = 0
        for line in page['text'].splitlines(keepends=True):
            heading = HEADINGS.match(line.strip())
            if (heading or (line.strip() in page.get('headings', [])
                            and section_kind(line.strip()) in {'method', 'results', 'discussion', 'conclusion', 'limitations', 'references'})
                    or (NUMBERED_HEADING.match(line.strip()) and not re.search(r'\S {4,}\S',line.strip()))):
                title = line.strip(); kind = section_kind(heading.group(1) if heading else title)
                sections.append({'title': title, 'kind': kind, 'page': page.get('page')})
            start = 0
            while start < len(line):
                if not (chunks and chunks[-1]['page'] == page.get('page') and chunks[-1]['section'] == title
                        and len(chunks[-1]['text']) < chunk_size):
                    chunks.append({'id': f'c{len(chunks)+1:04d}', 'page': page.get('page'),
                                   'section': title, 'kind': kind, 'offset': offset+start, 'text': ''})
                capacity = chunk_size-len(chunks[-1]['text'])
                part = line[start:start+capacity]
                chunks[-1]['text'] += part
                start += len(part)
            offset += len(line)
    chunks = [c for c in chunks if c['text'].strip()]
    kinds = {s['kind'] for s in sections}
    title_words = set(re.findall(r'[a-z0-9]{3,}', record.title.lower()))
    head_words = set(re.findall(r'[a-z0-9]{3,}', text[:5000].lower()))
    title_match = not title_words or len(title_words & head_words)/len(title_words) >= float(cfg.get('title_match_ratio', .5))
    structural = 'method' in kinds and 'results' in kinds and bool(kinds & {'conclusion','discussion','references'})
    is_body = structural and len(text) >= int(cfg.get('min_body_chars', 3000)) and title_match
    if not title_match: issues.append('正文开头与论文标题未匹配，身份待核验')
    if source_type == 'legacy': issues.append('旧片段没有页码和完整性记录，不能证明全文')
    document_kind = 'full_text' if is_body and not issues else 'partial_text'
    if not text.strip(): document_kind = 'unavailable'
    elif source_type == 'html' and not structural: document_kind = 'abstract_only'
    elif source_type == 'abstract': document_kind = 'abstract_only'
    if not structural and text.strip(): issues.append('方法/结果/结尾结构未完整识别，不能确认正文完整性')
    issues.append('图表图像、公式视觉保真及外部补充材料未核验')
    return {'schema_version': SCHEMA, 'identity': version_identity(record), 'content_hash': digest(text),
            'source_url': source_url, 'source_type': source_type, 'document_kind': document_kind,
            'title_match': title_match, 'pages': kept, 'sections': sections, 'chunks': chunks,
            'coverage': {'parsed_pages': sum(bool(p['text'].strip()) for p in kept), 'total_pages': len(pages),
                         'char_count': len(text), 'sections_found': sorted(kinds)},
            'limitations': list(dict.fromkeys(issues))}


class StructuredHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in {'script','style','nav','footer','header','aside'}: self.skip += 1
        if tag in {'p','div','section','article','br','h1','h2','h3','h4','li','tr'}: self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in {'script','style','nav','footer','header','aside'}: self.skip = max(0,self.skip-1)
        if tag in {'td','th'} and not self.skip: self.parts.append('\t')
        if tag in {'p','div','section','article','h1','h2','h3','h4','li','tr'}: self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def extract_document(content: bytes, record, url: str, settings: dict) -> dict:
    if content.startswith(b'%PDF'):
        pages, errors = [], []
        source_page_count = None
        try:
            import fitz
            with fitz.open(stream=content, filetype='pdf') as pdf:
                source_page_count = len(pdf)
                from daily_agent.page_evidence import extract_pages
                pages, errors = extract_pages(pdf, settings)
        except Exception:
            try:
                from pypdf import PdfReader
                from io import BytesIO
                reader = PdfReader(BytesIO(content))
                source_page_count = len(reader.pages)
                for i, page in enumerate(reader.pages):
                    try: value = page.extract_text() or ''
                    except Exception: value = ''; errors.append(f'第 {i+1} 页解析失败')
                    pages.append({'page': i+1, 'text': value, 'visual_required': True})
            except Exception: errors.append('PDF 解析器不可用或文件损坏')
        document = build_document(record, pages, url, 'pdf', settings, errors)
        document['source_pdf_sha256'] = hashlib.sha256(content).hexdigest()
        document['source_page_count'] = source_page_count
        return document
    if not re.search(rb'<(?:html|body|article|main)\b', content[:4096], re.I):
        return build_document(record, [], url, 'html', settings, ['响应不是可识别的论文文档'])
    parser = StructuredHTML()
    try: parser.feed(content.decode('utf-8', errors='replace'))
    except Exception: pass
    return build_document(record, [{'page': None, 'text': ''.join(parser.parts)}], url, 'html', settings)


def attach_document(record, document):
    record.paper_document = document
    text = '\n'.join(p['text'] for p in document['pages'])
    record.paper_text_excerpt = text
    kinds = document['coverage']['sections_found']
    record.paper_text_status = {'status': 'available' if text.strip() else 'unavailable', 'available': bool(text.strip()), 'source_type': document['source_type'],
        'source_url': document['source_url'], 'document_kind': document['document_kind'],
        'sections_found': kinds, 'missing_sections': [s for s in ['method','results'] if s not in kinds],
        'sufficient_for_deep_summary': False, 'raw_char_count': len(text), 'excerpt_char_count': len(text),
        'section_notes': {}, 'section_note_count': 0}


def valid_document(value, identity):
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA or value.get('identity') != identity:
        return False
    try:
        pages, chunks = value['pages'], value['chunks']
        if not isinstance(pages,list) or not isinstance(chunks,list): return False
        if not all(isinstance(p,dict) and isinstance(p.get('text'),str) for p in pages): return False
        if not all(isinstance(c,dict) and isinstance(c.get('id'),str) and isinstance(c.get('text'),str) for c in chunks): return False
        if len({c['id'] for c in chunks}) != len(chunks): return False
        # A page hash alone cannot authenticate derived chunks. Check their
        # exact spans and ensure no non-whitespace source text was omitted.
        page_text = {p['page']: p['text'] for p in pages}
        if len(page_text) != len(pages): return False
        ends = {number: 0 for number in page_text}
        for chunk in chunks:
            number, start = chunk['page'], chunk['offset']
            if number not in page_text or type(start) is not int: return False
            source, previous = page_text[number], ends[number]
            if start < previous or start > len(source) or not chunk['text'].strip(): return False
            if source[previous:start].strip(): return False
            end = start + len(chunk['text'])
            if source[start:end] != chunk['text']: return False
            ends[number] = end
        if any(source[ends[number]:].strip() for number, source in page_text.items()): return False
        return (digest('\n'.join(p['text'] for p in pages)) == value['content_hash']
                and value['document_kind'] in {'full_text','partial_text','abstract_only','unavailable'}
                and isinstance(value['coverage']['sections_found'],list)
                and isinstance(value['limitations'],list))
    except (KeyError,TypeError):
        return False
