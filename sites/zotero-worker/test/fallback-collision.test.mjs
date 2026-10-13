import test from 'node:test';
import assert from 'node:assert/strict';
import {matchPaper,metadataFor} from '../src/import-domain.mjs';
const paper={paper_id:'synthetic-no-identifier',title:'Synthetic Study: Robust Learning',source_url:'https://publisher.example.test/v1/synthetic.html',doi:'',arxiv:'',authors:['Synthetic A Writer','Second Author'],year:'2000',venue:'Proceedings of the Synthetic Learning Conference',item_type:'conferencePaper',publication:'published_verified',topics:['other-reviewed']};
const candidate=()=>({key:'ABCDEFGH',data:metadataFor(paper)});
const nearCases={
 'abbreviated venue':data=>data.proceedingsTitle='Synthetic Conf.',
 'full date':data=>data.date='2000-07-21',
 'alternate full date':data=>data.date='July 21, 2000',
 'initial punctuation':data=>data.creators[0].name='Synthetic A. Writer',
 'wrong author order':data=>data.creators.reverse(),
 'candidate DOI':data=>data.DOI='10.9999/synthetic',
 'invalid candidate DOI':data=>data.DOI='not-a-doi',
 'punctuated title':data=>data.title='Synthetic Study - Robust Learning!',
 'same source changed title':data=>data.title='Completely different stored label',
};
for(const [name,mutate]of Object.entries(nearCases))test('fallback collision blocks creation without fuzzy matching: '+name,()=>{
 const item=candidate();mutate(item.data);assert.throws(()=>matchPaper(paper,[item]),{code:'FALLBACK_IDENTITY_REVIEW_REQUIRED'});
 const exact=candidate();item.key='BCDEFGHI';for(const rows of [[exact,item],[item,exact]])assert.throws(()=>matchPaper(paper,rows),{code:'FALLBACK_IDENTITY_REVIEW_REQUIRED'});
});
test('trash near match blocks even beside a valid exact candidate in any order',()=>{const item=candidate();item.key='BCDEFGHI';item.data.date='2000-07-21';item.data.deleted=1;const exact=candidate();for(const rows of [[item],[exact,item],[item,exact]])assert.throws(()=>matchPaper(paper,rows),{code:'MATCH_IN_TRASH'});});
test('two exact fallbacks remain ambiguous; normalized exact remains allowed; unrelated items remain absent',()=>{
 const a=candidate(),b=candidate();b.key='BCDEFGHI';assert.throws(()=>matchPaper(paper,[a,b]),{code:'AMBIGUOUS_IDENTITY'});
 const normalized=candidate();normalized.data.title='ＳＹＮＴＨＥＴＩＣ STUDY:   ROBUST LEARNING';assert.equal(matchPaper(paper,[normalized]).key,'ABCDEFGH');
 const unrelated=candidate();unrelated.data.title='Unrelated study';unrelated.data.url='https://publisher.example.test/elsewhere';assert.equal(matchPaper(paper,[unrelated]),null);
});
test('strong identifier matching/conflicts remain unchanged',()=>{const identified={...paper,doi:'10.9999/one'};const matching=candidate();matching.data.DOI='10.9999/one';matching.data.title='Unrelated stored title';assert.equal(matchPaper(identified,[matching]).key,'ABCDEFGH');const unrelated=candidate();unrelated.data.DOI='10.9999/two';assert.equal(matchPaper(identified,[unrelated]),null);});
for(const [incoming,stored]of [['Synthetic Robust-Learning Study','Synthetic Robust Learning Study'],['Synthetic Model-based Vision-Language-Action','Synthetic Model based Vision Language Action'],['Synthetic Long-horizon Speed-Up','Synthetic Long horizon Speed Up'],['Synthetic Modelbased Study','Synthetic Model-based Study']])test('title-only stop signal handles punctuation word boundaries without a stored URL: '+incoming,()=>{
  const source={...paper,title:incoming},item={key:'ABCDEFGH',data:metadataFor({...paper,title:stored})};delete item.data.url;item.data.date='2000-07-21';
  assert.throws(()=>matchPaper(source,[item]),{code:'FALLBACK_IDENTITY_REVIEW_REQUIRED'});
});
