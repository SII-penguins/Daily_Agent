/** Offline SQLite -> Drizzle schema/journal generation. Never connects to live D1. */
import { readdir, readFile, writeFile, mkdir, access } from 'node:fs/promises';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
const root=process.cwd(), db=new DatabaseSync(':memory:');
const exists=async path=>{try{await access(path);return true;}catch{return false;}};
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const id=name=>{if(!/^[a-z][a-z0-9_]*$/.test(name))throw new Error('Unsupported SQL identifier');return name;};
const q=name=>'"'+id(name)+'"';
// Split only semicolons outside strings/comments. Trigger bodies are unsupported.
export function statements(sql){
  if(/\bCREATE\s+(?:TEMP\s+)?TRIGGER\b/i.test(sql))throw new Error('Trigger migrations need an explicit reviewed splitter');
  let quote=null,line=false,block=false,start=0;const result=[];
  for(let i=0;i<sql.length;i++){
    const a=sql[i],b=sql[i+1];
    if(line){if(a==='\n')line=false;continue;}
    if(block){if(a==='*'&&b==='/'){block=false;i++;}continue;}
    if(quote){if(a===quote){if(b===quote){i++;continue;}quote=null;}continue;}
    if(a==='-'&&b==='-'){line=true;i++;continue;}
    if(a==='/'&&b==='*'){block=true;i++;continue;}
    if(a==="'"||a==='"'||a==='`'){quote=a;continue;}
    if(a==='['){quote=']';continue;}
    if(a===';'){result.push(sql.slice(start,i+1).trim());start=i+1;}
  }
  if(quote||block)throw new Error('Unterminated SQL');
  const tail=sql.slice(start).trim();if(tail)result.push(tail);
  return result.filter(Boolean);
}
function checks(sql){
  const result=[];const pattern=/\bCHECK\s*\(/gi;let match;
  while((match=pattern.exec(sql))){
    let depth=1,quote=null,i=pattern.lastIndex,start=i;
    for(;i<sql.length;i++){
      const c=sql[i];if(quote){if(c===quote){if(sql[i+1]===quote){i++;continue;}quote=null;}continue;}
      if(c==="'"||c==='"'){quote=c;continue;}
      if(c==='(')depth++;if(c===')'&&--depth===0){result.push(sql.slice(start,i));pattern.lastIndex=i+1;break;}
      if(c!==')')continue;
    }
    if(depth)throw new Error('Unbalanced CHECK expression');
  }return result;
}
function schema(){
  const tables=db.prepare("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid").all();
  const output=['// Generated locally from immutable schema-only source SQL. No user records.','import { sql, desc } from "drizzle-orm";','import { sqliteTable, text, integer, blob, real, primaryKey, unique, index, uniqueIndex, foreignKey, check } from "drizzle-orm/sqlite-core";',''];
  for(const table of tables){
    id(table.name);const cols=db.prepare(`PRAGMA table_info(${q(table.name)})`).all(),extras=[];
    output.push(`export const ${table.name} = sqliteTable(${JSON.stringify(table.name)}, {`);
    for(const col of cols){
      id(col.name);const type=({TEXT:'text',INTEGER:'integer',BLOB:'blob',REAL:'real'})[col.type];
      if(!type)throw new Error('Unsupported SQLite column type');
      output.push(`  ${col.name}: ${type}(${JSON.stringify(col.name)})${col.notnull?'.notNull()':''}${col.dflt_value!==null?`.default(sql.raw(${JSON.stringify(col.dflt_value)}))`:''},`);
    }
    const pks=cols.filter(c=>c.pk).sort((a,b)=>a.pk-b.pk);
    if(pks.length)extras.push(`primaryKey({ columns: [${pks.map(c=>`table.${c.name}`).join(', ')}] })`);
    for(const ix of db.prepare(`PRAGMA index_list(${q(table.name)})`).all()){
      if(ix.origin==='pk')continue;
      const ixcols=db.prepare(`PRAGMA index_xinfo(${JSON.stringify(ix.name)})`).all().filter(c=>c.key);
      if(ixcols.some(c=>c.cid<0||!c.name))throw new Error('Expression indexes require explicit schema support');
      if(ix.partial)throw new Error('Partial indexes require explicit schema support');
      const name=ix.origin==='u'?`${table.name}_${ixcols.map(c=>c.name).join('_')}_unique`:id(ix.name);
      const kind=ix.origin==='u'?'unique':ix.unique?'uniqueIndex':'index';
      extras.push(`${kind}(${JSON.stringify(name)}).on(${ixcols.map(c=>c.desc?`desc(table.${id(c.name)})`:`table.${id(c.name)}`).join(', ')})`);
    }
    const fks=new Map();for(const fk of db.prepare(`PRAGMA foreign_key_list(${q(table.name)})`).all()){if(!fks.has(fk.id))fks.set(fk.id,[]);fks.get(fk.id).push(fk);}
    for(const group of fks.values()){
      group.sort((a,b)=>a.seq-b.seq);const fk=group[0];
      if(fk.on_update!=='NO ACTION'||fk.on_delete!=='NO ACTION')throw new Error('Foreign key actions require explicit schema support');
      extras.push(`foreignKey({ columns: [${group.map(f=>`table.${id(f.from)}`).join(', ')}], foreignColumns: [${group.map(f=>`${id(f.table)}.${id(f.to)}`).join(', ')}] })`);
    }
    checks(table.sql).forEach((expr,n)=>extras.push(`check(${JSON.stringify(`${table.name}_check_${n}`)}, sql.raw(${JSON.stringify(expr)}))`));
    output.push(`}, (table) => [\n  ${extras.join(',\n  ')}\n]);\n`);
  }
  return output.join('\n');
}
const sources=(await readdir(resolve(root,'zotero/migrations'))).filter(x=>/^\d{4}_[a-z0-9_]+\.sql$/.test(x)).sort();
if(!sources.length)throw new Error('No source migrations');
const mappingPath=resolve(root,'drizzle/zotero-source-migrations.json');
const mapping=await exists(mappingPath)?JSON.parse(await readFile(mappingPath,'utf8')):{version:1,migrations:[]};
for(const name of sources){
  const sql=await readFile(resolve(root,'zotero/migrations',name),'utf8');
  const parts=statements(sql);
  // Deliberately reject data mutations and external SQLite access in migrations.
  for(const part of parts){const first=part.replace(/--[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\//g,'').trim();if(!/^(?:CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX)|ALTER\s+TABLE|PRAGMA\s+foreign_keys\s*=\s*ON)/i.test(first))throw new Error('Migration must contain supported schema-only SQL');}
  db.exec(sql);
  const declaration=schema();
  await writeFile(resolve(root,'db/schema.ts'),declaration);
  const deploymentSql=parts.join('\n--> statement-breakpoint\n')+'\n';
  const prior=mapping.migrations.find(item=>item.source===name);
  if(prior){if(prior.source_sha256!==hash(sql)||hash(await readFile(resolve(root,'drizzle',prior.output)))!==prior.output_sha256)throw new Error('Immutable migration changed');continue;}
  const journalPath=resolve(root,'drizzle/meta/_journal.json');
  const before=await exists(journalPath)?JSON.parse(await readFile(journalPath,'utf8')).entries.length:0;
  const result=spawnSync(process.execPath,[resolve(root,'node_modules/drizzle-kit/bin.cjs'),'generate',`--name=${name.slice(0,-4)}`],{cwd:root,stdio:'inherit',env:{...process.env,CI:'true'}});
  if(result.error||result.status!==0)throw result.error||new Error('Drizzle generation failed');
  const journal=JSON.parse(await readFile(journalPath,'utf8'));
  if(journal.dialect!=='sqlite'||journal.entries.length!==before+1)throw new Error('Unexpected generated Drizzle journal');
  const tag=journal.entries.at(-1).tag;
  if(!/^[a-zA-Z0-9_-]+$/.test(tag))throw new Error('Unsafe Drizzle migration name');
  const output=`${tag}.sql`;
  if(!await exists(resolve(root,'drizzle',output)))throw new Error('Pinned Drizzle output contract changed');
  await writeFile(resolve(root,'drizzle',output),deploymentSql);
  mapping.migrations.push({source:name,output,source_sha256:hash(sql),output_sha256:hash(deploymentSql),statement_count:parts.length});
  await writeFile(mappingPath,JSON.stringify(mapping,null,2)+'\n');
}
db.close();
console.log(JSON.stringify({migration_count:mapping.migrations.length,schema_and_metadata_generated:true,live_database_changed:false}));
