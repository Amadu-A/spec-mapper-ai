// src/web/static/js/jobs.js
/** Рабочий список метаданных с серверными фильтрами и owner-safe ссылками. */
import {api} from "./api.js";
import {element} from "./dom.js";
import {labels,date,number,tone} from "./dashboard-state.js";
export function createJobs(operator,notify) {
  /** Пагинация имеет защиту от запоздалого ответа предыдущего фильтра. */
  const root=document.querySelector("[data-jobs-panel]");if(!root)return ()=>{};
  const find=s=>root.querySelector(s), filter=find("[data-jobs-state]"), profile=find("[data-jobs-profile]"), global=find("[data-global-jobs]"),rows=find("[data-jobs-rows]");
  let offset=0,order="desc",generation=0;const limit=10;
  find("[data-global-jobs-label]").hidden=!operator;
  async function load() {
    const current=++generation;
    try {
      const data=await api(`/api/v1/${global.checked?'operator':'dashboard'}/jobs?offset=${offset}&limit=${limit}&state=${encodeURIComponent(filter.value)}&order=${order}&profile=${encodeURIComponent(profile.value)}`);
      if(current!==generation)return;
      rows.replaceChildren(...data.items.map(job=>{
        const row=element("tr"),id=element("td","jobs-table__id",job.job_id);id.title=job.job_id;row.append(id,element("td","",`${job.profile==="invoice"?"Счета":"Спецификации"} · ${job.source_type}`));
        const state=element("span","badge",labels[job.status]||job.stage);state.dataset.tone=tone(job.status);const cell=element("td");cell.append(state);row.append(cell,element("td","",job.total_known?number(job.total):"—"),element("td","",job.total_known?`${number(job.counts.matched)} / ${number(job.counts.manual)}`:"—"),element("td","",job.total_known?number(job.unresolved_count):"—"),element("td","",date(job.created_at)));
        const action=element("td");if(job.can_open){const link=element("a","button","Открыть");link.href=job.ui_url;action.append(link);}else action.append(element('span','','Только метаданные'));
        const menu=element('details','job-actions'),summary=element('summary','','Ещё');menu.append(summary);
        const copy=element('button','button','Скопировать UUID');copy.type='button';copy.addEventListener('click',async()=>{try{if(navigator.clipboard)await navigator.clipboard.writeText(job.job_id);else{const input=element('textarea');input.value=job.job_id;action.append(input);input.select();const copied=document.execCommand('copy');input.remove();if(!copied)throw new Error('Выделите UUID в таблице и скопируйте вручную');}notify('Полный UUID скопирован');}catch(error){notify(error.message,true);}});menu.append(copy,element('p','',`Статус: ${labels[job.status]||job.status}${job.error_code?` · ${job.error_code}`:''}`));
        if(job.can_open){for(const [kind,url] of Object.entries(job.export_links)){
          const names={source:"Исходный JSON",draft:"Черновой JSON",result:"Утверждённый JSON",xlsx:"Excel"};const link=element('a','button',names[kind]||kind);link.href=url;menu.append(link);
        }}
        action.append(menu);row.append(action);return row;
      }));
      find("[data-jobs-empty]").hidden=!!data.items.length;find("[data-jobs-empty]").textContent="Нет заданий по выбранному фильтру";
      find("[data-jobs-page-info]").textContent=`${data.total?offset+1:0}–${offset+data.items.length} из ${data.total}`;
      find("[data-jobs-prev]").disabled=offset===0;find("[data-jobs-next]").disabled=offset+limit>=data.total;
      find("[data-jobs-scope]").textContent=global.checked?"Общий обзор без доступа к чужим документам":"Только задания текущего владельца";
    }catch(error){if(current===generation){notify(error.message,true);find("[data-jobs-empty]").textContent="Не удалось получить задания";}}
  }
  profile.addEventListener("change",()=>{offset=0;load();});filter.addEventListener("change",()=>{offset=0;load();});global.addEventListener("change",()=>{offset=0;load();});
  find("[data-refresh-jobs]").addEventListener("click",load);
  find("[data-sort-jobs]").addEventListener("click",()=>{order=order==='desc'?'asc':'desc';offset=0;find("[data-sort-direction]").textContent=order==='desc'?'↓':'↑';load();});
  find("[data-jobs-prev]").addEventListener("click",()=>{offset=Math.max(0,offset-limit);load();});find("[data-jobs-next]").addEventListener("click",()=>{offset+=limit;load();});load();return load;
}
