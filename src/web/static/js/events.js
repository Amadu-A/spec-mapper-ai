// src/web/static/js/events.js
/** История импортов и отдельный безопасный операторский аудит. */
import {api} from "./api.js";
import {element} from "./dom.js";
import {date,labels,changeText} from "./dashboard-state.js";
export function createEvents(operator,notify) {
  /** Журнал фильтруется и пагинируется на сервере, без хранения исходных документов. */
  const root=document.querySelector('[data-history-panel]');if(!root)return;
  const find=s=>root.querySelector(s),kind=find('[data-history-kind]'),state=find('[data-history-state]'),source=find('[data-history-source]');let offset=0,generation=0;const limit=20;
  find('[data-history-access]').textContent=operator?'Техническая история · срок хранения задаётся сервером':'Журнал доступен оператору. Откройте «Доступ оператора».';
  if(!operator){root.querySelectorAll('button,select').forEach(n=>{n.disabled=true;});return;}
  async function load(){const current=++generation;try{
    const data=await api(kind.value==='audit'?`/api/v1/operator/audit?offset=${offset}&limit=${limit}`:`/api/v1/catalog/import-history?offset=${offset}&limit=${limit}&state=${state.value}&source_type=${source.value}`);if(current!==generation)return;
    find('[data-history-items]').replaceChildren(...data.items.map(run=>{const article=element('article','history-item');
      if(kind.value==='audit'){article.append(element('h2','',run.event),element('p','',date(run.at)),element('pre','',JSON.stringify(run.details,null,2)));return article;}
      article.append(element('h2','',`${labels[run.stage]||run.stage} · ${run.source_type==='local_xml'?'Локальный XML':'HTTP API 1С'}`),element('p','',`${date(run.created_at)} · ${run.initiator}`),element('p','',changeText(run.details)));
      for(const ext of ['json','xlsx']){const a=element('a','button',`Отчёт ${ext.toUpperCase()}`);a.href=`/api/v1/catalog/import-history/${run.id}/report.${ext}`;article.append(a);}
      const details=element('details'),summary=element('summary','','Этапы и диагностика');details.append(summary,...run.events.map(event=>element('p','',`${date(event.at)} · ${labels[event.stage]||event.stage}${event.error_code?` · ${event.error_code}`:''}`)));article.append(details);return article;}));
    find('[data-history-empty]').hidden=!!data.items.length;find('[data-history-page]').textContent=`${data.total?offset+1:0}–${offset+data.items.length} из ${data.total}`;find('[data-history-prev]').disabled=offset===0;find('[data-history-next]').disabled=offset+limit>=data.total;
  }catch(error){notify(error.message,true);}}
  for(const input of [kind,state,source])input.addEventListener('change',()=>{offset=0;state.disabled=source.disabled=kind.value==='audit';load();});
  find('[data-history-refresh]').addEventListener('click',load);find('[data-history-prev]').addEventListener('click',()=>{offset=Math.max(0,offset-limit);load();});find('[data-history-next]').addEventListener('click',()=>{offset+=limit;load();});load();
}
