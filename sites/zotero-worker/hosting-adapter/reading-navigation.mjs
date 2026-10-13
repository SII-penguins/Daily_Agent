/** Pure, offline URL-only remap. Takes public-relative path -> Buffer maps.
 * Never reads or writes a checkout, refreshes pages, publishes, or grants access.
 * Callers must authenticate the baseline, mapping approval and serving route.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

export const NAVIGATION_FILES = Object.freeze(['index.html','archive/index.html','assets/archive-browser.js','manifest.json']);
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const same = (a,b,message) => assert.deepEqual(a,b,message);
const fail = message => { throw new Error(message); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const utf8 = bytes => { const text = new TextDecoder('utf-8',{fatal:true}).decode(bytes); if (!Buffer.from(text).equals(bytes)) fail('Noncanonical UTF-8'); return text; };
const compare = (a,b) => a < b ? -1 : a > b ? 1 : 0;

export function validateMappings(mappings) {
  if (!Array.isArray(mappings) || mappings.length < 1 || mappings.length > 100) fail('Explicit bounded mappings required');
  const from = new Set(), to = new Set();
  for (const rule of mappings) {
    if (!object(rule) || Object.keys(rule).sort().join(',') !== 'from,to' || !/^\/(?:reports|examples)\/[A-Za-z0-9][A-Za-z0-9._-]*\/reading\.html$/.test(rule.from) || !/^\/zotero\/reports\/[a-z0-9]+(?:-[a-z0-9]+)*$/.test(rule.to)) fail('Only exact archive reading paths to same-origin Zotero report routes are allowed');
    if (from.has(rule.from) || to.has(rule.to)) fail('Duplicate or ambiguous mapping');
    from.add(rule.from); to.add(rule.to);
  }
  return mappings.map(rule => ({...rule}));
}

export function remapReadingURL(value, mappings, {relativeSource=false}={}) {
  for (const {from,to} of validateMappings(mappings)) {
    const source = relativeSource ? from.slice(1) : from;
    if (value === source || value.startsWith(source+'#')) return to+value.slice(source.length);
  }
  return value;
}

// Keep JSON formatting, key order, Unicode escaping and all unselected bytes.
// Duplicate keys are rejected because their meaning differs across consumers.
function jsonSpans(text) {
  let i=0; const nodes=[]; const space=()=>{while(/[\t\n\r ]/.test(text[i]??'!'))i++;};
  const string=()=>{const start=i;if(text[i++]!=='"')fail('Invalid JSON string');let escaped=false;while(i<text.length){const c=text[i++];if(c==='"'&&!escaped)return JSON.parse(text.slice(start,i));if(c==='\\'&&!escaped)escaped=true;else escaped=false;}fail('Unclosed JSON string');};
  function value(path) {
    space();const start=i;let result;
    if(text[i]==='"')result=string();
    else if(text[i]==='{') { i++;space();result=Object.create(null);const keys=new Set();if(text[i]!=='}')while(true){space();const key=string();if(keys.has(key))fail('Duplicate JSON key');keys.add(key);space();if(text[i++]!==':')fail('Invalid JSON object');result[key]=value([...path,key]);space();if(text[i]!==',')break;i++;}if(text[i++]!=='}')fail('Invalid JSON object'); }
    else if(text[i]==='[') { i++;space();result=[];if(text[i]!==']')while(true){result.push(value([...path,result.length]));space();if(text[i]!==',')break;i++;}if(text[i++]!==']')fail('Invalid JSON array'); }
    else {const match=/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(i));if(!match)fail('Invalid JSON value');i+=match[0].length;result=JSON.parse(match[0]);}
    nodes.push({path,start,end:i,value:result});return result;
  }
  const result=value([]);space();if(i!==text.length)fail('Trailing JSON content');return {value:result,nodes};
}

function edit(text, patches) {
  const sorted=[...patches].sort((a,b)=>a.start-b.start);let end=0,output='';
  for(const patch of sorted){if(patch.start<end||patch.start>patch.end||text.slice(patch.start,patch.end)!==patch.before)fail('Overlapping or stale patch');output+=text.slice(end,patch.start)+patch.after;end=patch.end;}
  return output+text.slice(end);
}
const patch = (text,start,end,after) => ({start,end,before:text.slice(start,end),after});

function htmlPatches(text,mappings) {
  const patches=[];let i=0;
  while((i=text.indexOf('<',i))!==-1) {
    if(text.startsWith('<!--',i)){const end=text.indexOf('-->',i+4);if(end<0)fail('Unclosed HTML comment');i=end+3;continue;}
    let j=i+1,quote=null;
    for(;j<text.length;j++){const c=text[j];if(quote){if(c===quote)quote=null;}else if(c==='"'||c==="'")quote=c;else if(c==='>')break;}
    if(j===text.length)fail('Unclosed HTML tag');
    const tag=text.slice(i,j+1),name=/^<\s*([A-Za-z][A-Za-z0-9-]*)\b/.exec(tag)?.[1]?.toLowerCase();
    if(name==='a') {
      // Scan attributes, consuming their quoted values so a string inside a
      // different attribute can never masquerade as href.
      let p=/^<\s*a\b/i.exec(tag)[0].length,href=false;
      while(p<tag.length){while(/\s/.test(tag[p]??''))p++;if(tag[p]==='>'||tag.startsWith('/>',p))break;const attr=/^[^\s=/>]+/.exec(tag.slice(p));if(!attr)fail('Unsupported HTML attribute');p+=attr[0].length;while(/\s/.test(tag[p]??''))p++;if(tag[p]!=='=')continue;p++;while(/\s/.test(tag[p]??''))p++;const q=tag[p];if(q!=='"'&&q!=="'")fail('Quoted HTML attributes required');const start=++p,end=tag.indexOf(q,p);if(end<0)fail('Unclosed HTML attribute');p=end+1;
        if(attr[0].toLowerCase()==='href'){if(href)fail('Duplicate href');href=true;const before=tag.slice(start,end),after=remapReadingURL(before,mappings);if(after!==before)patches.push(patch(text,i+start,i+end,after));}
      }
    }
    i=j+1;
    if(['script','style','textarea','title'].includes(name)){const close=new RegExp('</\\s*'+name+'\\s*>','ig');close.lastIndex=i;const match=close.exec(text);if(!match)fail('Unclosed HTML raw text');i=close.lastIndex;}
  }
  return patches;
}

function scriptPatches(text,mappings) {
  const newline=text.indexOf('\n'),prefix='window.DAILY_ARCHIVE = ';
  if(newline<0||!text.startsWith(prefix)||text[newline-1]!==';')fail('Expected exact archive data first line');
  const parsed=jsonSpans(text.slice(prefix.length,newline-1));
  if(!Array.isArray(parsed.value))fail('Archive data array required');
  for(const day of parsed.value)if(!object(day)||!Array.isArray(day.editions))fail('Archive day editions required');
  return parsed.nodes.filter(node=>node.path.length===4&&typeof node.path[0]==='number'&&node.path[1]==='editions'&&typeof node.path[2]==='number'&&node.path[3]==='reading').flatMap(node=>{
    if(typeof node.value!=='string')fail('Archive reading URL required');const after=remapReadingURL(node.value,mappings);return after===node.value?[]:[patch(text,prefix.length+node.start,prefix.length+node.end,JSON.stringify(after))];
  });
}

function validateFiles(files) {
  if(!(files instanceof Map)||files.size<4||files.size>20000)fail('Complete bounded public file Map required');
  for(const [name,bytes]of files){if(typeof name!=='string'||!name||name.startsWith('/')||/[\\\x00-\x1f]/.test(name)||name.split('/').some(part=>!part||part==='.'||part==='..')||!Buffer.isBuffer(bytes)||bytes.length>25*1024*1024)fail('Invalid public file');}
  for(const name of NAVIGATION_FILES)if(!files.has(name))fail('Missing navigation file: '+name);
}

export function inventory(files) {
  validateFiles(files);return [...files].sort(([a],[b])=>compare(a,b)).map(([path,bytes])=>({path,bytes:bytes.length,sha256:digest(bytes)}));
}

function manifestFor(files) {
  const parsed=jsonSpans(utf8(files.get('manifest.json'))),m=parsed.value;
  if(m.schema_version!==1||!object(m.files)||!Array.isArray(m.entries)||!object(m.navigation)||m.navigation.schema!=='daily-agent-archive-navigation-v2'||!object(m.navigation.reading_pages)||!object(m.navigation.preferred_by_date))fail('Existing v2 navigation manifest required');
  same(Object.keys(m.files).sort(),[...files.keys()].filter(p=>p!=='manifest.json').sort(),'Manifest file set mismatch');
  for(const [name,metadata]of Object.entries(m.files)){
    if(!object(metadata)||Object.keys(metadata).sort().join(',')!=='sha256,size')fail('Invalid manifest metadata');
    same({sha256:metadata.sha256,size:metadata.size},{sha256:digest(files.get(name)),size:files.get(name).length},'Manifest bytes mismatch: '+name);
  }
  const seen=new Set();for(const entry of m.entries){if(!object(entry)||typeof entry.key!=='string'||seen.has(entry.key)||entry.page!==entry.key+'/index.html'||!files.has(entry.page))fail('Invalid or duplicate manifest entry');seen.add(entry.key);}
  return parsed;
}

/** Return a complete virtual after-tree and exact text patches; inputs untouched.
 * A target already mapped everywhere is an intentional no-op. Mixed old/new
 * state is transformed deterministically; verifyNavigationDelta still proves
 * every byte changed by this call. Authenticating that baseline is the host's job.
 */
