from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from daily_agent.config import load_config
from daily_agent.models import DigestItem, MaterialRecord, ApprovedItem, EditorialDraft, EditorialReview
from daily_agent.scoring.rules import score_items
from daily_agent.scoring.publication import publication_evidence, publication_scores
from daily_agent.scoring.topics import balanced_quantum_order, quantum_topic, qas_qnas_subtopic
from daily_agent.storage import select_library_candidates, upsert_materials, write_material_library, load_material_library
from daily_agent.material_pool import retain_quota_deferred
from daily_agent.discovery_window import calendar_month_start, within_discovery_window
from daily_agent.editorial import _limit_approved_items
from daily_agent.cloud_workflow import _qualifying_rows

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 8)

def cfg(tmp_path):
    return replace(load_config(ROOT), root=tmp_path)

def paper(n='1', title='Quantum machine learning benchmarks', source='arxiv', published='2026-10-01'):
    return DigestItem(id=n, source=source, item_type='paper', title=title, url=f'https://arxiv.org/abs/{n}',
                      arxiv_id=n if source=='arxiv' else None, published_at=published, updated_at=published,
                      abstract='Learning models with quantum hardware and classical baselines.', quota_group='quantum')

def reviewed(p):
    r=MaterialRecord.from_item(p)
    r.paper_document={'document_kind':'full_text','source_type':'html','pages':[]}
    r.reading={'complete':True,'verification':{'status':'located','semantic_support':'model_checked'}}
    return r

def approved(r):
    return ApprovedItem(r.key,r.item_type,r.title,r.source,r.url,{},r)


def test_calendar_month_boundaries_not_ninety_days():
    assert calendar_month_start(DAY)==date(2026,7,8)
    assert calendar_month_start(date(2026,5,31))==date(2026,2,28)
    assert calendar_month_start(date(2024,5,31))==date(2024,2,29)
    config=load_config(ROOT)
    assert within_discovery_window(paper(published='2026-07-08'),config,DAY)
    assert not within_discovery_window(paper(published='2026-07-07'),config,DAY)
    assert not within_discovery_window(paper(published='2026-10-09'),config,DAY)


def test_preprint_eligible_but_equal_formal_paper_preferred(tmp_path):
    config=cfg(tmp_path)
    preprint=paper('1')
    formal=paper('2',source='pmlr')
    formal.url='https://proceedings.mlr.press/v306/example26.html'
    formal.raw={'venue':'ICML 2026'}
    ranked=score_items([preprint,formal],config,target_date=datetime(2026,10,8,tzinfo=timezone.utc))
    assert ranked[0] is formal and preprint.score>-50
    assert preprint.score_breakdown['publication']==-10
    rows,excluded=_qualifying_rows([approved(reviewed(preprint)).to_dict()])
    assert len(rows)==1 and not excluded
    assert publication_evidence(preprint)['status']=='preprint_only'


def test_preprint_still_requires_full_reading_support():
    r=reviewed(paper());r.reading['complete']=False
    rows,excluded=_qualifying_rows([approved(r).to_dict()])
    assert not rows and excluded[0]['reasons']==['full_reading_or_claim_support_incomplete']


def test_bidirectional_config_and_soft_qas_balance(tmp_path):
    config=cfg(tmp_path)
    assert {d.name for d in config.domains}>={'ai_for_quantum','quantum_for_ai','embodied_and_agents'}
    assert {d.priority for d in config.domains if d.quota_group=='quantum'}=={1.0}
    titles=['Quantum circuit synthesis '+str(i) for i in range(10)]+[
        'Quantum control using neural feedback','Neural quantum state learning',
        'Quantum kernels for classification','Quantum generative models']
    records=[reviewed(paper(str(i),title)) for i,title in enumerate(titles)]
    for i,r in enumerate(records):r.score=100-i
    for i in range(4):
        r=reviewed(paper('e'+str(i),'Vision language action model '+str(i)));r.quota_group='exploratory';records.append(r)
    for i in range(2):
        r=MaterialRecord.from_item(DigestItem(id='repo'+str(i),source='github',item_type='repo',title='AI agent '+str(i),url='https://github.com/x/'+str(i),quota_group='exploratory'))
        records.append(r)
    chosen=_limit_approved_items(config,[approved(r) for r in records])
    papers=[i.material for i in chosen if i.item_type=='paper']
    assert len(papers)==8 and len(chosen)==10
    assert 2 <= sum(bool(qas_qnas_subtopic(r)) for r in papers)<=3
    assert {quantum_topic(r)[0] for r in papers}>={'ai_for_quantum','quantum_for_ai'}
    assert sum(i.material.quota_group=='quantum' for i in chosen)==6
    assert all(r.raw.get('personal_topic_umbrella',{}).get('basis')=='user_organizational_grouping' for r in papers if qas_qnas_subtopic(r))


