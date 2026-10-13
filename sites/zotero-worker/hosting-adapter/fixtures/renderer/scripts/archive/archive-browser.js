(function () {
'use strict';
const normalize = value => String(value || '').normalize('NFKC').toLocaleLowerCase().trim().replace(/\s+/g, ' ');
function select(data, state) {
 const terms = normalize(state.q).split(' ').filter(Boolean);
 return data.filter(day => (!state.date || day.date === state.date) && (!state.year || day.date.startsWith(state.year+'-')) && (!state.month || day.date.startsWith(state.month))).map(day => {
  const editions=day.editions.map(edition => ({...edition,matches:edition.titles.filter(item=>terms.every(term=>normalize(item.title).includes(term)))})).filter(edition=>!terms.length || terms.every(term=>normalize(day.date+' '+edition.label).includes(term)) || edition.matches.length);
  return {...day,editions};
 }).filter(day=>day.editions.length);
}
function parse(search) {
 const p=new URLSearchParams(search);return {q:p.get('q')||'',year:/^\d{4}$/.test(p.get('year')||'')?p.get('year'):'',month:/^\d{4}-\d{2}$/.test(p.get('month')||'')?p.get('month'):'',date:/^\d{4}-\d{2}-\d{2}$/.test(p.get('date')||'')?p.get('date'):'',page:Math.max(1,parseInt(p.get('page'),10)||1)};
}
function query(state) {const p=new URLSearchParams();for(const k of ['q','year','month','date'])if(state[k])p.set(k,state[k]);if(state.page>1)p.set('page',state.page);return p.toString();}
function groups(data) {const months=[];for(const day of data){let group=months[months.length-1];if(!group||group.month!==day.date.slice(0,7)){group={month:day.date.slice(0,7),days:[]};months.push(group);}group.days.push(day);}return months;}
if(typeof module !=='undefined'&&module.exports)module.exports={normalize,select,parse,query,groups};
if(typeof document==='undefined')return;
const data=window.DAILY_ARCHIVE;
if(!Array.isArray(data))return;
const $=id=>document.getElementById(id),form=$('archive-filters'),results=$('archive-results'),status=$('archive-status');
const allMonths=[...new Set(data.map(day=>day.date.slice(0,7)))],years=[...new Set(data.map(day=>day.date.slice(0,4)))];
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
function options(node,values,all,selected){node.replaceChildren();const first=el('option',all);first.value='';node.append(first);for(const value of values){const option=el('option',value);option.value=value;node.append(option);}if(selected&&!values.includes(selected)){const option=el('option',selected+'（暂无日报）');option.value=selected;node.append(option);}node.value=selected;}
function link(label,url,cls){const a=el('a',label,cls);a.href=url;return a;}
let state=parse(location.search);
function navigate(next){state={...next};const q=query(state);history.pushState(null,'',location.pathname+(q?'?'+q:''));render();}
function render(){
 $('archive-query').value=state.q;$('archive-date').value=state.date;options($('archive-year'),years,'全部年份',state.year);options($('archive-month'),allMonths.filter(m=>!state.year||m.startsWith(state.year+'-')),'全部月份',state.month);
 const matched=select(data,state),monthGroups=groups(matched),pages=Math.max(1,Math.ceil(monthGroups.length/12));state.page=Math.min(state.page,pages);
 const visible=monthGroups.slice((state.page-1)*12,state.page*12),filtered=Boolean(state.q||state.year||state.month||state.date);
 status.textContent=`找到 ${matched.length} 期日报 · ${matched.reduce((n,d)=>n+d.editions.length,0)} 个版本`+(monthGroups.length>12?` · 第 ${state.page}/${pages} 页`:'');results.replaceChildren();
 if(!matched.length){const empty=el('div',undefined,'empty-state');empty.append(el('h2','没有找到日报'),el('p',state.date?`${state.date} 没有符合条件的已归档日报。可清除日期，或调整关键词。`:'试试其他关键词、日期或月份，也可以重置筛选。'));results.append(empty);}
 for(const [index,group] of visible.entries()){
  const details=el('details',undefined,'archive-month-group');details.open=filtered||(!index&&state.page===1);const summary=el('summary');summary.append(el('strong',group.month.replace('-',' 年 ')+' 月'),el('span',`${group.days.length} 期`));details.append(summary);
  for(const day of group.days){const card=el('section',undefined,'archive-day');const heading=el('div',undefined,'archive-date-heading'),h=el('h2'),time=el('time',day.date);time.dateTime=day.date;h.append(time);heading.append(h,link('打开日报 →',day.editions[0].reading));card.append(heading);
   const editionList=el('details',undefined,'archive-editions');editionList.open=Boolean(state.q);editionList.append(el('summary',`本日 ${day.editions.length} 个版本 · 标题与原始页面`));
   for(const edition of day.editions){const block=el('div',undefined,'archive-edition');block.append(link(edition.label+' · 阅读 →',edition.reading),link('原始页面',edition.original,'archive-original'));
    const titles=state.q?edition.matches:edition.titles;if(titles.length){const ul=el('ul',undefined,'archive-title-list');for(const item of titles){const li=el('li');li.append(link(item.title,edition.reading+'#'+item.anchor));ul.append(li);}block.append(ul);}editionList.append(block);
   }card.append(editionList);details.append(card);
  }results.append(details);
 }
 const pager=$('archive-pagination');pager.replaceChildren();if(pages>1){for(const [label,delta]of [['上一页',-1],['下一页',1]]){const button=el('button',label);button.type='button';button.disabled=state.page+delta<1||state.page+delta>pages;button.addEventListener('click',()=>{navigate({...state,page:state.page+delta});status.focus();});pager.append(button);}pager.append(el('span',`每页最多 12 个月 · ${state.page}/${pages}`));}
 $('archive-static').hidden=true;form.hidden=false;$('archive-dynamic').hidden=false;
}
form.addEventListener('submit',event=>{event.preventDefault();navigate({...state,q:$('archive-query').value.trim(),date:$('archive-date').value,page:1});});
$('archive-year').addEventListener('change',()=>navigate({...state,year:$('archive-year').value,month:'',date:'',page:1}));
$('archive-month').addEventListener('change',()=>navigate({...state,month:$('archive-month').value,date:'',page:1}));
$('archive-date').addEventListener('change',()=>navigate({...state,date:$('archive-date').value,year:'',month:'',page:1}));
$('archive-reset').addEventListener('click',()=>{navigate({q:'',year:'',month:'',date:'',page:1});$('archive-query').focus();});
window.addEventListener('popstate',()=>{state=parse(location.search);render();});
render();
})();
