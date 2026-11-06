// src/web/static/js/app.js
/** Точка входа UI: сессия, навигация, реальные зависимости, история и поиск справочника. */
import { api, initializeSession } from "./api.js";
import { element } from "./dom.js";
import { createMapping } from "./mapping.js";
import { debounce, stages, validUuid } from "./state.js";

function notify(message, error = false) {
  /** Объявляет безопасный результат действия через aria-live без alert и подстановки HTML. */
  const node = document.querySelector("[data-notice]"); node.textContent = message;
  node.className = error ? "notice notice--error" : "notice"; node.hidden = false;
}
const open = createMapping(notify);

async function historyJobs() {
  /** Читает только задания текущего backend owner и восстанавливает их при выборе. */
  try {
    const jobs = await api("/api/v1/mappings"), list = document.querySelector("[data-jobs]");
    list.replaceChildren(...jobs.filter(job => job.kind === "mapping").map(job => {
      const item = element("article", "workspace__item"), text = element("p", "", job.job_id);
      text.append(element("small", "", `${stages[job.status] || job.stage} · ${job.total} позиций · до ${new Date(job.expires_at * 1000).toLocaleTimeString("ru")}`));
      const button = element("button", "button", "Открыть"); button.type = "button";
      button.addEventListener("click", () => { navigate("mapping"); open(job.job_id); }); item.append(text, button); return item;
    }));
    if (!list.children.length) list.append(element("p", "workspace__empty", "В этой сессии ещё нет заданий"));
  } catch (error) { notify(error.message, true); }
}
function navigate(view) {
  /** Переключает только существующие рабочие разделы с корректным aria-current. */
  document.querySelectorAll("[data-view]").forEach(node => { node.hidden = node.dataset.view !== view; });
  document.querySelectorAll("[data-nav]").forEach(node => {
    node.classList.toggle("sidebar__link--active", node.dataset.nav === view);
    if (node.dataset.nav === view) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
  document.querySelector("[data-breadcrumb]").textContent = { mapping: "Сопоставление", jobs: "Задания", catalog: "Справочник 1С" }[view];
  if (view === "jobs") historyJobs();
}
async function catalogStatus() {
  /** Показывает actual count, fingerprint и принятый карантин, без демонстрационных дат. */
  try {
    const data = await api("/api/v1/catalog/status");
    document.querySelector("[data-catalog-date]").textContent = data.last_successful_sync ? `Каталог: ${new Date(data.last_successful_sync * 1000).toLocaleString("ru")}` : "Каталог не импортирован";
    document.querySelector("[data-catalog-info]").textContent = data.ready ? `${data.count} активных позиций · версия ${data.version} · fingerprint ${data.fingerprint} · повторно использовано векторов: ${data.reused_count}${data.last_error ? ` · Ошибка последней синхронизации: ${data.last_error}` : ""}` : `Каталог не готов${data.last_error ? `: ${data.last_error}` : ". Запустите импорт XML на сервере."}`;
    const warnings = document.querySelector("[data-catalog-warnings]");
    warnings.replaceChildren(...(data.warnings || []).map(text => element("li", "", text))); warnings.hidden = !warnings.children.length;
  } catch (error) { notify(error.message, true); }
}
async function dependencies() {
  /** Индикаторы OK появляются только после успешной проверки реальных подключений. */
  try {
    const response = await fetch("/health/ready", { credentials: "same-origin" }); const data = await response.json();
    document.querySelectorAll("[data-dependency]").forEach(node => { node.textContent = data.dependencies?.[node.dataset.dependency] ? "OK" : "Недоступен"; });
  } catch { document.querySelectorAll("[data-dependency]").forEach(node => { node.textContent = "Нет связи"; }); }
}
let searchGeneration = 0;
document.querySelector("[data-catalog-query]").addEventListener("input", debounce(async event => {
  const request = ++searchGeneration, node = document.querySelector("[data-catalog-items]"), query = event.target.value.trim();
  node.replaceChildren(); if (!query) return;
  try {
    const data = await api(`/api/v1/catalog/items?q=${encodeURIComponent(query)}`);
    if (request !== searchGeneration) return;
    node.replaceChildren(...data.items.map(item => { const row = element("article", "workspace__item"); const text = element("p", "", `${item.code} · ${item.name}`); text.append(element("small", "", `Ед.: ${item.unit} · артикул: ${item.article || "—"} · ${item.onec_guid ? `GUID 1С: ${item.onec_guid}` : `Внутренний UUID: ${item.guid}`}`)); row.append(text); return row; }));
    if (!data.items.length) node.append(element("p", "", "Ничего не найдено"));
  } catch (error) { if (request === searchGeneration) notify(error.message, true); }
}));
document.querySelectorAll("[data-nav]").forEach(node => node.addEventListener("click", () => navigate(node.dataset.nav)));
document.querySelector("[data-refresh-jobs]").addEventListener("click", historyJobs);
try {
  await initializeSession(); await catalogStatus(); dependencies();
  const job = new URLSearchParams(location.search).get("job"); if (job && validUuid(job)) await open(job);
  setInterval(() => { catalogStatus(); dependencies(); }, 60000);
} catch (error) { notify(error.message, true); }
