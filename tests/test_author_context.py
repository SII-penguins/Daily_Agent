import json
from dataclasses import replace
from pathlib import Path

from daily_agent.author_context import (build_author_context, enrich_author_contexts,
    publisher_evidence, research_context_lines, update_watchlist)
from daily_agent.models import DigestItem
from daily_agent.config import load_config


def paper(**kwargs):
    return DigestItem(id='test', source='pmlr', item_type='paper', title='Quantum AI Paper',
                      url='https://proceedings.mlr.press/test.html', **kwargs)


def evidence(**kwargs):
    return {'title':'Quantum AI Paper', 'source_url':'https://lab.example.edu/publications',
        'source_kind':'official_lab','review_status':'verified','checked_at':'2026-10-08T00:00:00Z',
        'excerpt':'Quantum AI Paper', **kwargs}


def test_unknowns_are_not_inferred_from_order_or_institution():
    p=paper(authors=['A', 'B'], raw={'authorships':[{'name':'B','institutions':['University X']}]})
    context=build_author_context(p)
    assert context['labs'] == []
    assert all(not a['roles'] for a in context['authors'])
    assert context['institutions'][0]['status']=='source_metadata_unverified'
    assert '不按末位作者推定' in ' '.join(context['uncertainties'])


def test_exact_paper_identity_and_review_required():
    p=paper(authors=['A'])
    lab={'name':'Lab','relationship':'paper_listed_by_group'}
    assert not build_author_context(p,[evidence(title='Different Paper',labs=[lab])])['labs']
    assert not build_author_context(p,[evidence(review_status='unverified',labs=[lab])])['labs']
    assert build_author_context(p,[evidence(labs=[lab])])['labs'][0]['status']=='verified_official'
    assert not build_author_context(p,[evidence(labs=[{**lab,'relationship':'same_institution'}])])['labs']


def test_publisher_metadata_exact_title_and_explicit_affiliations():
    p=paper()
    html='<meta name="citation_title" content="Quantum AI Paper"><meta name="citation_author" content="Alice"><meta name="citation_author_institution" content="University X"><meta name="citation_author" content="Bob">'
    ev=publisher_evidence(p,html,p.url)
    ctx=build_author_context(p,[ev])
    assert ctx['authors'][0]['roles']==['first_author']
    assert ctx['authors'][1]['institutions']==[]
    assert not ctx['labs']
    assert not publisher_evidence(p,html,'https://untrusted.example.com')
    assert not publisher_evidence(p,html.replace('Quantum AI Paper','Other'),p.url)


def test_orcid_conflict_not_silently_merged():
    p=paper(raw={'authorships':[{'name':'Alice','orcid':'a'}]})
    ctx=build_author_context(p,[evidence(authors=[{'name':'Alice','orcid':'b'}])])
    assert ctx['authors'][0]['orcid']=='a'
    assert any('ORCID' in s for s in ctx['uncertainties'])


def test_homonyms_are_not_global_identity_and_watchlist_is_local(tmp_path):
    p=paper(authors=['Alice'])
    p.raw['research_context']=build_author_context(p)
    q=paper(authors=['Alice']); q.id='other'; q.url='https://proceedings.mlr.press/other.html'
    q.raw['research_context']=build_author_context(q)
    path=tmp_path/'watch.json'
    update_watchlist(path,[p,q])
    saved=json.loads(path.read_text())
    assert len(saved['entries'])==2
    assert saved['external_actions'] is False
    update_watchlist(path,[p],limit=1)
    assert len(json.loads(path.read_text())['entries'])==1


def test_vlanext_reviewed_seed_has_no_pi_or_single_lab_inference():
    root=Path(__file__).resolve().parents[1]
    seed=json.loads((root/'config/research-context-evidence.json').read_text())
    p=paper(); p.title='VLANeXt: Recipes for Building Strong VLA Models'
    ctx=build_author_context(p,seed['sources'])
    assert len(ctx['authors'])==9
    assert [a['name'] for a in ctx['authors'] if 'corresponding_author' in a['roles']]==['Chen Change Loy']
    assert {x['name'] for x in ctx['labs']}=={'MMLab@NTU','S-Lab, Nanyang Technological University'}
    text=' '.join(research_context_lines(ctx))
    assert '非全部作者归属声明' in text
    assert '历史研究背景' in text
    assert 'PI' not in text


def test_network_budget_and_cache(tmp_path, monkeypatch):
    config=replace(load_config(),root=tmp_path,sources={'author_context':{'max_pages_per_run':1}})
    calls=[]
    def fetch(client,item,max_bytes,deadline=None):
        calls.append(item.id)
        return {'title':item.title,'source_url':item.url,'source_kind':'publisher_metadata',
                'checked_at':'2026-10-08T00:00:00Z','excerpt':item.title,'authors':[{'name':'Alice'}]}
    monkeypatch.setattr('daily_agent.author_context._fetch_publisher',fetch)
    items=[paper(),paper()]; items[1].id='second'; items[1].title='Second'
    enrich_author_contexts(items,config)
    assert len(calls)==1
    enrich_author_contexts(items[:1],config)
    assert len(calls)==1
    assert items[0].raw['research_context']['authors'][0]['name']=='Alice'


def test_conflicting_doi_rejects_even_same_title():
    p=paper(doi='10.1000/right')
    html='<meta name="citation_title" content="Quantum AI Paper"><meta name="citation_doi" content="10.1000/wrong">'
    assert not publisher_evidence(p,html,p.url)


def test_lab_listing_is_generic_but_requires_reviewed_page():
    from daily_agent.author_context import lab_listing_evidence
    p=paper()
    lab={'name':'Lab X','url':'https://lab.example.edu/publications','review_status':'verified'}
    html='<h2>Publications</h2><a>Quantum AI Paper</a>'
    ev=lab_listing_evidence(p,lab,html,'2026-10-08T00:00:00Z')
    ctx=build_author_context(p,[ev])
    assert ctx['labs'][0]['relationship']=='paper_listed_by_group'
    assert ctx['authors']==[]
    assert not lab_listing_evidence(p,{**lab,'review_status':'candidate'},html,'now')
    assert not lab_listing_evidence(p,lab,'Different paper','now')


def test_metadata_survives_existing_material_roundtrip():
    from daily_agent.models import MaterialRecord
    p=paper(authors=['Alice'])
    p.raw['research_context']=build_author_context(p)
    out=MaterialRecord.from_item(p).to_digest_item()
    assert out.raw['research_context']==p.raw['research_context']


def test_unverified_authors_remain_visible_in_rendered_context():
    ctx=build_author_context(paper(authors=['Alice']))
    assert 'Alice（元数据，未独立核实）' in ' '.join(research_context_lines(ctx))


def test_duplicate_lab_listings_merge_provenance():
    ev=evidence(labs=[{'name':'Lab X','relationship':'paper_listed_by_group'}])
    ctx=build_author_context(paper(),[ev,ev])
    assert len(ctx['labs'])==1
    assert len(ctx['labs'][0]['evidence'])==1
