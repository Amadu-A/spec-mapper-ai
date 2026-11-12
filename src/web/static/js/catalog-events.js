// src/web/static/js/catalog-events.js
/** Реальная диагностика durable webhook; обычной сессии доступны только агрегаты. */
import {api} from './api.js';
import {element} from './dom.js';
import {date,number} from './dashboard-state.js';

export function createCatalogEvents(operator,notify) {
  /** Подключает копирование URL и обновление счётчиков без секретов. */
  const root=document.querySelector('[data-catalog-events]');
  if(!root)return async()=>{};
  const input=root.querySelector('[data-webhook-url]');
  root.querySelector('[data-copy-webhook]').addEventListener('click',async()=>{
    try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(input.value);else{input.focus();input.select();if(!document.execCommand('copy'))throw new Error('Выделенный адрес можно скопировать вручную.');}notify('Адрес webhook скопирован');}
    catch(error){input.focus();input.select();notify(error.message,true);}
  });
  return async()=>{
    const value=await api(operator?'/api/v1/catalog/events':'/api/v1/catalog/event-status');
    input.value=value.webhook_url;
    const labels={ready:'Обработчик готов',awaiting_contract:'Ожидается подтверждённый контракт 1С',awaiting_bootstrap:'Ожидается первоначальный полный импорт',awaiting_configuration:'Нужен проверенный JSON HTTP источник'};
    root.querySelector('[data-catalog-events-state]').textContent=`${labels[value.state]||value.state} · ${value.secret_configured?'Ключ настроен':'Ключ не настроен'} · объединение ${value.batch_window_seconds} с`;
    for(const key of ['received','pending','applied','failed'])root.querySelector(`[data-event-count="${key}"]`).textContent=number(value[key]);
    root.querySelector('[data-event-last]').textContent=date(value.last_applied_at);
    const journal=root.querySelector('[data-event-journal]');journal.hidden=!operator;
    if(operator)root.querySelector('[data-event-items]').replaceChildren(...(value.items||[]).map(row=>element('li','catalog-events__entry',`${date(row.received_at)} · ${row.event_id} · ${row.state} · изменений: ${row.change_count} · попыток: ${row.attempts}${row.last_error?' · '+row.last_error:''}`)));
  };
}
