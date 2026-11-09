// src/web/static/js/sync-settings.js
/** Форма PostgreSQL-настроек, ручной запуск и реальный прогресс общего pipeline. */
import {api,writeOptions} from "./api.js";
import {date,number,labels,changeText} from "./dashboard-state.js";
import {put} from "./dashboard.js";
import {newIdempotencyKey} from "./state.js";
export function createSync(operator,notify) {
  /** Несохранённые поля не перезаписываются фоновыми запросами статуса. */
  const root=document.querySelector("[data-sync-panel]");if(!root)return async()=>{};
  const form=root.querySelector("[data-sync-form]"),fields=[...form.elements].filter(n=>n.name);
  let saved=null,dirty=false,busy=false,active=false,loaded=false;
  let refreshState=null,cooldownUntil=0,pendingRefreshKey=null;
  const refreshButton=root.querySelector('[data-sync-now]');
  const errorNode=root.querySelector("[data-sync-error]");
  const showError=error=>{errorNode.textContent=error.message;errorNode.hidden=false;};
  function access() {
    const http=form.elements.source_type.value==='http_onec',unverified=http&&(!saved?.contract_confirmed||!saved?.probe_current||saved?.source_type!=='http_onec');
    fields.forEach(n=>{n.disabled=!operator||!saved||busy||(n.name==='url'&&!http)||(n.name==='enabled'&&!n.checked&&unverified);});
    root.querySelector("[data-save-sync]").disabled=!operator||!saved||busy;
    root.querySelector("[data-test-source]").disabled=!operator||!saved||busy||dirty;
    const seconds=Math.max(0,Math.ceil((cooldownUntil-performance.now())/1000));
    const allowed=refreshState?.available || (refreshState?.reason==='catalog_refresh_cooldown'&&!seconds&&!refreshState?.active);
    refreshButton.disabled=busy||dirty||seconds>0||!allowed;
    refreshButton.textContent=seconds?`Обновить через ${seconds} с`:'Обновить сейчас';
    const reason=refreshState?.reason;
    put('[data-refresh-status]',seconds?`Следующее ручное обновление доступно через ${seconds} с`:
      refreshState?.active?'Обновление справочника выполняется':allowed?'Ручное обновление доступно не чаще раза в минуту':
      reason==='operator_required'?'HTTP 1С ещё не настроен. Локальный XML обновляет оператор.':
      reason==='onec_contract_required'?'Ожидается утверждённый HTTP-контракт 1С':'Источник ещё не подтверждён. Настройки меняет оператор.');
  }
  function render(value,reset=false) {
    saved=value;
    if(reset||!dirty)for(const field of fields){if(field.type==='checkbox')field.checked=value[field.name];else field.value=value[field.name]??'';}
    if(reset){dirty=false;put("[data-sync-save-status]","Настройки сохранены в PostgreSQL");}
    put("[data-secret-state]",`${value.secret_configured?'Серверные credentials настроены':'Credentials не настроены'} · ${value.contract_confirmed?'Контракт разрешён на сервере':'Ожидается утверждённый HTTP-контракт 1С'} · ${labels[value.probe?.state]||value.probe?.state||'Проверка не выполнялась'}${value.probe?.error_code?` (${value.probe.error_code})`:''}`);
    const description=root.querySelector("[data-sync-description] span");
    description.textContent=value.enabled?`Ежедневно в ${value.time_utc} UTC будет запущено обновление из ${value.source_type==='local_xml'?'локального XML':'проверенного HTTP-источника 1С'}.`:`Автообновление выключено. Сохранённое время: ${value.time_utc} UTC. Ручной запуск не меняет расписание.`;
    put("[data-sync-next]",value.enabled?date(value.next_run_at):"Автообновление выключено");
    const link=root.querySelector("[data-source-link]");link.href=value.source_type==='http_onec'&&value.url?value.url:'/settings#onec-connection';link.target=value.source_type==='http_onec'?'_blank':'_self';link.rel='noopener noreferrer';
    access();
  }
  form.addEventListener("input",()=>{dirty=true;put("[data-sync-save-status]","Есть несохранённые изменения");access();});
  form.addEventListener("change",access);
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!saved||busy)return;errorNode.hidden=true;
    const time=form.elements.time_utc.value;
    if(!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)){put("[data-time-error]","Укажите HH:MM UTC");return;}
    put("[data-time-error]","");const value={revision:saved.revision};
    for(const field of fields)value[field.name]=field.type==='checkbox'?field.checked:field.type==='number'?Number(field.value):field.value;
    busy=true;access();
    try{render(await api('/api/v1/catalog/sync-settings',{method:'PUT',body:JSON.stringify(value)}),true);await refresh();notify("Расписание сохранено. Импорт при сохранении не запускается.");}
    catch(error){showError(error);if(error.status===409)put("[data-sync-save-status]","Настройки изменены в другой сессии. Перезагрузите страницу перед повторным сохранением.");}
    finally{busy=false;access();}
  });
  root.querySelector("[data-test-source]").addEventListener('click',async()=>{
    busy=true;errorNode.hidden=true;access();
    try{render(await api('/api/v1/catalog/connection-test',{method:'POST'}));await refresh();notify(saved.probe?.state==='confirmed'?"Источник проверен. Каталог не изменён.":"Источник пока не подтверждён; смотрите статус контракта.");}
    catch(error){showError(error);}finally{busy=false;access();}
  });
  root.querySelector("[data-sync-now]").addEventListener('click',async()=>{
    if(refreshButton.disabled)return;busy=true;errorNode.hidden=true;access();
    pendingRefreshKey ||= newIdempotencyKey();
    try{await api('/api/v1/catalog/refresh',writeOptions({},pendingRefreshKey));pendingRefreshKey=null;cooldownUntil=performance.now()+60000;notify("Импорт поставлен в очередь");await refresh();}
    catch(error){if(error.retryAfter)cooldownUntil=performance.now()+error.retryAfter*1000;if(error.status&&error.status!==429)pendingRefreshKey=null;showError(error);await refresh();}finally{busy=false;access();}
  });
  async function refresh() {
    try {
      const [catalog,availability]=await Promise.all([api('/api/v1/catalog/status'),api('/api/v1/catalog/refresh-status')]);
      refreshState=availability;cooldownUntil=performance.now()+availability.retry_after*1000;
      put('[data-sync-success]',date(catalog.last_successful_sync));put('[data-sync-version]',catalog.version||'Нет активной версии');put('[data-sync-hash]',`SHA-256: ${catalog.source_hash?catalog.source_hash.slice(0,8)+'…'+catalog.source_hash.slice(-8):'—'}`);root.querySelector('[data-sync-hash]').title=catalog.source_hash||'';
      if(!operator){put('[data-sync-access]','Расписание, источник и общие отчёты доступны оператору. Подтверждённый HTTP-справочник может обновить каждый пользователь.');
        const summary=await api('/api/v1/dashboard/summary');
        if(!loaded){form.elements.time_utc.value=summary.schedule.time_utc;form.elements.enabled.checked=summary.schedule.enabled;form.elements.source_type.value=summary.schedule.source_type;loaded=true;}
        put('[data-sync-description] span',summary.schedule.enabled?`Расписание включено: ${summary.schedule.time_utc} UTC`:'Автообновление выключено');put('[data-sync-next]',summary.schedule.enabled?date(summary.schedule.next_run_at):'Выключено');
        const progress=availability.active,panel=root.querySelector('[data-sync-progress]');panel.hidden=!progress;
        if(progress){put('[data-sync-stage]',labels[progress.stage]||progress.stage);put('[data-sync-counts]',`${number(progress.processed)} / ${number(progress.total)} записей · ${number(progress.downloaded_bytes)} байт`);const meter=root.querySelector('[data-sync-meter]');if(progress.total){meter.max=progress.total;meter.value=progress.processed;}else meter.removeAttribute('value');}
        put('[data-sync-processed]',number(catalog.statistics?.total_rows??catalog.count));access();return;}
      put('[data-sync-access]','Операторская сессия · секреты остаются на сервере');
      const [value,history]=await Promise.all([api('/api/v1/catalog/sync-settings'),api('/api/v1/catalog/import-history?limit=1')]);render(value);
      const run=history.items[0],panel=root.querySelector('[data-sync-progress]'),report=root.querySelector('[data-sync-report]');
      active=!!run&&!['complete','failed'].includes(run.state);panel.hidden=!active;
      put('[data-sync-attempt]',run?`Последняя попытка: ${date(run.created_at)} · ${labels[run.stage]||run.stage}`:'После обновления ещё не было запусков');
      if(run){const d=run.details;report.href=`/api/v1/catalog/import-history/${run.id}/report.json`;report.removeAttribute('aria-disabled');
        const stats=d.statistics||{},vectors=d.run_statistics||d;put('[data-sync-processed]',number(stats.total_rows??d.processed??d.count));put('[data-sync-changes]',`${changeText(d)} · карантин: ${number(stats.quarantined_rows)} · векторы рассчитаны: ${number(vectors.embedded_count)}, из кэша: ${number(vectors.reused_count)}`);
        put('[data-sync-stage]',`${labels[run.stage]||run.stage}${d.error_code?` · ${d.error_code}`:''}`);
        put('[data-sync-counts]',`${number(d.processed)} / ${number(d.total)} записей · загружено ${number(d.downloaded_bytes)} байт`);
        const meter=root.querySelector('[data-sync-meter]');if(d.total>0){meter.max=d.total;meter.value=d.processed||0;}else meter.removeAttribute('value');
        if(run.state==='failed'){errorNode.textContent=`Последняя попытка завершилась ошибкой: ${d.error_code||'см. отчёт'}. Рабочая версия каталога сохранена.`;errorNode.hidden=false;}
      }access();
    }catch(error){showError(error);if(error.status===403){operator=false;access();put('[data-sync-access]','Сессия оператора истекла. Войдите повторно.');}}
  }
  access();setInterval(()=>{if(!document.hidden)access();},1000);return refresh;
}