export function planNavigationRemap({files,mappings}) {
  validateFiles(files);mappings=validateMappings(mappings);const parsed=manifestFor(files);
  for(const {from}of mappings){const key=from.slice(1,-'/reading.html'.length);if(!files.has(from.slice(1))||!parsed.value.entries.some(e=>e.key===key))fail('Mapping lacks a retained sealed source edition');}
  const after=new Map([...files].map(([name,bytes])=>[name,Buffer.from(bytes)])),patches={};
  for(const name of NAVIGATION_FILES.slice(0,3)) {
    const text=utf8(files.get(name));patches[name]=name.endsWith('.js')?scriptPatches(text,mappings):htmlPatches(text,mappings);
    after.set(name,Buffer.from(edit(text,patches[name])));
  }
  const text=utf8(files.get('manifest.json'));patches['manifest.json']=[];
  for(const node of parsed.nodes) {
    let replacement;
    if(node.path.length===3&&node.path[0]==='navigation'&&['reading_pages','preferred_by_date'].includes(node.path[1])) {
      if(typeof node.value!=='string')fail('Manifest reading URL required');
      // Historical manifest values are archive-relative; newly mapped routes
      // are root-relative. Preserve all unrelated values exactly.
      const remapped=remapReadingURL(node.value,mappings,{relativeSource:true});if(remapped!==node.value)replacement=JSON.stringify(remapped);
    }
    if(node.path.length===3&&node.path[0]==='files'&&NAVIGATION_FILES.slice(0,3).includes(node.path[1])) {
      const bytes=after.get(node.path[1]);if(node.path[2]==='sha256'&&node.value!==digest(bytes))replacement=JSON.stringify(digest(bytes));if(node.path[2]==='size'&&node.value!==bytes.length)replacement=String(bytes.length);
    }
    if(replacement!==undefined)patches['manifest.json'].push(patch(text,node.start,node.end,replacement));
  }
  after.set('manifest.json',Buffer.from(edit(text,patches['manifest.json'])));manifestFor(after);
  // Require consistent links for every requested mapping after transformation.
  for(const {from,to}of mappings){const key=from.slice(1,-'/reading.html'.length),m=manifestFor(after).value;if(m.navigation.reading_pages[key]!==to)fail('Mapped reading-page record is missing or inconsistent');
    for(const name of NAVIGATION_FILES.slice(0,3))if(!utf8(after.get(name)).includes(to))fail('Mapped reading URL is absent from a global navigation surface');}
  return {files:after,patches};
}

