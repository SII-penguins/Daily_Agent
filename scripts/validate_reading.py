#!/usr/bin/env python3
"""Isolated, explicit live-model acceptance. Never collects sources or delivers reports."""
from __future__ import annotations
import argparse
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from daily_agent.config import load_config
from daily_agent.editorial import _llm_writer_command,_llm_writer_settings,_decode_model_json,draft_report_items,review_draft,approve_publication
from daily_agent.models import MaterialRecord,RunStatus
from daily_agent.reading import audit_reading
from daily_agent.paper_document import extract_document,attach_document,build_document,atomic_json
from daily_agent.rendering.notes import write_reading_notes
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.rendering.html import render_daily_html
from daily_agent.storage import write_editorial_artifacts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True,help='Existing approval.json, read only')
    parser.add_argument('--output',type=Path,required=True,help='New empty directory under tmp/')
    parser.add_argument('--papers',type=int,default=1)
    parser.add_argument('--repos',type=int,default=0)
    parser.add_argument('--resume',action='store_true',help='Reuse matching snapshot/config and validated reading cache in an existing output')
    parser.add_argument('--live',action='store_true',help='Invoke configured model; may incur model usage')
    args=parser.parse_args()
    output=args.output.resolve()
    if ROOT/'tmp' not in output.parents:
        parser.error('output must be under project tmp/')
    if output.exists() and not args.resume:
        parser.error('output exists; use --resume for the same fixed input')
    if args.resume:
        snapshot=output/'input-snapshot.json'
        if not snapshot.is_file() or snapshot.read_bytes()!=args.input.read_bytes():
            parser.error('resume requires the identical input snapshot')
    if args.papers<0 or args.repos<0 or args.papers+args.repos<1:
        parser.error('select at least one item')
    output.mkdir(parents=True,exist_ok=args.resume)
    if not args.resume:
        shutil.copytree(ROOT/'config',output/'config')
        shutil.copyfile(args.input,output/'input-snapshot.json')
    cfg=load_config(output)
    cfg.quota.update(max_items=args.papers+args.repos,paper_target=args.papers,github_target=args.repos)
    if args.live:
        try:
            result=subprocess.run(_llm_writer_command(_llm_writer_settings(cfg),'禁止调用工具。只输出JSON：{"ok":true}'),
                                  capture_output=True,text=True,check=True,timeout=60)
            if _decode_model_json(result.stdout).get('ok') is not True: raise ValueError('invalid model probe')
            atomic_json(output/'model-probe.json',{'status':'passed'})
        except Exception as exc:
            atomic_json(output/'model-probe.json',{'status':'blocked','error_type':type(exc).__name__,
                'instruction':'Check configured llm_writer.command, provider endpoint, API key availability and network. API wrappers require load_user_config=true. No model acceptance claimed.'})
            print('Model probe failed; no complete report generated. See',output/'model-probe.json')
            return 2
    records=[]; counts={'paper':0,'repo':0}; limits={'paper':args.papers,'repo':args.repos}
    for raw in json.loads((output/'input-snapshot.json').read_text()):
        record=MaterialRecord.from_dict(raw.get('material',raw))
        if counts[record.item_type]>=limits[record.item_type]: continue
        counts[record.item_type]+=1
        record.reading={};record.paper_document={}
        record.raw.pop('reading_note_url',None);record.raw.pop('reading_note_html_url',None)
        if record.item_type=='paper':
            value=record.raw.get('local_pdf_path')
            pdf=Path(value) if value else None
            if pdf is not None and not pdf.is_absolute(): pdf=ROOT/pdf
            if pdf is not None and pdf.is_file():
                target=output/'inputs'/f'paper-{counts["paper"]}.pdf';target.parent.mkdir(exist_ok=True)
                shutil.copyfile(pdf,target)
                doc=extract_document(target.read_bytes(),record,record.pdf_url or record.url,
                    {**cfg.sources['paper_text'],'page_image_dir':str(output/'pages'/f'paper-{counts["paper"]}')})
            else:
                doc=build_document(record,[{'page':None,'text':record.abstract or ''}],record.url,'abstract',problems=['本地PDF缺失；没有联网补取'])
            attach_document(record,doc)
        records.append(record)
    atomic_json(output/'input-manifest.json',{'requested':limits,'selected':counts,'source':str(args.input.resolve())})
    atomic_json(output/'input-documents.json',[r.to_dict() for r in records])
    drafts=draft_report_items(cfg,records,use_llm=args.live)
    reviews=review_draft(cfg,drafts,use_llm=args.live)
    approved=approve_publication(cfg,records,drafts,reviews)
    today=date.today();write_reading_notes(cfg,approved,today,dry_run=True)
    status=RunStatus();status.fallback='隔离验收：固定本地素材；不含实时采集，不外部投递'
    cfg.reports_dir.mkdir(exist_ok=True)
    (cfg.reports_dir/'validation.md').write_text(render_daily_markdown(approved,today,status))
    (cfg.reports_dir/'validation.html').write_text(render_daily_html(approved,today,status))
    write_editorial_artifacts(cfg,today,records,drafts,reviews,approved)
    audit = audit_reading([r.to_dict() for r in records])
    failed_keys = {p['key'] for p in audit['papers'] if not p['quality_passed']}
    failures=[{'key':r.key,'read_complete':r.reading.get('complete',False),'visual':r.reading.get('visual',{}),
               'verification':r.reading.get('verification',{})} for r in records if r.item_type=='paper'
               and r.key in failed_keys]
    passed=args.live and counts==limits and not failures and len(approved)==len(records)
    papers=[r for r in records if r.item_type=='paper']
    atomic_json(output/'acceptance.json',{'live':args.live,'passed':passed,'selected':counts,'approved':len(approved),'failures':failures,
        'pipeline_completed':True,
        'text_reading_complete':all(p['full_text_read'] for p in audit['papers']),
        'extracted_chunks_read_complete':all(r.reading.get('complete') for r in papers),
        'reading_audit':audit,
        'visual_reading_complete':all(not r.reading.get('visual',{}).get('required_pages') or r.reading['visual'].get('complete') for r in papers),
        'summary_semantics_checked':all(r.reading.get('verification',{}).get('semantic_support')=='model_checked' for r in papers),
        'scope':'Fixed local corpus end-to-end reading; no live source ingestion or remote delivery'})
    print('PASS' if passed else 'INCOMPLETE',output/'acceptance.json')
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
