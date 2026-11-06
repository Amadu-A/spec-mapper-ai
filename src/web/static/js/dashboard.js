// src/web/static/js/dashboard.js
/** Сводка и проверенные индикаторы; пустые данные и ошибки отображаются явно. */
import {api} from "./api.js";
import {date,number,percent,labels,tone} from "./dashboard-state.js";
import {element} from "./dom.js";
export const put = (selector,value) => { const node=document.querySelector(selector); if(node) node.textContent=value; };
export function badge(node,state) {
  /** Статус берётся только из ответа backend. */
  if(!node) return; node.textContent=labels[state] || state; node.dataset.tone=tone(state);
}
export async function refreshSummary() {
  /** Сохраняет различие собственной и общей статистики, не читая чужие документы. */
  try {
    const data=await api("/api/v1/dashboard/summary"), m=data.metrics, c=data.catalog;
    for(const node of document.querySelectorAll("[data-health]")) { const state=data.health[node.dataset.health]; node.dataset.tone=tone(state); node.title=labels[state] || state; }
    put("[data-service-state]",Object.values(data.health).every(v=>v==="healthy")?"Сервис работает":"Есть недоступные компоненты");
    put("[data-kpi='catalog']",number(c.count)); put("[data-kpi='jobs']",number(m.jobs));
    put("[data-kpi='automatic']",percent(m.automatic_percent)); put("[data-kpi='review']",percent(m.review_percent));
    put("[data-kpi-total]",`Всего с начала наблюдения${m.scope==='own'?` (за ${m.metrics_retention_days} дней)`:''}: ${number(m.jobs_total)}`);
    put("[data-kpi-auto-count]",`${number(m.matched)} автоматически · ${number(m.manual)} вручную / ${number(m.positions)} позиций`);
    put("[data-kpi-review-count]",`${number(m.unresolved)} позиций, включая неподтверждённые «не найдено»`);
    put("[data-kpi-delta]",m.weekly_delta_pp===null?"Нет данных за две полные недели":`${m.weekly_delta_pp>=0?'+':''}${number(Number(m.weekly_delta_pp.toFixed(1)))} п.п. к прошлой неделе`);
    put("[data-metrics-scope]",`${m.scope==='global'?'Общая статистика оператора':'Только текущий владелец'} · задания созданы ${m.day_utc} UTC · проценты по завершённым позициям · наблюдение с ${date(m.coverage_started_at)}`);
    badge(document.querySelector("[data-catalog-current]"),c.ready?(c.last_error?"degraded":"healthy"):"unavailable");
    put("[data-catalog-date]",`Каталог: ${date(c.last_successful_sync)}`);
    return data;
  } catch(error) {
    document.querySelectorAll("[data-health]").forEach(n=>{n.dataset.tone="bad";n.title="Нет связи";});
    put("[data-service-state]","Нет связи с API"); put("[data-metrics-scope]",error.message); throw error;
  }
}
export async function refreshIntegrations() {
  /** Telegram означает входящий сигнал, 1С — результат сохранённой проверки контракта. */
  const data=await api("/api/v1/integrations/status");
  for(const [key,value] of Object.entries(data)) badge(document.querySelector(`[data-integration='${key}']`),value.state);
  put("[data-telegram-seen]",`Telegram — входящий API-клиент. Последний успешный запрос: ${date(data.telegram.last_seen)}`);
}
export async function refreshTimeline(operator) {
  /** Техническая история доступна только оператору; пустота не заменяется демо-событиями. */
  const node=document.querySelector("[data-timeline]"); if(!node) return;
  if(!operator) { node.replaceChildren(element("li","","Журнал доступен оператору"));return; }
  const data=await api("/api/v1/catalog/import-history?limit=3");
  const events=data.items.flatMap(run=>run.events.map(event=>({...event,run}))).sort((a,b)=>b.at-a.at).slice(0,4);
  node.replaceChildren(...events.map(event=>{const run=event.run,li=element("li","timeline__item");li.dataset.tone=tone(event.stage);li.append(element("strong","",`${labels[event.stage]||event.stage} · ${run.source_type==='local_xml'?'Локальный XML':'HTTP API 1С'}`),element("small","",date(event.at)),element('small','',`Задание: ${run.job_id}`));const a=element("a","","Открыть отчёт");a.href=`/api/v1/catalog/import-history/${run.id}/report.json`;li.append(a);return li;}));
  if(!data.items.length)node.append(element("li","","После обновления ещё не было импортов"));
}