/** Undo only the derived patches. Verify the after-bytes before restoring. */
export function reverseNavigationRemap({files,patches}) {
  const restored=new Map([...files].map(([name,bytes])=>[name,Buffer.from(bytes)]));
  for(const name of NAVIGATION_FILES){let shift=0;const inverse=patches[name].map(p=>{const start=p.start+shift;shift+=p.after.length-p.before.length;return {start,end:start+p.after.length,before:p.after,after:p.before};});restored.set(name,Buffer.from(edit(utf8(files.get(name)),inverse)));}
  return restored;
}

/** Fail closed on ANY extra/removed/changed byte outside the exact generated
 * delta. Caller-supplied receipts cannot relax this check. A future append must
 * prove its ordinary refresh separately, then use that exact tree as `before`.
 */
export function verifyNavigationDelta({before,after,mappings,requireChange=true}) {
  validateFiles(before);validateFiles(after);same([...after.keys()].sort(),[...before.keys()].sort(),'Navigation remap must not add or remove files');
  const planned=planNavigationRemap({files:before,mappings});
  for(const [name,bytes]of planned.files)if(!bytes.equals(after.get(name)))fail('Unapproved navigation delta: '+name);
  const changed=[...before.keys()].filter(name=>!before.get(name).equals(after.get(name))).sort();
  if(requireChange&&changed.length===0)fail('Navigation remap made no changes');
  const restored=reverseNavigationRemap({files:after,patches:planned.patches});
  for(const [name,bytes]of before)if(!bytes.equals(restored.get(name)))fail('Navigation delta is not exactly reversible');
  const beforeManifest=manifestFor(before),afterManifest=manifestFor(after);
  same(beforeManifest.value.entries,afterManifest.value.entries,'Manifest entries changed');
  const entriesText=x=>{const n=x.nodes.find(node=>node.path.length===1&&node.path[0]==='entries');return utf8(x===beforeManifest?before.get('manifest.json'):after.get('manifest.json')).slice(n.start,n.end);};
  same(entriesText(beforeManifest),entriesText(afterManifest),'Manifest entry bytes changed');
  const tail=bytes=>bytes.subarray(bytes.indexOf(10));same(tail(before.get('assets/archive-browser.js')),tail(after.get('assets/archive-browser.js')),'Archive script tail changed');
  const sealed=inventory(before).filter(row=>!NAVIGATION_FILES.includes(row.path));
  return {schema:'daily-agent-reading-navigation-delta-v1',mappings:validateMappings(mappings),changed_files:changed,total_files:before.size,sealed_files:sealed.length,sealed_inventory_sha256:digest(JSON.stringify(sealed)),manifest_entries:beforeManifest.value.entries.length,manifest_entries_sha256:digest(entriesText(beforeManifest)),javascript_tail_sha256:digest(tail(before.get('assets/archive-browser.js'))),all_other_bytes_preserved:true,manifest_entries_byte_preserved:true,javascript_tail_byte_preserved:true,exact_reverse_verified:true,patch_counts:Object.fromEntries(Object.entries(planned.patches).map(([name,p])=>[name,p.length])),before:inventory(before).filter(row=>changed.includes(row.path)),after:inventory(after).filter(row=>changed.includes(row.path)),route_serving_verified:false,published:false};
}
