// src/web/static/js/job-workspace.js
/** Общие server history, logical submit lock и SSE/fallback двух рабочих пространств. */
import {api} from "./api.js";
import {element} from "./dom.js";
import {newIdempotencyKey,validUuid} from "./state.js";
import {SubmissionState,fileProgress,jobLabels,jobStage,knownState,processingStates,progressView} from "./job-state.js";

export function createJobWorkspace({family,onOpen,onState,onNew,onExpired,onChange,onReady,notify}) {
  /** Durable source истины — API; в browser storage не сохраняются документы или ключи. */
  const panel=document.querySelector("[data-job-lifecycle]"),find=hook=>panel.querySelector(`[data-${hook}]`);
  const dialog=document.querySelector("[data-job-cancel-dialog]"),submission=new SubmissionState(newIdempotencyKey);
  let current=null,source=null,timer,request,epoch=0,cursor=0,failures=0,retrySubmit,cooldown=0,cancelBusy=false;
  const records=new Map(),historySelect=find("job-history");submission.state="restoring";

  function stop() {
    /** Закрывает только frontend subscription и устаревшие GET, не backend job. */
    epoch++;source?.close();source=null;clearTimeout(timer);request?.abort();request=null;
  }
  function render() {
    /** Отображает только принятые metadata и явно отдельные локальные состояния отправки. */
    const label=submission.state==="submitting"?jobLabels.submitting:submission.state==="uncertain"?"Исход отправки неизвестен":current?jobStage(current):jobLabels.idle;
    find("lifecycle-stage").textContent=label;
    panel.setAttribute("aria-busy",String(submission.state==="submitting"));
    const cancel=find("job-cancel");cancel.hidden=!current||![...processingStates,"awaiting_review"].includes(current.status);
    cancel.disabled=cancelBusy||current?.status==="cancelling";
    cancel.textContent=current?.status==="awaiting_review"?"Отказаться от черновика":current?.status==="cancelling"?"Отмена запрошена…":"Отменить";
    find("job-retry-submit").hidden=submission.state!=="uncertain";
    const warning=find("lifecycle-warning");warning.hidden=!current?.error_code;
    warning.textContent=current?.error_code==="upstream_submission_unknown"?"Результат отправки на извлечение неизвестен. Отмена пока не подтверждена; новое извлечение не запускается.":current?.error_code?`Код ошибки: ${current.error_code}`:"";
    if(!current){find("lifecycle-progress").hidden=true;find("lifecycle-clock").textContent="";find("lifecycle-id").textContent="";find("lifecycle-expiry").textContent="";find("lifecycle-steps").querySelectorAll("li").forEach(node=>node.dataset.state="waiting");onChange?.();return;}
    const view=progressView(current),progress=find("lifecycle-bar");
    find("lifecycle-progress").hidden=false;find("lifecycle-progress-text").textContent=view.text;
    find("lifecycle-percent").textContent=view.percent===null?"":`${Math.round(view.percent)}%`;
    if(view.percent===null)progress.removeAttribute("value");else progress.value=view.percent;
    const fileCount=current.file_count;
    find("lifecycle-package").textContent=current.extracting_applicable?`${fileCount===null?"Источник":`Файлов: ${fileCount}`} · ${current.total_pages===null?"Страницы определяются":`Выбрано страниц: ${current.total_pages}`}${current.total_items===null?" · Определяется количество позиций":` · Позиций: ${current.total_items}`}`:`Готовый JSON · Позиций: ${current.total_items??"определяется"}`;
    find("lifecycle-files").replaceChildren(...current.files.map(file=>{
      const value=fileProgress(file),row=element("div","job-lifecycle__file");
      const symbol=value.state==="failed"?"!":value.state==="cancelled"?"■":value.state==="completed"?"✓":value.state==="running"?"◉":"○";
      row.append(element("span","",symbol),element("span","",(file.source_file||`Файл ${file.file_index+1}`)+(value.state==="failed"?" · Ошибка обработки":value.state==="cancelled"?" · Отменено":"")),element("span","",value.total?`${value.done}/${value.total}`:"—"));return row;
    }));
    const phase={queued:1,extracting:2,matching:3,awaiting_review:4,approved:5}[current.status];
    find("lifecycle-steps").querySelectorAll("li").forEach((node,index)=>{
      node.dataset.state=index===2&&!current.extracting_applicable?"skipped":phase===undefined?"waiting":index<phase?"done":index===phase?"active":"waiting";
    });
    find("lifecycle-id").textContent=`UUID: ${current.job_id}`;
    find("lifecycle-expiry").textContent=`Доступно до ${new Date(current.expires_at*1000).toLocaleString("ru")}. Просмотр срок не продлевает.`;
    onChange?.();
  }
  function renderJournal() {
    /** Хранит в DOM ограниченную metadata историю, без source/денежных JSON. */
    const values=[...records.values()].slice(-200);
    find("lifecycle-journal").replaceChildren(...values.map(record=>element("li","",`${new Date(record.at*1000).toLocaleTimeString("ru")} · ${jobStage(record.data)}${record.data.current_pdf_page?` · страница ${record.data.current_pdf_page}`:""}`)));
    find("lifecycle-event-count").textContent=`(${current?.last_event_id||0})`;
    const last=values.at(-1);
    find("lifecycle-last-event").textContent=last?`Последнее событие: ${jobStage(last.data)}${last.data.current_pdf_page?` · страница ${last.data.current_pdf_page}`:""}`:current?`Последнее обновление: ${new Date(current.updated_at*1000).toLocaleTimeString("ru")}`:"Последнее событие появится после приёма задания";
  }
  async function loadHistory() {
    /** История/active count приходят из БД и сохраняются при переключении вкладок. */
    const value=await api(`/api/v1/${family}?limit=100`);
    const selected=current?.job_id||"";
    historySelect.replaceChildren(element("option","","Выберите задание"));historySelect.firstChild.value="";
    for(const item of value.items){const option=element("option","",`${jobLabels[item.status]||item.status} · ${item.files[0]?.source_file||item.job_id} · ${new Date(item.created_at*1000).toLocaleTimeString("ru")}`);option.value=item.job_id;option.selected=item.job_id===selected;historySelect.append(option);}
    find("job-active-count").textContent=`Активных: ${value.active_total}`;
    return value;
  }
  function expire(reason=410) {
    /** Удаляет временные данные панели и закрывает stream при 404/410/session expiry. */
    stop();current=null;records.clear();cursor=0;retrySubmit=null;submission.accepted();
    find("lifecycle-files").replaceChildren();historySelect.replaceChildren();find("lifecycle-package").textContent="Задание недоступно";
    find("lifecycle-id").textContent="";find("lifecycle-expiry").textContent="";dialog.close();
    render();renderJournal();find("lifecycle-stage").textContent=reason===410?jobLabels.expired:"Задание не найдено или недоступно";
    onExpired?.(reason);loadHistory().catch(()=>{});
  }
  async function readSnapshot(expected=epoch) {
    /** Один metadata GET; запрос/ответ прошлого задания не меняет текущую таблицу. */
    request?.abort();request=new AbortController();
    const value=await api(`${current.api_root}/${current.job_id}/snapshot`,{signal:request.signal});
    if(expected!==epoch)return null;
    if(!knownState(value.status))throw new Error(`Неизвестное состояние задания: ${value.status}`);
    const initial=!current.status;
    current=value;find("lifecycle-connection").textContent="";if(initial)cursor=Math.max(0,(value.journal_start_id||1)-1);onState?.(value);render();renderJournal();
    if(value.expires_at*1000<=Date.now()){expire();return null;}
    if(!processingStates.has(value.status)){source?.close();source=null;await onReady?.(value);}
    return value;
  }
  function reconnect(error,expected=epoch) {
    /** При transient failure оставляет последние факты; fallback имеет backoff и Retry-After. */
    if(expected!==epoch||!current)return;
    if([401,403,404,410].includes(error?.status)){expire(error.status);return;}
    source?.close();source=null;clearTimeout(timer);failures++;
    find("lifecycle-connection").textContent="Соединение потеряно, восстанавливаем подключение. Последний прогресс сохранён.";
    timer=setTimeout(async()=>{
      if(expected!==epoch)return;
      try{const value=await readSnapshot(expected);if(value&&processingStates.has(value.status))connect(expected);}
      catch(next){if(next.name!=="AbortError")reconnect(next,expected);}
    },Math.max(error?.retryAfter*1000||0,Math.min(15000,1000*2**Math.min(failures,4))));
  }
  function connect(expected=epoch) {
    /** При живом SSE второго постоянного polling нет; события принадлежат журналу Mapper. */
    if(!current||(!processingStates.has(current.status)&&cursor>=current.last_event_id))return;
    source?.close();clearTimeout(timer);
    if(!globalThis.EventSource){reconnect(null,expected);return;}
    const stream=new EventSource(`${current.api_root}/${current.job_id}/events?after=${cursor}`);source=stream;
    stream.onopen=()=>{if(expected===epoch){failures=0;find("lifecycle-connection").textContent="";}};
    stream.addEventListener("lifecycle",event=>{
      if(expected!==epoch)return;
      const record=JSON.parse(event.data);
      if(records.has(record.id))return;records.set(record.id,record);cursor=Math.max(cursor,record.id);
      if(knownState(record.data.status))current.status=record.data.status;
      while(records.size>200)records.delete(records.keys().next().value);
      clearTimeout(timer);timer=setTimeout(()=>readSnapshot(expected).catch(error=>{if(error.name!=="AbortError")reconnect(error,expected);}),100);
    });
    stream.addEventListener("reset",()=>{stream.close();source=null;cursor=0;records.clear();delete current.status;reconnect(null,expected);});
    stream.addEventListener("access_expired",()=>expire());
    stream.addEventListener("reconnect",()=>{stream.close();source=null;timer=setTimeout(()=>connect(expected),250);});
    stream.onerror=()=>{if(source===stream&&expected===epoch){stream.close();source=null;if(processingStates.has(current.status))reconnect({retryAfter:15},expected);}};
  }
  async function watch(identifier) {
    /** Привязывает stream к одному существующему UUID; GET не создаёт новое задание. */
    stop();const expected=epoch;records.clear();cursor=0;failures=0;submission.accepted();retrySubmit=null;
    current={job_id:identifier,api_root:`/api/v1/${family==="invoices"?"invoices":"mappings"}`};
    try{await readSnapshot(expected);if(expected===epoch&&current)connect(expected);await loadHistory();}
    catch(error){if(error.name!=="AbortError"){if(!current?.status){find("lifecycle-stage").textContent="Задание принято · получение состояния…";panel.setAttribute("aria-busy","false");onChange?.();}reconnect(error,expected);}}
  }
  async function restore() {
    /** Явный URL приоритетен; затем наиболее новое active, затем review, иначе новая форма. */
    try{
      const params=new URLSearchParams(location.search),id=params.get("job");await loadHistory();
      if(id&&validUuid(id)){await onOpen(id);return;}
      if(params.get("new")==="1")return;
      const active=await api(`/api/v1/${family}?state=active&limit=1`);
      const pending=active.items.length?active:await api(`/api/v1/${family}?state=awaiting_review&limit=1`);
      if(pending.items.length)await onOpen(pending.items[0].job_id);
    }catch(error){notify(error.message,true);}finally{if(submission.state==="restoring"){submission.reset();render();}}
  }
  function begin(signature,retry) {
    /** Устанавливает logical lock синхронно; повтор после сетевой ошибки использует прежний key. */
    if(Date.now()<cooldown)return null;
    const key=submission.begin(signature);if(!key)return null;
    retrySubmit=retry;render();return key;
  }
  function failed(error) {
    /** Не принимает потерю ответа за отсутствие задания; 429 уважает server cooldown. */
    submission.failed(error.status);cooldown=error.status===429?Date.now()+(error.retryAfter||3)*1000:0;
    find("lifecycle-connection").textContent=error.message;render();
    if(submission.state==="uncertain")loadHistory().catch(()=>{});
  }
  find("job-retry-submit").addEventListener("click",()=>retrySubmit?.());
  find("job-new").addEventListener("click",()=>{
    stop();current=null;records.clear();cursor=0;submission.reset();cooldown=0;retrySubmit=null;
    const url=new URL(location.href);url.searchParams.delete("job");url.searchParams.delete("source_job_id");url.searchParams.set("new","1");
    // window.history отдельно от select истории.
    globalThis.history.replaceState(null,"",url);onNew?.();find("lifecycle-files").replaceChildren();find("lifecycle-package").textContent="Выберите входные документы или готовый JSON";find("lifecycle-connection").textContent="";render();renderJournal();loadHistory().catch(()=>{});
  });
  historySelect.addEventListener("change",()=>{if(validUuid(historySelect.value))onOpen(historySelect.value);});
  find("job-history-refresh").addEventListener("click",()=>Promise.all([loadHistory(),current?readSnapshot():null]).catch(error=>notify(error.message,true)));
  find("job-cancel").addEventListener("click",()=>{document.querySelector("[data-job-cancel-description]").textContent=current.status==="awaiting_review"?"Вы откажетесь от неутверждённого результата.":current.api_root.endsWith("mappings")&&current.extracting_applicable?"Будет остановлено сопоставление. Извлечение, запущенное вне Mapper, продолжится.":"Отмена будет запрошена. Текущий шаг может завершиться до остановки.";dialog.showModal();});
  document.querySelector("[data-job-cancel-back]").addEventListener("click",()=>dialog.close());
  document.querySelector("[data-job-cancel-confirm]").addEventListener("click",async()=>{
    if(!current||cancelBusy)return;cancelBusy=true;render();
    try{await api(`${current.api_root}/${current.job_id}/cancel`,{method:"POST"});dialog.close();await readSnapshot();if(current&&processingStates.has(current.status))connect();}
    catch(error){notify(error.message,true);}finally{cancelBusy=false;render();}
  });
  const tick=setInterval(()=>{
    if(current?.expires_at&&current.expires_at*1000<=Date.now()){expire();return;}
    if(current?.created_at)find("lifecycle-clock").textContent=`Прошло ${Math.floor((Date.now()/1000-current.created_at)/60)} мин`;
    if(cooldown){if(Date.now()>=cooldown)cooldown=0;onChange?.();}
  },1000);
  window.addEventListener("pagehide",()=>{stop();clearInterval(tick);});
  window.addEventListener("focus",()=>{if(current)readSnapshot().catch(error=>reconnect(error));});
  render();return {watch,restore,begin,failed,invalidate:expire,canSubmit:()=>submission.state==="idle"&&Date.now()>=cooldown,
                   submitLabel:fallback=>Date.now()<cooldown?`Повтор через ${Math.ceil((cooldown-Date.now())/1000)} с`:submission.state==="submitting"?jobLabels.submitting:current?.status?jobLabels[current.status]||fallback:submission.state==="uncertain"?"Исход отправки неизвестен":fallback,
                   changed:()=>submission.changed(),isUncertain:()=>submission.state==="uncertain",stop};
}