def test_quota_deferred_retained_beyond_freshness_and_rescored(tmp_path):
    config=cfg(tmp_path);config.quota.update(max_items=1,paper_target=1,github_target=0)
    chosen=reviewed(paper('chosen',published='2026-07-08'))
    later=reviewed(paper('later',title='Quantum neural network generalization',published='2026-07-08'))
    library={r.key:r for r in [chosen,later]}
    drafts=[EditorialDraft(r.key,'paper',r.title,{}) for r in library.values()]
    reviews=[EditorialReview(r.key,'PASS') for r in library.values()]
    retain_quota_deferred(config,library,list(library.values()),drafts,reviews,[approved(chosen)],DAY)
    assert later.raw['pool_deferral']['reason']=='daily_count_limit'
    assert 'pool_deferral' not in chosen.raw
    write_material_library(config,library)
    candidates=select_library_candidates(config,load_material_library(config),DAY+timedelta(days=1))
    assert [r.key for r in candidates]==[later.key]
    assert candidates[0].score_breakdown['deferred_revisit']==.5
    assert candidates[0].raw['publication_evidence']['status']=='preprint_only'
    # Rediscovery of exactly the same version preserves the reviewed deferral.
    upsert_materials(config,[later.to_digest_item()],DAY+timedelta(days=1))
    assert load_material_library(config)[later.key].raw['pool_deferral']['first_deferred_on']==DAY.isoformat()


def test_unsupported_rejected_and_reserved_are_not_quota_deferred(tmp_path,monkeypatch):
    config=cfg(tmp_path);config.quota.update(max_items=1,paper_target=1)
    chosen=reviewed(paper('chosen'));bad=reviewed(paper('bad'));bad.reading['complete']=False
    rejected=reviewed(paper('reject'));rejected.quality_status='rejected'
    reserved=reviewed(paper('reserved'))
    monkeypatch.setattr('daily_agent.cloud_workflow.reserved_delivery_identities',lambda *a:{reserved.key})
    records=[chosen,bad,rejected,reserved];library={r.key:r for r in records}
    retain_quota_deferred(config,library,records,[EditorialDraft(r.key,'paper',r.title,{}) for r in records],
                          [EditorialReview(r.key,'PASS') for r in records],[approved(chosen)],DAY)
    assert not any('pool_deferral' in r.raw for r in records)


def test_nature_landing_proof_and_merged_preprint_preserve_truth():
    from daily_agent.scoring.dedup import deduplicate_items
    p=paper(source='nature');p.url='https://www.nature.com/articles/s41586-026-12345-x';p.doi='10.1038/s41586-026-12345-x'
    proof={'verified':True,'method':'publisher_landing_citation_metadata','url':p.url,'title':p.title,'venue':'Nature'}
    p.raw={'venue':'Nature','publication_status':'published','primary_landing_verified':True,'primary_verification':proof}
    assert publication_scores(p)==(14,6)
    a=paper();a.doi=p.doi
    merged=deduplicate_items([a,p])[0]
    roundtrip=MaterialRecord.from_item(merged).to_digest_item()
    assert publication_evidence(roundtrip)['status']=='published'
    proof['title']='A different paper'
    assert publication_evidence(p)['status']!='published'


