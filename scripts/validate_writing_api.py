#!/usr/bin/env python3
"""Bounded real-API writing acceptance on a fixed local corpus; never publishes."""
from __future__ import annotations
import argparse
import base64
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from threading import Lock
import time
from urllib.parse import urlsplit
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from daily_agent.config import load_config
from daily_agent.editorial import _llm_prompt, _validated_llm_drafts, review_draft, approve_publication
from daily_agent.models import MaterialRecord
from daily_agent.models import RunStatus
from daily_agent.paper_document import atomic_json, digest, extract_document, attach_document
from daily_agent.reading import read_papers, valid_note, verify_draft, semantic_review
from daily_agent.rendering.composition import paper_paragraphs, featured_keys
from daily_agent.rendering.notes import write_reading_notes
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.rendering.html import render_daily_html
from daily_agent.storage import write_editorial_artifacts, write_daily_report, write_daily_html_report


def credentials(path):
    text = path.read_text()
    def field(name):
        match = re.search(rf'{name}\s*[:=：]\s*(\S+)', text, re.I)
        if not match:
            raise ValueError(f'Missing {name} in credential file')
        return match.group(1).strip('`"\'')
    base, key = field('baseurl').rstrip('/'), field('apikey')
    if urlsplit(base).scheme != 'https':
        raise ValueError('API URL must use HTTPS')
    if not base.endswith('/chat/completions'):
        base += '/chat/completions' if base.endswith('/v1') else '/v1/chat/completions'
    return base, key


