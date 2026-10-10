from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from daily_agent.config import load_config
from daily_agent.models import DigestItem
from daily_agent import formal_discovery
from daily_agent.scoring.dedup import deduplicate_items

ROOT=Path(__file__).resolve().parents[1]
TARGET=datetime(2026,10,10,tzinfo=timezone.utc)


def item(stage='version_of_record'):
    url='https://proceedings.mlr.press/v306/paper.html'
    title='Quantum circuit learning'
    return DigestItem(id='paper',source='pmlr',item_type='paper',title=title,url=url,
        abstract='Quantum circuit learning with quantum neural networks.',published_at='2026-09-29',
        raw={'venue':'ICML 2026','publication_stage':stage,'primary_landing_verified':True,
             'publication_date_precision':'day','abstract_source':'official_pmlr_abstract',
             'primary_verification':{'verified':True,'url':url,'title':title,'venue':'ICML 2026','published_at':'2026-09-29'},
             'metadata_evidence':{'response_sha256':'a'*64,'citation_excerpt':'<meta name="citation_conference_title" content="International Conference on Machine Learning">'}})


def test_independent_journal_nature_pass_shares_one_budget(tmp_path,monkeypatch):
    cfg=replace(load_config(ROOT),root=tmp_path);seen=[]
    monkeypatch.setattr(formal_discovery,'fetch_pmlr',lambda *a,**k:[item()])
    def preferred(*a,**k):
        seen.append(k['budget']);k['budget'].reserve();return []
    def nature(*a,**k):
        seen.append(k['budget']);assert k['discovery_seeds']==[];k['budget'].reserve();return []
    monkeypatch.setattr(formal_discovery,'fetch_preferred_journals',preferred)
    monkeypatch.setattr(formal_discovery,'fetch_nature',nature)
    items,coverage=formal_discovery.collect_official_candidates(cfg,TARGET)
    assert len(items)==1 and seen[0] is seen[1]
    assert coverage['nature_and_journals_total']['requests']==2
    assert coverage['complete_archive'] is False


def test_handoff_has_exact_extract_but_no_pdf_or_early_vor_claim(tmp_path,monkeypatch):
    cfg=replace(load_config(ROOT),root=tmp_path/'state')
    good=item();early=item('accepted_manuscript');early.id='early'
    monkeypatch.setattr(formal_discovery,'collect_official_candidates',lambda *a:([good,early],{'complete_archive':False}))
    rows,_=formal_discovery.export_candidate_handoff(cfg,TARGET,tmp_path/'handoff')
    proof=rows[0]['publication'];assert proof['quote'] in Path(proof['evidence_path']).read_text()
    assert 'not full page or full text' in Path(proof['evidence_path']).read_text()
    assert 'publication' not in rows[1]
    assert rows[0]['handoff_status']=='metadata_candidate_requires_full_reading'
    assert 'pdf_path' not in rows[0]
    assert not (tmp_path/'handoff'/'state.json').exists()


def test_merged_arxiv_record_keeps_official_abstract_date_provenance():
    official=item()
    preprint=DigestItem(id='preprint',source='arxiv',item_type='paper',title=official.title,
        url='https://arxiv.org/abs/2610.00001',published_at='2026-10-01')
    merged,=deduplicate_items([preprint,official])
    evidence=merged.raw['evidence']['sources']['pmlr']
    assert merged.source=='arxiv'
    assert evidence['publication_date']=='2026-09-29'
    assert evidence['abstract_source']=='official_pmlr_abstract'
    assert evidence['metadata_evidence']['citation_excerpt']
