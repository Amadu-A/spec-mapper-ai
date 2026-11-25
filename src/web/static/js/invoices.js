// src/web/static/js/invoices.js
/** Invoice workspace: загрузка/ожидание, read-only финансы и общие selection/approval API. */
import {api,writeOptions} from "./api.js";
import {element} from "./dom.js";
import {createSelection} from "./selection.js";
import {debounce,newIdempotencyKey,pageInfo} from "./state.js";
import {invoiceApprovalPayload,invoiceCanApprove,invoiceSelectionSource,invoiceStages,warningLabels} from "./invoice-state.js";
import {createJobWorkspace} from "./job-workspace.js";
import {invoiceRow} from "./invoice-table.js";
import {createInvoicePreview} from "./invoice-preview.js";

export function createInvoices(notify) {
  /** Подключает отдельный профиль и возвращает восстановление job по URL без localStorage. */
  let submitSequence=0;
  const files=document.querySelector("[data-invoice-files]"),form=document.querySelector("[data-invoice-upload]");
  const uploadButton=document.querySelector("[data-invoice-submit]"),jsonInput=document.querySelector("[data-invoice-import]");
  const rows=document.querySelector("[data-invoice-rows]"),approve=document.querySelector("[data-invoice-approve]");
  const filter=document.querySelector("[data-invoice-filter]"),statusFilter=document.querySelector("[data-invoice-filter-status]");
  const warningsNode=document.querySelector("[data-invoice-warnings]"),preview=createInvoicePreview();
  let jobId,revision=0,offset=0,epoch=0,read=0,timer,expiry,busy=false,currentStatus="queued",previewAvailable=false;
  let warnings=[],acknowledged=new Set(),workspace;
  const pendingJson=new Map(),limit=25;
  const choose=createSelection(async(saved=true)=>{
    await poll();notify(saved?"Выбор сохранён":"Таблица изменена в другой вкладке; проверьте результат",!saved);
  },{root:"/api/v1/invoices",sourceView:invoiceSelectionSource});

  function updateControls() {
    /** Доступность повторяет публичное состояние; окончательное решение всегда у backend. */
    approve.disabled=!invoiceCanApprove(currentStatus,warnings,acknowledged,busy);
    const locked=busy||workspace?.canSubmit()===false;
    uploadButton.disabled=locked;files.disabled=locked;jsonInput.disabled=locked;
    document.querySelectorAll("[data-invoice-file-pages]").forEach(input=>{input.disabled=locked;});
    uploadButton.textContent=workspace?.submitLabel("Извлечь и сопоставить")||"Извлечь и сопоставить";
    rows.querySelectorAll("[data-invoice-mutable]").forEach(button=>{button.disabled=busy||currentStatus!=="awaiting_review"||button.dataset.invoiceSelectable==="false";});
    warningsNode.querySelectorAll("input").forEach(input=>{input.disabled=busy||currentStatus==="approved";});
    document.querySelectorAll("[data-invoice-export]").forEach(link=>{
      const kind=link.dataset.invoiceExport,ready=["awaiting_review","approved"].includes(currentStatus);
      const enabled=jobId&&ready&&(!["result.json","result.xlsx"].includes(kind)||currentStatus==="approved");
      link.setAttribute("aria-disabled",String(!enabled));
      if(enabled)link.href=`/api/v1/invoices/${jobId}/${kind}`;else link.removeAttribute("href");
    });
  }
  function expire(fromLifecycle=false,reason=410) {
    submitSequence++;
    /** Убирает исходные реквизиты, preview и dialog из DOM после TTL/удаления задания. */
    if(!fromLifecycle&&workspace){workspace.invalidate();return;}workspace?.stop();epoch++;read++;clearTimeout(timer);clearTimeout(expiry);currentStatus="expired";
    rows.replaceChildren();warningsNode.replaceChildren();warnings=[];acknowledged.clear();pendingJson.clear();
    files.value="";jsonInput.value="";jobId=undefined;previewAvailable=false;choose.reset();
    document.querySelector("[data-invoice-job]").textContent="";
    document.querySelectorAll("[data-invoice-count]").forEach(node=>node.textContent="—");
    document.querySelector("[data-invoice-preview]").close();
    document.querySelector("[data-invoice-file-list]").replaceChildren();
    document.querySelector("[data-invoice-status]").textContent=reason===410?"Срок хранения задания истёк":"Задание не найдено или недоступно";
    document.querySelector("[data-invoice-empty]").hidden=false;
    document.querySelector("[data-invoice-empty]").textContent="Создайте новое сопоставление счетов";
    updateControls();notify("Задание недоступно или срок хранения истёк",true);
  }
  function renderWarnings() {
    /** Показывает серверные блокеры и отдельные явные отметки для каждого документа. */
    warningsNode.replaceChildren();
    for(const warning of warnings){
      const label=element("label","invoice-warnings__item"),checkbox=element("input");
      checkbox.type="checkbox";checkbox.dataset.invoiceAcknowledge=warning.document_id;
      checkbox.checked=currentStatus==="approved"||acknowledged.has(warning.document_id);
      checkbox.disabled=currentStatus==="approved"||busy;
      checkbox.addEventListener("change",()=>{if(checkbox.checked)acknowledged.add(warning.document_id);else acknowledged.delete(warning.document_id);updateControls();});
      label.append(checkbox,element("span","",`Подтверждаю обработку документа ${warning.document_id} с предупреждениями: ${warning.codes.map(code=>warningLabels[code]||code).join("; ")}. Исходная проверка: ${warning.validation_status}.`));
      warningsNode.append(label);
    }
    if(!warnings.length)warningsNode.append(element("p","","Сервер не сообщил финансовых блокеров. Суммы не пересчитывались в Mapper."));
    updateControls();
  }
  async function loadRows(expected=epoch) {
    /** Защищает фильтр/пагинацию от устаревших HTTP-ответов и не выполняет поиск заново. */
    const request=++read;
    const query=new URLSearchParams({q:filter.value,status:statusFilter.value,offset,limit});
    const data=await api(`/api/v1/invoices/${jobId}/rows?${query}`);
    if(expected!==epoch||request!==read)return;
    revision=data.revision;warnings=data.financial_warnings;previewAvailable=data.preview_available;
    rows.replaceChildren(...data.items.map(row=>invoiceRow(row,{
      choose:row=>choose(jobId,row,revision),clear:clearRow,preview:row=>preview(jobId,row),
      locked:busy||currentStatus==="approved",previewAvailable,
    })));
    renderWarnings();
    document.querySelector("[data-invoice-empty]").hidden=data.items.length>0;
    document.querySelector("[data-invoice-empty]").textContent="Нет позиций по выбранному фильтру";
    const page=pageInfo(offset,data.items.length,data.filtered_total,limit);
    document.querySelector("[data-invoice-page-info]").textContent=page.text;
    document.querySelector("[data-invoice-page]").textContent=page.page;
    document.querySelector("[data-invoice-previous]").disabled=!page.previous;
    document.querySelector("[data-invoice-next]").disabled=!page.next;
  }
  async function poll() {
    /** Читает durable status и ставит ограниченное ожидание только технически незавершённого job. */
    clearTimeout(timer);const expected=epoch;
    try{
      const data=await api(`/api/v1/invoices/${jobId}`);
      if(expected!==epoch)return;
      currentStatus=data.status;revision=data.revision;
      document.querySelector("[data-invoice-status]").textContent=`${invoiceStages[data.status]||data.status} · ${data.processed}/${data.total}${data.error_code?` · ${data.error_code}`:""}${data.upstream_warning?" · Извлечение завершено с предупреждениями":""}`;
      document.querySelector("[data-invoice-approval]").textContent=data.status==="approved"?`Результат утверждён ${new Date(data.approved_at*1000).toLocaleString("ru")}. Исходные финансовые проверки сохранены.`:"Утверждение подтвердит все текущие соответствия и пустые строки. Отметьте финансовые предупреждения перед утверждением.";
      document.querySelectorAll("[data-invoice-count]").forEach(node=>{
        const key=node.dataset.invoiceCount;node.textContent=key==="matched"?(data.counts.matched||0)+(data.counts.manual||0):data[key];
      });
      clearTimeout(expiry);expiry=setTimeout(expire,Math.max(0,data.expires_at*1000-Date.now()));
      if(["awaiting_review","approved"].includes(data.status))await loadRows(expected);
      else if(["cancelled","failed"].includes(data.status)){rows.replaceChildren();document.querySelector("[data-invoice-empty]").hidden=false;document.querySelector("[data-invoice-empty]").textContent=data.status==="cancelled"?"Задание отменено":"Обработка завершилась ошибкой";}
      updateControls();return true;
    }catch(error){
      if(expected!==epoch)return;
      if([404,410].includes(error.status))expire();else notify(error.message,true);return false;
    }
  }
  async function open(id) {
    /** Закрепляет один внешний ID в URL и очищает состояние предыдущего документа. */
    choose.reset();document.querySelector("[data-invoice-preview]").close();epoch++;read++;clearTimeout(timer);clearTimeout(expiry);jobId=id;revision=0;offset=0;
    warnings=[];acknowledged.clear();rows.replaceChildren();currentStatus="queued";
    document.querySelector("[data-invoice-job]").textContent=`Задание: ${id}`;
    const url=new URL(location.href);url.searchParams.set("job",id);url.searchParams.delete("new");history.replaceState(null,"",url);
    updateControls();await workspace.watch(id);
  }
  async function submitJson(path,payload) {
    /** Потерянный ответ повторяется с прежним ключом; другая revision/документ получает новый ключ. */
    const signature=path+JSON.stringify(payload);
    if(!pendingJson.has(signature))pendingJson.set(signature,newIdempotencyKey());
    const result=await api(path,writeOptions(payload,pendingJson.get(signature)));
    pendingJson.delete(signature);return result;
  }
  async function clearRow(row) {
    /** Сохраняет явный null через общий серверный CAS; исходная строка остаётся в таблице. */
    if(busy||currentStatus==="approved")return;
    busy=true;updateControls();
    try{await api(`/api/v1/invoices/${jobId}/rows/${row.index}/selection?revision=${revision}`,{method:"DELETE"});notify("Отсутствие соответствия подтверждено человеком");}
    catch(error){notify(error.message,true);}
    finally{busy=false;await poll();}
  }
  files.addEventListener("change",()=>{
    workspace.changed();const list=document.querySelector("[data-invoice-file-list]");list.replaceChildren();
    const ranges=document.querySelector(".invoice-upload__ranges");if(ranges){ranges.open=files.files.length<=3;ranges.querySelector("summary").textContent=`Файлы и физические страницы (${files.files.length})`;}
    [...files.files].forEach((file,index)=>{
      const row=element("div","invoice-upload__file"),label=element("label","",`${file.name} · страницы (пусто = все)`),input=element("input","input");
      input.id=`invoice-file-pages-${index}`;input.dataset.invoiceFilePages=String(index);input.placeholder="Например: 1-3,5";input.maxLength=2000;label.htmlFor=input.id;
      input.addEventListener("input",()=>workspace.changed());row.append(label,input);list.append(row);
    });
  });
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(busy)return;
    const selection={};document.querySelectorAll("[data-invoice-file-pages]").forEach(input=>{if(input.value.trim())selection[input.dataset.invoiceFilePages]=input.value;});
    const signature=JSON.stringify({files:[...files.files].map(file=>[file.name,file.size,file.lastModified]),selection});
    let key;try{key=workspace.begin(signature,()=>form.requestSubmit());}catch(error){notify(error.message,true);return;}
    if(!key)return;const submission=++submitSequence;busy=true;updateControls();
    try{
      const body=new FormData();[...files.files].forEach(file=>body.append("files",file));
      body.append("page_selection",JSON.stringify(selection));
      const result=await api("/api/v1/invoices",{method:"POST",body,headers:{"Idempotency-Key":key}});
      if(submission!==submitSequence)return;files.value="";await open(result.job_id);
    }catch(error){if(submission===submitSequence){workspace.failed(error);notify(error.message,true);}}finally{if(submission===submitSequence){busy=false;updateControls();}}
  });
  async function importJson(file) {
    /** Logical lock устанавливается до file.text; retry использует тот же File и key. */
    if(!file||busy)return;
    if(file.size>15000000){notify("JSON превышает лимит 15 МБ",true);return;}
    let key;try{key=workspace.begin(`json:${file.name}:${file.size}:${file.lastModified}`,()=>importJson(file));}catch(error){notify(error.message,true);return;}
    if(!key)return;const submission=++submitSequence;busy=true;updateControls();let sent=false;
    try{const payload=JSON.parse(await file.text());sent=true;const result=await api("/api/v1/invoices/import-json",writeOptions(payload,key));if(submission!==submitSequence)return;await open(result.job_id);jsonInput.value="";}
    catch(error){if(submission===submitSequence){if(!sent)error.status=422;workspace.failed(error);notify(error.message,true);}}finally{if(submission===submitSequence){busy=false;updateControls();}}
  }
  jsonInput.addEventListener("change",()=>importJson(jsonInput.files[0]));
  approve.addEventListener("click",async()=>{
    if(approve.disabled||busy)return;busy=true;updateControls();
    try{await submitJson(`/api/v1/invoices/${jobId}/approve`,invoiceApprovalPayload(revision,acknowledged));notify("Результат утверждён. Финансовые предупреждения сохранены для 1С.");}
    catch(error){notify(error.message,true);}finally{busy=false;await poll();}
  });
  const reload=()=>{if(jobId)loadRows().catch(error=>{if([404,410].includes(error.status))expire();else notify(error.message,true);});};
  const filtered=debounce(()=>{offset=0;reload();});filter.addEventListener("input",filtered);statusFilter.addEventListener("change",filtered);
  document.querySelector("[data-invoice-previous]").addEventListener("click",()=>{offset=Math.max(0,offset-limit);reload();});
  document.querySelector("[data-invoice-next]").addEventListener("click",()=>{offset+=limit;reload();});
  function snapshotChanged(value) {
    /** KPI и доступность получены из safe snapshot, без фиктивных нулей до извлечения. */
    currentStatus=value.status;revision=value.revision;
    document.querySelector("[data-invoice-status]").textContent=invoiceStages[value.status]||value.status;
    const fields={file_count:"file_count",document_count:"document_count",total:"total_items",matched:"matched_count",unresolved_count:"unresolved_count",financial_warning_count:"financial_warning_count"};
    document.querySelectorAll("[data-invoice-count]").forEach(node=>{node.textContent=value[fields[node.dataset.invoiceCount]]??"—";});
    const inputPanel=document.querySelector("[data-invoice-input-panel]");if(inputPanel)inputPanel.open=false;
    const reviewPanel=document.querySelector("[data-invoice-review-panel]");if(reviewPanel)reviewPanel.hidden=!["awaiting_review","approved"].includes(value.status);
    updateControls();
  }
  function resetWorkspace() {
    submitSequence++;choose.reset();document.querySelector("[data-invoice-preview]").close();
    /** Явное новое задание не отменяет прежнее; оно остаётся в серверной истории. */
    epoch++;read++;clearTimeout(timer);clearTimeout(expiry);jobId=undefined;currentStatus="idle";busy=false;
    warnings=[];acknowledged.clear();pendingJson.clear();rows.replaceChildren();warningsNode.replaceChildren();files.value="";jsonInput.value="";
    document.querySelector("[data-invoice-file-list]").replaceChildren();document.querySelector("[data-invoice-job]").textContent="";
    document.querySelector("[data-invoice-empty]").hidden=false;document.querySelector("[data-invoice-empty]").textContent="Загрузите новый пакет или готовый JSON";
    document.querySelectorAll("[data-invoice-count]").forEach(node=>node.textContent="—");
    const inputPanel=document.querySelector("[data-invoice-input-panel]");if(inputPanel)inputPanel.open=true;
    const reviewPanel=document.querySelector("[data-invoice-review-panel]");if(reviewPanel)reviewPanel.hidden=true;updateControls();
  }
  workspace=createJobWorkspace({family:"invoices",onOpen:open,onState:snapshotChanged,onNew:resetWorkspace,onExpired:reason=>expire(true,reason),onChange:updateControls,
    onReady:async()=>{if(!await poll())throw new Error("Не удалось обновить результат. Восстанавливаем подключение.");},notify});
  document.querySelector("[data-invoice-status]").textContent="Загрузите документы или импортируйте готовый invoice JSON";
  updateControls();return Object.assign(open,{restore:workspace.restore});
}