class ChatAPI:
    def __init__(self, url, key, model, folder, max_calls=80, stream=False):
        self.url, self.key, self.model, self.folder = url, key, model, folder
        self.stream = stream
        self.lock, self.calls, self.max_calls = Lock(), 0, max_calls
        folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, prompt, timeout=180, image_path=None):
        image_paths = image_path if isinstance(image_path, list) else [image_path] if image_path else []
        images = [Path(path).read_bytes() for path in image_paths]
        hashes = [hashlib.sha256(value).hexdigest() for value in images]
        image_hash = hashes[0] if len(hashes) == 1 else hashes if hashes else None
        identity = digest([self.url, self.model, prompt, image_hash]) if image_hash else digest([self.url, self.model, prompt])
        path = self.folder / (identity + '.json')
        if path.exists():
            return json.loads(path.read_text())['parsed']
        with self.lock:
            if self.calls >= self.max_calls:
                raise RuntimeError('API call budget exhausted')
            self.calls += 1
            call = self.calls
        started = time.monotonic()
        # Never put the credential into CLI arguments, output, persisted config or exceptions.
        content = prompt
        if images:
            content = [{'type': 'text', 'text': prompt}] + [
                {'type': 'image_url', 'image_url': {
                    'url': 'data:image/png;base64,' + base64.b64encode(value).decode(), 'detail': 'high'}}
                for value in images]
        body = {'model': self.model, 'messages': [{'role': 'user', 'content': content}],
                'stream': self.stream, 'max_tokens': 12000}
        headers = {'Authorization': 'Bearer ' + self.key}
        try:
            if self.stream:
                pieces, finish, usage, response_id = [], None, None, None
                with httpx.stream('POST', self.url, headers=headers, json=body, timeout=timeout) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f'API HTTP {response.status_code}')
                    for line in response.iter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            break
                        event = json.loads(data)
                        if 'error' in event:
                            raise RuntimeError('API stream error')
                        response_id = event.get('id') or response_id
                        usage = event.get('usage') or usage
                        for choice in event.get('choices', []):
                            if choice.get('index', 0) != 0:
                                continue
                            piece = choice.get('delta', {}).get('content')
                            if isinstance(piece, str):
                                pieces.append(piece)
                            finish = choice.get('finish_reason') or finish
                payload = {'id':response_id, 'usage':usage,
                           'choices':[{'finish_reason':finish, 'message':{'content':''.join(pieces)}}]}
            else:
                response = httpx.post(self.url, headers=headers, json=body, timeout=timeout)
                if response.status_code != 200:
                    raise RuntimeError(f'API HTTP {response.status_code}')
                payload = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(type(exc).__name__) from None
        choice = payload['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise ValueError('Model response did not finish normally')
        raw = choice['message']['content'].strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
        parsed = json.loads(raw)
        if self.key in raw:
            raise ValueError('Refusing to persist credential-bearing output')
        atomic_json(path, {'prompt': prompt, 'parsed': parsed, 'model': self.model,
                          'image_sha256': image_hash, 'response_id': payload.get('id'), 'usage': payload.get('usage'),
                          'seconds': round(time.monotonic()-started, 2)})
        print(f'API call {call} complete ({round(time.monotonic()-started)}s)', flush=True)
        return parsed



def assessment_passed(value):
    if not isinstance(value, dict) or value.get('passed') is not True:
        return False
    issues = value.get('issues')
    return isinstance(issues, list) and all(isinstance(issue, dict)
        and issue.get('severity') == 'minor' and isinstance(issue.get('reason'), str)
        for issue in issues)


def check_reading(record):
    chunks = {c['id']: c for c in record.paper_document.get('chunks', [])}
    notes = record.reading.get('notes', [])
    return bool(chunks) and len(notes) == len(chunks) and len({n['chunk_id'] for n in notes}) == len(chunks) and all(
        n['chunk_id'] in chunks and valid_note(n, chunks[n['chunk_id']], {}) for n in notes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, default=ROOT/'secret.md')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--reuse-reading', type=Path, required=True)
    parser.add_argument('--paper-key', action='append', required=True)
    parser.add_argument('--repo-key', action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='gpt-6-sol')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--stream', action='store_true', help='Use SSE to avoid gateway idle timeouts')
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT/'tmp' not in output.parents:
        parser.error('Output must be under project tmp/')
    if output.exists() and not args.resume:
        parser.error('Output exists; use --resume for identical fixed inputs')
    url, key = credentials(args.credentials)
    snapshot = args.input.read_bytes()
    reuse = args.reuse_reading.read_bytes()
    manifest = {'input_sha256': hashlib.sha256(snapshot).hexdigest(),
                'reading_sha256': hashlib.sha256(reuse).hexdigest(),
                'paper_keys': args.paper_key, 'repo_keys': args.repo_key,
                'model': args.model, 'endpoint_hash': digest(url)}
    if output.exists() and json.loads((output/'manifest.json').read_text()) != manifest:
        parser.error('Resume input or model identity mismatch')
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output/'manifest.json', manifest)
    api = ChatAPI(url, key, args.model, output/'api-calls', stream=args.stream)
    rows = {r['key']: r['material'] for r in json.loads(snapshot)}
    reusable = {r['key']: r['material'] for r in json.loads(reuse)}
    config = load_config(ROOT)
    object.__setattr__(config, 'root', output)
    config.sources['reading'] = {**config.sources.get('reading', {}), 'run_budget_seconds': 900,
                                  'timeout_seconds': 150, 'concurrent_reads': 3}
    records = []
    for paper_key in args.paper_key:
        record = MaterialRecord.from_dict(reusable.get(paper_key, rows[paper_key]))
        if not check_reading(record):
            pdf = Path(record.raw.get('local_pdf_path') or '')
            if not pdf.is_file():
                raise ValueError('Selected paper has no local PDF')
            doc = extract_document(pdf.read_bytes(), record, record.pdf_url or record.url,
                {**config.sources.get('paper_text', {}), 'chunk_chars': 8000,
                 'page_image_dir': str(output/'pages'/digest(paper_key)[:12])})
            attach_document(record, doc)
            print(f'Reading {record.title}: {len(doc["chunks"])} chunks', flush=True)
            read_papers([record], config, api)
            record.reading['visual'] = {'required_pages': len(doc['pages']), 'complete': False,
                                       'passed': False, 'notes': []}
            record.paper_text_status['sufficient_for_deep_summary'] = False
        print(f'Writing input {record.title}: {len(record.reading.get("notes", []))} notes', flush=True)
        records.append(record)
    records.extend(MaterialRecord.from_dict(rows[k]) for k in args.repo_key)
    atomic_json(output/'reading-inputs.json', [r.to_dict() for r in records])
    drafts = []
    for record in records:
        print('Drafting '+record.title, flush=True)
        prompt = _llm_prompt([record], max_input_chars_per_item=24000)
        draft = _validated_llm_drafts(api(prompt, 240), {record.key: record})[0]
        atomic_json(output/'drafts'/f'{digest(record.key)[:12]}-raw.json', draft.to_dict())
        if record.item_type == 'paper':
            verify_draft(draft, record)
            semantic_review(draft, record, api, 180)
            issues = draft.verification.get('issues', [])
            repairable = [v for v in issues if any(v.startswith(f+':') for f in ['problem','method','why_it_works','novelty_or_difference','key_result','limitations','technical_route','method_steps','possible_use_or_impact'])]
            if repairable:
                print('One bounded rewrite: '+record.title, flush=True)
                feedback = ('\n上一版被证据审核拒绝。请输出同结构完整JSON数组；只写引句直接支持的最小事实，'
                            '多事实附多条逐字引句。不能把conditions当作证据，不能补齐引句没有的数字。'
                            '若无证据则写not_stated。具体反馈：'+json.dumps(repairable,ensure_ascii=False))
                revised = _validated_llm_drafts(api(prompt+feedback, 240), {record.key: record})[0]
                atomic_json(output/'drafts'/f'{digest(record.key)[:12]}-rewrite-raw.json', revised.to_dict())
                verify_draft(revised, record)
                semantic_review(revised, record, api, 180)
                def score(d):
                    valid = set(d.verification.get('valid_fields', []))
                    return (d.verification.get('semantic_support') == 'model_checked', {'problem','method'} <= valid,
                            'key_result' in valid, len(valid))
                if score(revised) > score(draft):
                    draft = revised
                record.reading['verification'] = draft.verification
        drafts.append(draft)
    reviews = review_draft(config, drafts, True)
    approved = approve_publication(config, records, drafts, reviews)
    today = date.today()
    for item in approved:
        if item.item_type != 'paper':
            continue
        source_pdf = Path(item.material.raw.get('local_pdf_path') or '')
        if source_pdf.is_file():
            target = output/'data'/'pdfs'/today.isoformat()/(digest(item.key)[:16]+'.pdf')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_pdf, target)
            item.material.raw['local_pdf_report_url'] = '../'+target.relative_to(output).as_posix()
        else:
            item.material.raw.pop('local_pdf_report_url', None)
    write_reading_notes(config, approved, today, True)
    write_editorial_artifacts(config, today, records, drafts, reviews, approved)
    status = RunStatus(fallback='固定3篇论文与1个项目的真实API写作验收；未外发，非实时采集，图表/公式未通过严格验收者保留降级。')
    settings = config.sources.get('report_writing', {})
    insights = config.sources.get('insights', {})
    markdown = render_daily_markdown(approved, today, status, insights, settings)
    html = render_daily_html(approved, today, status, insights, settings)
    md_path = write_daily_report(config, today, markdown)
    html_path = write_daily_html_report(config, today, html)
    featured = featured_keys(approved, settings)
    audit = [{'key': item.key, 'featured': item.key in featured,
              'paragraphs': [asdict(p) for p in paper_paragraphs(item, item.key in featured)]}
             for item in approved if item.item_type == 'paper']
    atomic_json(output/'composition-audit.json', audit)
    assessment = api('你是日报终审编辑。禁止工具。对以下固定素材日报做独立质量验收。'
        '只依据所给批准字段和证据，不引入外部事实。检查段落是否遗漏改变结论的条件、预测是否误写实测、'
        '编辑启发是否冒充验证结果、跨论文归纳是否有依据、信息重复是否严重、结果缺失是否明确。'
        '输出JSON对象：passed布尔、issues数组(每项severity为critical/major/minor,key,reason), strengths数组。'
        '只有严重事实错误/误导或不能帮助读者判断的major问题才使passed=false。低置信度且明确披露本身不算失败。'
        '输入：\n'+json.dumps({'report':markdown,'approved':[{'key':i.key,'fields':i.final_fields,'evidence':i.material.reading.get('claim_evidence',[])} for i in approved]},ensure_ascii=False), 240)
    atomic_json(output/'editorial-assessment.json', assessment)
    papers = [i for i in approved if i.item_type == 'paper']
    result = {'model':args.model, 'api_calls_this_run':api.calls,'approved_papers':len(papers),
              'approved_repos':len(approved)-len(papers),
              'text_reading_complete':all(check_reading(r) for r in records if r.item_type=='paper'),
              'semantics_checked':all(i.material.reading.get('verification',{}).get('semantic_support')=='model_checked' for i in papers),
              'results_supported':all('key_result' in i.material.reading.get('verification',{}).get('valid_fields',[]) for i in papers),
              'editorial_passed':assessment_passed(assessment),
              'strict_fulltext_fidelity_passed':False, 'formal_publication':False,
              'reports':[str(md_path.relative_to(output)),str(html_path.relative_to(output))]}
    result['writing_acceptance_passed'] = (len(papers)==len(args.paper_key) and len(approved)==len(records)
        and result['text_reading_complete'] and result['semantics_checked']
        and result['results_supported'] and result['editorial_passed'])
    atomic_json(output/'acceptance.json', result)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return 0 if result['writing_acceptance_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
