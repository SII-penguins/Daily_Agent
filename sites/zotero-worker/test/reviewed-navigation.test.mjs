import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {renderReviewedPage} from '../src/reviewed-ui.mjs';
test('fragment links stay on reviewed route despite original asset base; navigation is visible',async()=>{
 const html='<html><head></head><body><a href="#coverage">覆盖与核验</a><a href=\'#top\'>顶部</a><a href="#paper-1">论文</a><img src="figure.png"><p id="coverage">核验</p></body></html>';
 const bytes=new TextEncoder().encode(html),original=bytes.slice(),issue={issue_id:'synthetic-issue',entry_path:'reports/original/index.html',csp_ranges:[],anchors:[],head_offset:html.indexOf('</head>'),body_offset:html.indexOf('<body>')+6};
 const rendered=await renderReviewedPage(bytes,issue).text();assert.deepEqual(bytes,original);
 for(const [,q,href] of rendered.matchAll(/<a\b[^>]*href=(["'])(.*?)\1/g)){const u=new URL(href,'https://example.test/reports/original/');if(u.hash)assert.equal(u.pathname,'/zotero/reports/synthetic-issue');}
 assert.match(rendered,/href="\/archive\/">往期日报/);assert.match(rendered,/position:sticky/);assert.match(rendered,/id="da-z-panel"/);assert.match(rendered,/<img src="figure.png">/);
});