def test_screened_overflow_survives_but_is_not_falsely_approved(tmp_path):
    config=cfg(tmp_path);config.quota.update(max_items=1,paper_target=1)
    selected=reviewed(paper('selected',published='2026-07-08'))
    pending=MaterialRecord.from_item(paper('pending',title='Quantum kernel learning',published='2026-07-08'))
    pending.score=60
    library={r.key:r for r in [selected,pending]}
    retain_quota_deferred(config,library,[selected],[EditorialDraft(selected.key,'paper',selected.title,{})],
                          [EditorialReview(selected.key,'PASS')],[approved(selected)],DAY)
    assert pending.raw['pool_deferral']['status']=='screening_deferred'
    assert pending.raw['pool_deferral']['review_required'] is True
    assert pending.raw['pool_deferral']['quality_basis']=='topic_and_source_screening_only'
    assert select_library_candidates(config,library,DAY+timedelta(days=1))==[pending]
    assert _qualifying_rows([approved(pending).to_dict()])[0]==[]


def test_pipeline_window_is_calendar_based_and_does_not_expand_to_a_year():
    from daily_agent.pipeline import _fallback_window_steps
    config=load_config(ROOT)
    assert _fallback_window_steps(config,datetime(2026,10,8,tzinfo=timezone.utc))==[(92,30)]
    assert _fallback_window_steps(config,datetime(2026,5,19,tzinfo=timezone.utc))==[(89,30)]
    coarse=paper(published='2026-01-01');coarse.raw['publication_date_precision']='year'
    assert not within_discovery_window(coarse,config,date(2026,2,1))


def test_delivered_alias_is_not_deferred_or_selected(tmp_path):
    config=cfg(tmp_path);config.quota.update(max_items=1,paper_target=1)
    delivered=reviewed(paper('old'));delivered.doi='10.1234/same';delivered.published_dates=['2026-10-07']
    alias=reviewed(paper('alias',source='crossref'));alias.doi=delivered.doi;alias.score=80
    selected=reviewed(paper('selected',title='Quantum neural kernel benchmark'))
    library={r.key:r for r in [delivered,alias,selected]}
    retain_quota_deferred(config,library,[selected],[EditorialDraft(selected.key,'paper',selected.title,{})],
                          [EditorialReview(selected.key,'PASS')],[approved(selected)],DAY)
    assert 'pool_deferral' not in alias.raw
    assert alias not in select_library_candidates(config,library,DAY)


def test_source_revision_signal_invalidates_preserved_reading(tmp_path):
    config=cfg(tmp_path)
    original=reviewed(paper('revision'))
    write_material_library(config,{original.key:original})
    replacement=paper('revision');replacement.updated_at='2026-10-08T00:00:00Z'
    stored=upsert_materials(config,[replacement],DAY)[original.key]
    assert not stored.paper_document and not stored.reading


def test_failed_review_does_not_become_quota_overflow_next_day(tmp_path):
    config=cfg(tmp_path);config.quota.update(max_items=1,paper_target=1)
    chosen=reviewed(paper('chosen'));bad=reviewed(paper('bad'));bad.reading['complete']=False;bad.score=80
    library={r.key:r for r in [chosen,bad]}
    retain_quota_deferred(config,library,[chosen,bad],[],[EditorialReview(bad.key,'FAIL')],[approved(chosen)],DAY)
    assert bad.raw['pool_review']['status']=='unsupported_or_failed'
    retain_quota_deferred(config,library,[chosen],[],[],[approved(chosen)],DAY+timedelta(days=1))
    assert 'pool_deferral' not in bad.raw


def test_reviewed_deferral_keeps_approval_basis_when_not_revisited(tmp_path):
    config=cfg(tmp_path);config.quota.update(max_items=1,paper_target=1)
    chosen=reviewed(paper('chosen'));later=reviewed(paper('later'));later.score=80
    records=[chosen,later];library={r.key:r for r in records}
    retain_quota_deferred(config,library,records,[EditorialDraft(r.key,'paper',r.title,{}) for r in records],
                          [EditorialReview(r.key,'PASS') for r in records],[approved(chosen)],DAY)
    retain_quota_deferred(config,library,[chosen],[],[],[approved(chosen)],DAY+timedelta(days=1))
    assert later.raw['pool_deferral']['status']=='quota_deferred'
    assert later.raw['pool_deferral']['quality_basis']=='full_reading_and_supported_claims'
