"""Offline, resumable calibration of one verified paper and one repository.

Inputs must already have been downloaded through an authorized read route.
This runner never collects live sources, sends a report, or commits history.
"""
from pathlib import Path
from datetime import date
import argparse
import hashlib
import json
import re
from bs4 import BeautifulSoup
from daily_agent.config import load_config
from daily_agent.models import DigestItem, MaterialRecord, RunStatus, SourceStatus
from daily_agent.parent_writer import PendingResponse
from daily_agent.workflow_state import atomic_json, read_json


def build(root):
    root=Path(root).resolve();cfg=load_config(root)
    if not cfg.delivery.get('cloud',{}).get('pilot'): raise ValueError('Dedicated pilot profile required')
    folder=root/'data/pilot-inputs'
    soup=BeautifulSoup((folder/'paper.html').read_text(),'html.parser')
    def meta(name):
        tag=soup.find('meta',attrs={'name':name});return tag.get('content') if tag else None
    title=meta('citation_title') or soup.title.get_text()
    url=meta('citation_abstract_html_url') or 'https://proceedings.mlr.press/v306/wu26m.html'
    pdf_url=meta('citation_pdf_url')
    if not url.startswith('https://proceedings.mlr.press/') or not pdf_url: raise ValueError('Verified official source metadata required')
    abstract=soup.select_one('#abstract')
    item=DigestItem(id='v306/wu26m',source='pmlr',item_type='paper',title=title,url=url,pdf_url=pdf_url,
        abstract=abstract.get_text(' ',strip=True) if abstract else None,
        raw={'venue':meta('citation_conference_title') or 'ICML 2026','pmlr_url':url},categories=['ICML 2026'],
        quota_group='exploratory',score=100,source_tags=['vla'])
    record=MaterialRecord.from_item(item)
    content=(folder/'paper.pdf').read_bytes()
    if not content.startswith(b'%PDF'): raise ValueError('Not a PDF')
    import fitz
    with fitz.open(stream=content,filetype='pdf') as pdf:
        normalize=lambda x: re.sub(r'\W+','',x).lower()
        if normalize(title) not in normalize(pdf[0].get_text()): raise ValueError('PDF title does not match official record')
    from daily_agent.paper_document import extract_document, attach_document
    settings={**cfg.sources.get('paper_text',{}), **cfg.sources.get('reading',{}),
              'page_image_dir':str(root/'data/pilot-inputs/pages')}
    document=extract_document(content,record,pdf_url,settings)
    document['source_pdf_path']=str(folder/'paper.pdf')
    attach_document(record,document)
    record.raw['source_verification']={'url':url,'html_sha256':hashlib.sha256((folder/'paper.html').read_bytes()).hexdigest(),
                                     'pdf_sha256':hashlib.sha256(content).hexdigest(),'title_matched':True}
    repo=json.loads((folder/'repo.json').read_text())
    github=DigestItem(id=repo['full_name'],source='github',item_type='repo',title=repo['full_name'],url=repo['html_url'],
                     repo_description=repo.get('description'),stars=repo['stargazers_count'],language=repo['language'],
                     raw=repo,quota_group='exploratory',score=90)
    repository=MaterialRecord.from_item(github); repository.readme_excerpt=(folder/'readme.md').read_text()
    atomic_json(folder/'materials.json',[record.to_dict(),repository.to_dict()])
    return {'title':title,'pages':len(document['pages']),'chunks':len(document['chunks']),'document_kind':document['document_kind'],
            'repo':repo['full_name'],'calibration_only':True}


def run(root,day):
    root=Path(root).resolve();cfg=load_config(root)
    if not cfg.delivery.get('cloud',{}).get('pilot') or cfg.sources['llm_writer']['provider']!='parent_queue':
        raise ValueError('Explicit parent-assisted pilot only')
    records=[MaterialRecord.from_dict(row) for row in read_json(root/'data/pilot-inputs/materials.json')]
    from daily_agent.editorial import draft_report_items,review_draft,approve_publication
    drafts=draft_report_items(cfg,records,use_llm=True)
    reviews=review_draft(cfg,drafts,use_llm=True)
    approved=approve_publication(cfg,records,drafts,reviews)
    from daily_agent.source_evidence_policy import audit_reading
    from daily_agent.storage import write_editorial_artifacts,write_daily_report,write_run_log
    from daily_agent.rendering.markdown import render_daily_markdown
    status=RunStatus(sources=[SourceStatus(name='PMLR official pilot snapshot',ok=True,item_count=1),
                             SourceStatus(name='GitHub official pilot snapshot',ok=True,item_count=1)],
                     fallback='迁移验收样例，非今日新闻；未运行全源抓取，不进入正式发布历史')
    write_editorial_artifacts(cfg,day,records,drafts,reviews,approved)
    write_daily_report(cfg,day,render_daily_markdown(approved,day,status))
    write_run_log(cfg,day,status,dry_run=False)
    from daily_agent.scheduling import seal_ready_report
    seal=seal_ready_report(cfg,day)
    result={'approved_count':len(approved),'audit':audit_reading([item.to_dict() for item in approved]),'sealed':bool(seal)}
    atomic_json(root/'data/pilot-result.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['build','run','worker']);p.add_argument('--root',required=True);p.add_argument('--date');a=p.parse_args()
    from daily_agent.cloud_workflow import _local_day,run_generation
    day=_local_day(a.root,a.date)
    if a.action=='run':
        rc=run_generation(a.root,day,900,worker_module='daily_agent.cloud_pilot')
        result={'state':'awaiting_parent_writer' if rc==75 else 'finished' if rc==0 else 'stopped', 'returncode':rc}
        print(json.dumps(result));return rc
    try: result=build(a.root) if a.action=='build' else run(a.root,day)
    except PendingResponse as exc:
        print(json.dumps({'state':'expired_parent_writer' if getattr(exc,'expired',False) else 'awaiting_parent_writer','job_id':exc.job_id,'issue_date':str(day)}));return 75
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__': raise SystemExit(main())
