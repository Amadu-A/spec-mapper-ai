// src/web/static/js/dashboard-state.js
/** Чистое форматирование фактических состояний dashboard; используется также Node-тестами. */
export const labels = {queued:"В очереди",polling:"Ожидание извлечения",fetching:"Получение файла",downloading:"Загрузка XML",reading_xml:"Чтение XML",validating:"Проверка XML",validated:"XML проверен",staging:"Подготовка версии",indexing:"Индексация",publishing:"Публикация",matching:"Сопоставление",ready_for_review:"Требует проверки",complete:"Завершено",approved:"Утверждено",failed:"Ошибка",awaiting_contract:"Ожидается контракт",check_required:"Нужна проверка",confirmed:"Проверено",not_configured:"Не настроено",key_missing:"Нет ключа",unauthorized:"Ключ отклонён",unavailable:"Недоступно",healthy:"Доступно",degraded:"Есть ошибки",active:"Есть обращения",past_activity:"Были обращения",no_activity:"Нет обращений"};
export const number = value => value === null || value === undefined ? "—" : Number(value).toLocaleString("ru-RU");
Object.assign(labels,{available:"Доступен, ключ проверен",error:"Ошибка проверки",preparing_index:"Подготовка индекса",retry:"Повторная попытка",coalesced:"Объединён с текущим импортом"});
export const percent = value => value === null || value === undefined ? "Нет данных" : `${Number(value).toLocaleString("ru-RU",{maximumFractionDigits:1})} %`;
export const date = value => value ? new Date(value * 1000).toLocaleString("ru-RU",{timeZone:"UTC",dateStyle:"short",timeStyle:"short"}) + " UTC" : "—";
export function tone(state) {
  /** Непроверенные источники никогда не получают зелёный статус. */
  return ["healthy","available","confirmed","active","complete","approved"].includes(state) ? "good" : ["failed","error","unavailable","unauthorized"].includes(state) ? "bad" : "pending";
}
export function changeText(details) {
  /** Нулевые изменения отличаются от отсутствующего или частичного отчёта. */
  const values = details?.run_statistics || details || {};
  if (values.added_count === undefined) return "Нет отчёта об изменениях";
  return `${values.counts_complete === false ? "Частичный отчёт · " : ""}Добавлено: ${number(values.added_count)} · изменено: ${number(values.changed_count)} · удалено: ${number(values.removed_count)} · без изменений: ${number(values.unchanged_count)}`;
}
