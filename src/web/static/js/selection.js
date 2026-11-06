// src/web/static/js/selection.js
/** Панель ручного перевыбора; сохранение атомарно и доступно только через общий API. */
import { api } from "./api.js";
import { element, option } from "./dom.js";
import { debounce, selectionPayload } from "./state.js";

export function createSelection(onSaved) {
  /** Инициализирует native dialog и возвращает операцию открытия для строки таблицы. */
  const dialog = document.querySelector("[data-drawer]");
  const query = dialog.querySelector("[data-search-candidates]");
  const save = dialog.querySelector("[data-save-selection]");
  const errorNode = dialog.querySelector("[data-drawer-error]");
  const results = dialog.querySelector("[data-search-results]");
  const noMatch = dialog.querySelector("[data-no-match]");
  let active, selected, generation = 0, busy = false;

  function choose(guid) {
    /** Отмечает неподтверждённый выбор; persisted состояние до ответа API не меняется. */
    selected = guid; save.disabled = false;
    dialog.querySelector("[data-save-status]").textContent = "Выбор ещё не сохранён";
  }
  function showError(error) {
    /** Показывает ошибку в dialog, сохраняя выбор для осмысленного повтора. */
    errorNode.textContent = error.message; errorNode.hidden = false;
  }
  function close() {
    /** Закрывает окно только после завершения записи и отбрасывает устаревший поиск. */
    if (!busy) { generation++; dialog.close(); }
  }
  noMatch.addEventListener("change", () => choose(null));
  dialog.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", close));
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); else generation++; });
  dialog.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
  });
  dialog.addEventListener("close", () => {
    generation++; active = undefined; selected = undefined;
    results.replaceChildren(); dialog.querySelector("[data-candidates]").replaceChildren();
    dialog.querySelector("[data-source-caption]").textContent = "";
    dialog.querySelector("[data-source-meta]").textContent = "";
  });
  query.addEventListener("input", debounce(async () => {
    const request = ++generation; results.replaceChildren();
    if (!query.value.trim()) return;
    try {
      const value = await api(`/api/v1/catalog/items?q=${encodeURIComponent(query.value.trim())}`);
      if (request !== generation || !dialog.open) return;
      results.replaceChildren(...value.items.map(item => option(item, null, selected, choose)));
      if (!value.items.length) results.append(element("p", "drawer__hint", "В действующем каталоге ничего не найдено"));
    } catch (error) { if (request === generation) showError(error); }
  }));
  save.addEventListener("click", async () => {
    if (selected === undefined || busy) return;
    busy = true; save.disabled = true; errorNode.hidden = true;
    dialog.querySelector("[data-save-status]").textContent = "Сохранение…";
    try {
      await api(`/api/v1/mappings/${active.jobId}/rows/${active.row.index}/selection`, {
        method: "PUT", body: JSON.stringify(selectionPayload(selected, active.revision)),
      });
      busy = false; dialog.close(); await onSaved();
    } catch (error) {
      busy = false; save.disabled = false; showError(error);
      dialog.querySelector("[data-save-status]").textContent = "Выбор не сохранён";
      if (error.code === "revision_conflict") { save.disabled = true; await onSaved(false); }
    }
  });
  return function openSelection(jobId, row, revision) {
    /** Использует Top-K текущего серверного snapshot, сохраняя исходные поля дословно. */
    generation++; active = { jobId, row, revision }; selected = undefined;
    busy = false; save.disabled = true; noMatch.checked = false; errorNode.hidden = true;
    query.value = ""; results.replaceChildren();
    dialog.querySelector("[data-source-caption]").textContent = row.source.Caption;
    dialog.querySelector("[data-source-meta]").textContent = `${row.source.Units} · ${row.source.Quantity} · стр. ${row.source.Pages} · ${row.source.UserComment1}`;
    dialog.querySelector("[data-save-status]").textContent = "Выберите позицию для подтверждения";
    const list = dialog.querySelector("[data-candidates]");
    list.replaceChildren(...row.candidates.map(candidate => option(candidate.item, candidate, undefined, choose)));
    if (!row.candidates.length) list.append(element("p", "drawer__hint", "Top-K пуст. Используйте поиск по каталогу."));
    dialog.showModal();
  };
}
