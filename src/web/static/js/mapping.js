// src/web/static/js/mapping.js
/** Ожидание durable задания, таблица, фильтры и пагинация без клиентского алгоритма подбора. */
import { api, writeOptions } from "./api.js";
import { element } from "./dom.js";
import { sourceAttributes } from "./source-fields.js";
import {createJobWorkspace} from "./job-workspace.js";
import { createSelection } from "./selection.js";
import { canApprove, canDownloadExport, clearSelectionRequest, debounce, labels, newIdempotencyKey, pageInfo, stages, terminal, validUuid } from "./state.js";

export function createMapping(notify) {
  /** Подключает элементы рабочей таблицы и возвращает восстановление задания по ID. */
  let jobId, revision = 0, offset = 0, timer, expiration, generation = 0;
  let resultGeneration = 0;
  let currentStatus = "queued";
  let mutationBusy = false,submitBusy=false,workspace,apiRoot="/api/v1/mappings";
  let submitSequence=0;
  const pdfForm=document.querySelector("[data-specification-upload]");
  const approveButton = document.querySelector("[data-approve]");
  let pendingSubmit;
  async function submitOnce(path, payload) {
    /** Повтор после потери HTTP-ответа использует прежний ключ вместо создания копии задания. */
    const signature = path + JSON.stringify(payload);
    if (pendingSubmit?.signature !== signature) pendingSubmit = { signature, key: newIdempotencyKey() };
    const result = await api(path, writeOptions(payload, pendingSubmit.key));
    pendingSubmit = undefined; return result;
  }
  const limit = 25, rowsNode = document.querySelector("[data-rows]");
  const filter = document.querySelector("[data-filter]"), status = document.querySelector("[data-status]");
  const form = document.querySelector("[data-submit]"), input = form.elements.source_job_id;
  const importInput = document.querySelector("[data-import]");
  const choose = createSelection(async (saved = true) => {
    await poll();
    notify(saved ? "Выбор сохранён в базе данных" : "Таблица обновлена. Повторите свой выбор после изменения в другой вкладке.", !saved);
  },{root:()=>apiRoot});

  function disableExports(disabled) {
    /** Не позволяет скачать чужое, незавершённое или истёкшее задание из UI. */
    document.querySelectorAll("[data-export]").forEach(link => {
      const unavailable = disabled || !canDownloadExport(link.dataset.export, currentStatus);
      link.setAttribute("aria-disabled", String(unavailable));
      if (unavailable) link.removeAttribute("href");
      else {const kind=link.dataset.export,managed=apiRoot.endsWith("specifications"),base=managed&&kind!=="source.json"?apiRoot:"/api/v1/mappings";const file=managed&&kind==="approved.json"?"result.json":managed&&kind==="result.json"?"draft.json":kind;link.href=`${base}/${jobId}/${file}`;}
    });
  }
  function expire(fromLifecycle=false,reason=410) {
    submitSequence++;
    /** Убирает временный документ из DOM по TTL, даже если вкладка оставалась открытой. */
    if(!fromLifecycle&&workspace){workspace.invalidate();return;}workspace?.stop();form.reset();pdfForm?.reset();clearTimeout(expiration);generation++; resultGeneration++; currentStatus = "expired"; clearTimeout(timer); disableExports(true); rowsNode.replaceChildren();
    approveButton.disabled = true;
    document.querySelector("[data-empty]").hidden = false;
    document.querySelector("[data-empty]").textContent = reason===410?"Срок хранения задания истёк. Создайте новое сопоставление.":"Задание не найдено или недоступно. Создайте новое сопоставление.";
    choose.reset();importInput.value="";jobId=undefined;pendingSubmit=undefined;
    document.querySelector("[data-job-id]").textContent="";
    document.querySelectorAll("[data-count]").forEach(node=>node.textContent="—");
    document.querySelector("[data-candidates]").replaceChildren();
    document.querySelector("[data-search-results]").replaceChildren();
    document.querySelector("[data-source-caption]").textContent = "";
    document.querySelector("[data-source-meta]").textContent = "";
    document.querySelector("[data-source-file]").textContent = "";
    notify(reason===410?"Срок хранения задания истёк":"Задание не найдено или недоступно", true);
  }
  function setMutationBusy(busy) {
    /** Не допускает одновременной очистки и утверждения из одной таблицы. */
    mutationBusy = busy;
    approveButton.disabled = !canApprove(currentStatus, busy);
    rowsNode.querySelectorAll("[data-clear-selection], [data-select-row]").forEach(button => { button.disabled = busy || currentStatus === "approved"; });
  }
  async function clearSelected(row) {
    /** Явный крестик сохраняет подтверждённый null через тот же owner/revision API. */
    if (mutationBusy || currentStatus === "approved") return;
    const expectedJob = jobId, expectedGeneration = generation;
    const request = clearSelectionRequest(jobId, row.index, revision);request.path=request.path.replace("/api/v1/mappings",apiRoot);
    setMutationBusy(true);
    try {
      await api(request.path, request.options);
      if (expectedJob === jobId && expectedGeneration === generation) notify("Соответствие удалено. Отсутствие подтверждено человеком.");
    } catch (error) {
      if (expectedJob === jobId && expectedGeneration === generation) notify(error.status === 409 ? "Результат изменён в другой вкладке. Таблица обновлена; проверьте выбор перед повтором." : error.message, true);
    } finally {
      setMutationBusy(false);
      if (expectedJob === jobId && expectedGeneration === generation) {
        await poll();
        rowsNode.querySelector(`[data-select-row="${row.index}"]`)?.focus();
      }
    }
  }
  function renderRow(row) {
    /** Создаёт семантическую строку с независимыми исходными и 1С-полями. */
    const tr = element("tr"); tr.append(element("td", "", String(row.index + 1)));
    const source = element("td"); source.append(element("div", "match-table__name", row.source.Caption), element("div", "match-table__meta", `стр. ${row.source.Pages} · ${row.source.UserComment1}`));
    for (const value of sourceAttributes(row.source)) source.append(element("div", "match-table__attribute", value));
    tr.append(source, element("td", "match-table__name", `${row.source.Units} · ${row.source.Quantity}`));
    const target = element("td");
    const targetHead = element("div", "match-table__target");
    targetHead.append(element("div", "match-table__name", row.selection?.name || "Соответствие не выбрано"));
    if (row.selection) {
      const clear = element("button", "match-table__clear", "×");
      clear.type = "button"; clear.dataset.clearSelection = String(row.index);
      clear.setAttribute("aria-label", `Очистить соответствие для строки ${row.index + 1}`);
      clear.title = "Оставить позицию без соответствия"; clear.disabled = currentStatus === "approved" || mutationBusy;
      clear.addEventListener("click", () => clearSelected(row)); targetHead.append(clear);
    }
    target.append(targetHead);
    if (row.selection) {
      target.append(element("div", "match-table__code", row.selection.code));
      target.append(element("div", "match-table__meta", row.selection.onec_guid ? `GUID 1С: ${row.selection.onec_guid}` : "GUID 1С отсутствует"));
    }
    tr.append(target);
    const score = !row.selection ? "—" : row.status === "manual" ? "ручной" : row.candidates[0]?.score.toFixed(3) || "—";
    tr.append(element("td", `match-table__score match-table__score--${row.status}`, score));
    const state = element("td"); state.append(element("span", `status status--${row.status}`, labels[row.status]));
    if (row.confirmed) state.append(element("small", "match-table__meta", "Подтверждено человеком"));
    tr.append(state);
    const action = element("td"), button = element("button", "button match-table__button", "Выбрать ↗");
    button.type = "button"; button.setAttribute("aria-label", `Выбрать соответствие для строки ${row.index + 1}`);
    button.dataset.selectRow = String(row.index); button.disabled = currentStatus === "approved" || mutationBusy;
    button.addEventListener("click", () => choose(jobId, row, revision)); action.append(button); tr.append(action); return tr;
  }
  async function loadRows() {
    /** Отбрасывает устаревшие ответы фильтра, чтобы медленный запрос не заменил новую страницу. */
    const request = ++resultGeneration, expectedJob = jobId;
    try {
      const query = new URLSearchParams({ q: filter.value, status: status.value, offset, limit });
      const data = await api(`${apiRoot}/${jobId}/${apiRoot.endsWith("specifications")?"rows":"result"}?${query}`);
      if (request !== resultGeneration || expectedJob !== jobId) return;
      revision = data.revision; rowsNode.replaceChildren(...data.items.map(renderRow));
      document.querySelector("[data-source-file]").textContent = data.file_name ? `Файл: ${data.file_name}` : "";
      document.querySelector("[data-empty]").hidden = data.items.length > 0;
      document.querySelector("[data-empty]").textContent = "Нет позиций по выбранному фильтру";
      const page = pageInfo(offset, data.items.length, data.filtered_total, limit);
      document.querySelector("[data-page-info]").textContent = page.text;
      document.querySelector("[data-page]").textContent = page.page;
      document.querySelector("[data-previous]").disabled = !page.previous;
      document.querySelector("[data-next]").disabled = !page.next;
    } catch (error) { if (request === resultGeneration) { if (error.status === 410) expire(); else notify(error.message, true); } }
  }
  async function poll() {
    /** Восстанавливает durable state; ручная проверка не мешает техническому завершению. */
    clearTimeout(timer); const request = generation;
    try {
      const data = await api(`${apiRoot}/${jobId}`);
      if (request !== generation) return;
      revision = data.revision; currentStatus = data.status==="awaiting_review"?"ready_for_review":data.status;
      approveButton.disabled = !canApprove(currentStatus, mutationBusy);
      document.querySelector("[data-approval-status]").textContent = data.status === "approved" ? `Результат утверждён ${new Date(data.approved_at * 1000).toLocaleString("ru")}. Финальный JSON доступен для 1С.` : `«Утвердить результат» подтвердит текущие соответствия и пустые строки сразу во всей спецификации. Спорных строк: ${data.unresolved_count}. Ошибочный выбор можно предварительно убрать крестиком.`;
      document.querySelector("[data-job-status]").textContent = `${stages[currentStatus] || data.stage}${data.error_code ? ` · ${data.error_code}` : ""}`;
      document.querySelector("[data-pipeline]").textContent = `${stages[data.status] || data.stage} · хранение до ${new Date(data.expires_at * 1000).toLocaleTimeString("ru")}. ${data.status === "approved" ? "Текущие соответствия и пустые строки подтверждены человеком." : "Автоподбор не означает проверку человеком."}`;
      document.querySelector("[data-count='total']").textContent = data.total;
      document.querySelector("[data-count='matched']").textContent = data.counts.matched + data.counts.manual;
      for (const name of ["review", "not_found"]) document.querySelector(`[data-count='${name}']`).textContent = data.counts[name];
      clearTimeout(expiration); expiration = setTimeout(expire, Math.max(0, data.expires_at * 1000 - Date.now()));
      if (["complete", "ready_for_review", "approved"].includes(currentStatus)) { disableExports(false); await loadRows(); }
      else if(currentStatus==="cancelled"){rowsNode.replaceChildren();document.querySelector("[data-empty]").hidden=false;document.querySelector("[data-empty]").textContent="Задание отменено";}
      else if(currentStatus==="failed")notify(`Обработка не выполнена: ${data.error_code}. Создайте новое задание после устранения причины.`, true);
      return true;
    } catch (error) {
      if (request !== generation) return true;
      if (error.status === 410 || error.status === 404) { expire(); return; }
      notify(error.message, true);return false;
    }
  }
  async function open(id) {
    /** Очищает предыдущий результат и закрепляет UUID в URL без хранения документа в localStorage. */
    choose.reset();generation++; resultGeneration++; clearTimeout(timer); clearTimeout(expiration); jobId = id; offset = 0;
    currentStatus = "queued"; approveButton.disabled = true;
    document.querySelector("[data-source-file]").textContent = "";
    rowsNode.replaceChildren(); disableExports(true); filter.value = ""; status.value = "all";
    document.querySelector("[data-empty]").hidden = false;
    document.querySelector("[data-empty]").textContent = "Ожидание результата сопоставления…";
    document.querySelector("[data-job-id]").textContent = `Задание: ${id}`;
    const url = new URL(location.href); url.searchParams.set("job", id); url.searchParams.delete("new");history.replaceState(null, "", url); await workspace.watch(id);
  }
  function updateInputs() {
    /** Краткий request busy не снимает logical lock активного или принятого задания. */
    const locked=submitBusy||workspace?.canSubmit()===false;
    form.querySelectorAll("input,button").forEach(node=>node.disabled=locked);importInput.disabled=locked;
    form.querySelector("button").textContent=workspace?.submitLabel("Сопоставить")||"Сопоставить";
    if(pdfForm){[...pdfForm.elements].forEach(node=>node.disabled=locked);pdfForm.querySelector("button").textContent=workspace?.submitLabel("Извлечь и сопоставить")||"Извлечь и сопоставить";}
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();if(submitBusy)return;const id=input.value.trim();
    if(!validUuid(id)){notify("Укажите полный UUID задания извлечения",true);input.focus();return;}
    let key;try{key=workspace.begin("external:"+id,()=>form.requestSubmit());}catch(error){notify(error.message,true);return;}
    if(!key)return;const submission=++submitSequence;submitBusy=true;updateInputs();
    try{const job=await api("/api/v1/mappings",writeOptions({source_job_id:id},key));if(submission!==submitSequence)return;await open(job.job_id);}
    catch(error){if(submission===submitSequence){workspace.failed(error);notify(error.message,true);}}finally{if(submission===submitSequence){submitBusy=false;updateInputs();}}
  });
  pdfForm?.addEventListener("submit",async event=>{
    event.preventDefault();if(submitBusy)return;const file=pdfForm.elements.file.files[0];if(!file)return;
    const pages=pdfForm.elements.pages.value,project=pdfForm.elements.project_code.value;
    let key;try{key=workspace.begin(JSON.stringify([file.name,file.size,file.lastModified,pages,project]),()=>pdfForm.requestSubmit());}catch(error){notify(error.message,true);return;}
    if(!key)return;const submission=++submitSequence;submitBusy=true;updateInputs();
    try{const body=new FormData();body.append("file",file);body.append("pages",pages);body.append("project_code",project);const job=await api("/api/v1/specifications",{method:"POST",body,headers:{"Idempotency-Key":key}});if(submission!==submitSequence)return;pdfForm.elements.file.value="";await open(job.job_id);}
    catch(error){if(submission===submitSequence){workspace.failed(error);notify(error.message,true);}}finally{if(submission===submitSequence){submitBusy=false;updateInputs();}}
  });
  form.addEventListener("input",()=>workspace.changed());pdfForm?.addEventListener("input",()=>workspace.changed());
  approveButton.addEventListener("click", async () => {
    if (!jobId || approveButton.disabled || mutationBusy) return;
    const expectedJob = jobId, expectedGeneration = generation;
    setMutationBusy(true);
    try { await submitOnce(`${apiRoot}/${jobId}/approve`, { revision }); if (expectedJob === jobId && expectedGeneration === generation) notify("Все текущие соответствия и пустые строки подтверждены. Скачайте финальный JSON для 1С."); }
    catch (error) { if (expectedJob === jobId && expectedGeneration === generation) notify(error.message, true); }
    finally { setMutationBusy(false); if (expectedJob === jobId && expectedGeneration === generation) await poll(); }
  });
  async function importJson(file) {
    /** JSON использует общий logical lock и тот же key при потерянном ответе POST. */
    if(!file||submitBusy)return;if(file.size>15000000){notify("JSON превышает допустимый размер 15 МБ",true);return;}
    let key;try{key=workspace.begin(`json:${file.name}:${file.size}:${file.lastModified}`,()=>importJson(file));}catch(error){notify(error.message,true);return;}
    if(!key)return;const submission=++submitSequence;submitBusy=true;updateInputs();let sent=false;
    try{const document=JSON.parse(await file.text());sent=true;const job=await api("/api/v1/mappings/import-json",writeOptions(document,key));if(submission!==submitSequence)return;await open(job.job_id);importInput.value="";}
    catch(error){if(submission===submitSequence){if(!sent)error.status=422;workspace.failed(error);notify(error.message,true);}}finally{if(submission===submitSequence){submitBusy=false;updateInputs();}}
  }
  importInput.addEventListener("change",()=>importJson(importInput.files[0]));
  const filtered = debounce(() => { offset = 0; if (jobId) loadRows(); });
  filter.addEventListener("input", filtered); status.addEventListener("change", filtered);
  document.querySelector("[data-previous]").addEventListener("click", () => { offset = Math.max(0, offset - limit); loadRows(); });
  document.querySelector("[data-next]").addEventListener("click", () => { offset += limit; loadRows(); });
  const source = new URLSearchParams(location.search).get("source_job_id"); if (source) input.value = source;
  function snapshotChanged(value) {
    /** Сохраняет legacy представление статусов таблицы, используя фактический API root. */
    apiRoot=value.api_root;revision=value.revision;currentStatus=value.status==="awaiting_review"?"ready_for_review":value.status;
    document.querySelector("[data-job-status]").textContent=stages[currentStatus]||value.status;
    document.querySelector("[data-count='total']").textContent=value.total_items??"—";
    document.querySelector("[data-count='matched']").textContent=value.matched_count??"—";
    for(const name of ["review","not_found"])document.querySelector(`[data-count='${name}']`).textContent=value.total_items===null?"—":value.counts[name]||0;
    approveButton.disabled=!canApprove(currentStatus,mutationBusy);disableExports(!["complete","ready_for_review","approved"].includes(currentStatus));
    const panel=document.querySelector("[data-mapping-input-panel]");if(panel)panel.open=false;
    const review=document.querySelector("[data-mapping-review-panel]");if(review)review.hidden=!["awaiting_review","approved"].includes(value.status);
    updateInputs();
  }
  function resetWorkspace() {
    submitSequence++;choose.reset();
    /** Новая форма не останавливает прежнюю обработку и не удаляет серверную историю. */
    generation++;resultGeneration++;clearTimeout(timer);clearTimeout(expiration);jobId=undefined;currentStatus="idle";apiRoot="/api/v1/mappings";pendingSubmit=undefined;submitBusy=false;
    input.value="";importInput.value="";pdfForm?.reset();rowsNode.replaceChildren();disableExports(true);approveButton.disabled=true;
    document.querySelector("[data-source-file]").textContent="";document.querySelector("[data-job-id]").textContent="";
    document.querySelectorAll("[data-count]").forEach(node=>node.textContent="—");document.querySelector("[data-empty]").hidden=false;document.querySelector("[data-empty]").textContent="Загрузите новую спецификацию или готовый JSON";
    const panel=document.querySelector("[data-mapping-input-panel]");if(panel)panel.open=true;
    const review=document.querySelector("[data-mapping-review-panel]");if(review)review.hidden=true;updateInputs();
  }
  workspace=createJobWorkspace({family:"specifications",onOpen:open,onState:snapshotChanged,onNew:resetWorkspace,onExpired:reason=>expire(true,reason),onChange:updateInputs,
    onReady:async()=>{if(!await poll())throw new Error("Не удалось обновить результат. Восстанавливаем подключение.");},notify});
  updateInputs();document.querySelector("[data-job-status]").textContent="Выберите PDF, UUID извлечения или готовый JSON";
  return Object.assign(open,{restore:workspace.restore});
}
