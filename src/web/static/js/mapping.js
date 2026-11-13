// src/web/static/js/mapping.js
/** Ожидание durable задания, таблица, фильтры и пагинация без клиентского алгоритма подбора. */
import { api, writeOptions } from "./api.js";
import { element } from "./dom.js";
import { sourceAttributes } from "./source-fields.js";
import { createSelection } from "./selection.js";
import { canApprove, canDownloadExport, clearSelectionRequest, debounce, labels, newIdempotencyKey, pageInfo, stages, terminal, validUuid } from "./state.js";

export function createMapping(notify) {
  /** Подключает элементы рабочей таблицы и возвращает восстановление задания по ID. */
  let jobId, revision = 0, offset = 0, timer, expiration, generation = 0;
  let resultGeneration = 0;
  let currentStatus = "queued";
  let mutationBusy = false;
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
  });

  function disableExports(disabled) {
    /** Не позволяет скачать чужое, незавершённое или истёкшее задание из UI. */
    document.querySelectorAll("[data-export]").forEach(link => {
      const unavailable = disabled || !canDownloadExport(link.dataset.export, currentStatus);
      link.setAttribute("aria-disabled", String(unavailable));
      if (unavailable) link.removeAttribute("href");
      else link.href = `/api/v1/mappings/${jobId}/${link.dataset.export}`;
    });
  }
  function expire() {
    /** Убирает временный документ из DOM по TTL, даже если вкладка оставалась открытой. */
    generation++; resultGeneration++; currentStatus = "expired"; clearTimeout(timer); disableExports(true); rowsNode.replaceChildren();
    approveButton.disabled = true;
    document.querySelector("[data-empty]").hidden = false;
    document.querySelector("[data-empty]").textContent = "Срок хранения задания истёк. Создайте новое сопоставление.";
    document.querySelector("[data-drawer]").close();
    document.querySelector("[data-candidates]").replaceChildren();
    document.querySelector("[data-search-results]").replaceChildren();
    document.querySelector("[data-source-caption]").textContent = "";
    document.querySelector("[data-source-meta]").textContent = "";
    document.querySelector("[data-source-file]").textContent = "";
    notify("Срок хранения задания истёк", true);
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
    const request = clearSelectionRequest(jobId, row.index, revision);
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
      const data = await api(`/api/v1/mappings/${jobId}/result?${query}`);
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
      const data = await api(`/api/v1/mappings/${jobId}`);
      if (request !== generation) return;
      revision = data.revision; currentStatus = data.status;
      approveButton.disabled = !canApprove(data.status, mutationBusy);
      document.querySelector("[data-approval-status]").textContent = data.status === "approved" ? `Результат утверждён ${new Date(data.approved_at * 1000).toLocaleString("ru")}. Финальный JSON доступен для 1С.` : `«Утвердить результат» подтвердит текущие соответствия и пустые строки сразу во всей спецификации. Спорных строк: ${data.unresolved_count}. Ошибочный выбор можно предварительно убрать крестиком.`;
      document.querySelector("[data-job-status]").textContent = `${stages[data.status] || data.stage}${data.error_code ? ` · ${data.error_code}` : ""} · ${data.processed}/${data.total}`;
      document.querySelector("[data-pipeline]").textContent = `${stages[data.status] || data.stage} · хранение до ${new Date(data.expires_at * 1000).toLocaleTimeString("ru")}. ${data.status === "approved" ? "Текущие соответствия и пустые строки подтверждены человеком." : "Автоподбор не означает проверку человеком."}`;
      document.querySelector("[data-count='total']").textContent = data.total;
      document.querySelector("[data-count='matched']").textContent = data.counts.matched + data.counts.manual;
      for (const name of ["review", "not_found"]) document.querySelector(`[data-count='${name}']`).textContent = data.counts[name];
      clearTimeout(expiration); expiration = setTimeout(expire, Math.max(0, data.expires_at * 1000 - Date.now()));
      if (["complete", "ready_for_review", "approved"].includes(data.status)) { disableExports(false); await loadRows(); }
      else if (!terminal(data.status)) timer = setTimeout(poll, 3000);
      else notify(`Обработка не выполнена: ${data.error_code}. Создайте новое задание после устранения причины.`, true);
    } catch (error) {
      if (request !== generation) return;
      if (error.status === 410 || error.status === 404) { expire(); return; }
      notify(error.message, true); timer = setTimeout(poll, 5000);
    }
  }
  async function open(id) {
    /** Очищает предыдущий результат и закрепляет UUID в URL без хранения документа в localStorage. */
    generation++; resultGeneration++; clearTimeout(timer); clearTimeout(expiration); jobId = id; offset = 0;
    currentStatus = "queued"; approveButton.disabled = true;
    document.querySelector("[data-source-file]").textContent = "";
    rowsNode.replaceChildren(); disableExports(true); filter.value = ""; status.value = "all";
    document.querySelector("[data-empty]").hidden = false;
    document.querySelector("[data-empty]").textContent = "Ожидание результата сопоставления…";
    document.querySelector("[data-job-id]").textContent = `Задание: ${id}`;
    const url = new URL(location.href); url.searchParams.set("job", id); history.replaceState(null, "", url); await poll();
  }
  form.addEventListener("submit", async event => {
    event.preventDefault(); const id = input.value.trim();
    if (!validUuid(id)) { notify("Укажите полный UUID задания извлечения", true); input.focus(); return; }
    const button = form.querySelector("button"); button.disabled = true;
    try { const job = await submitOnce("/api/v1/mappings", { source_job_id: id }); await open(job.job_id); }
    catch (error) { notify(error.message, true); } finally { button.disabled = false; }
  });
  approveButton.addEventListener("click", async () => {
    if (!jobId || approveButton.disabled || mutationBusy) return;
    const expectedJob = jobId, expectedGeneration = generation;
    setMutationBusy(true);
    try { await submitOnce(`/api/v1/mappings/${jobId}/approve`, { revision }); if (expectedJob === jobId && expectedGeneration === generation) notify("Все текущие соответствия и пустые строки подтверждены. Скачайте финальный JSON для 1С."); }
    catch (error) { if (expectedJob === jobId && expectedGeneration === generation) notify(error.message, true); }
    finally { setMutationBusy(false); if (expectedJob === jobId && expectedGeneration === generation) await poll(); }
  });
  importInput.addEventListener("change", async event => {
    const file = event.target.files[0]; if (!file) return;
    try {
      if (file.size > 15000000) throw new Error("JSON превышает допустимый размер 15 МБ");
      const document = JSON.parse(await file.text());
      const job = await submitOnce("/api/v1/mappings/import-json", document); await open(job.job_id);
    } catch (error) { notify(error.message, true); } finally { event.target.value = ""; }
  });
  const filtered = debounce(() => { offset = 0; if (jobId) loadRows(); });
  filter.addEventListener("input", filtered); status.addEventListener("change", filtered);
  document.querySelector("[data-previous]").addEventListener("click", () => { offset = Math.max(0, offset - limit); loadRows(); });
  document.querySelector("[data-next]").addEventListener("click", () => { offset += limit; loadRows(); });
  const source = new URLSearchParams(location.search).get("source_job_id"); if (source) input.value = source;
  // Сессия и обработчики готовы: разрешаем ввод независимо от фоновой сводки каталога.
  input.disabled = false; importInput.disabled = false; form.querySelector("button").disabled = false;
  document.querySelector("[data-job-status]").textContent = "Укажите ID задания извлечения или импортируйте готовый JSON";
  return open;
}
