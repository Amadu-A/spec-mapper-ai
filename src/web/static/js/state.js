// src/web/static/js/state.js
/** Небольшие чистые правила UI; бизнес-подбор и сохранение выполняет backend. */
export const labels = { matched: "Подобрано", review: "Проверить", not_found: "Не найдено", manual: "Вручную" };
export const stages = { queued: "В очереди", polling: "Ожидание извлечения", fetching: "Получение JSON", matching: "Сопоставление", ready_for_review: "Подбор завершён · требуется проверка", complete: "Обработка завершена", approved: "Результат утверждён", failed: "Ошибка обработки", retry: "Повторная попытка" };

export function terminal(status) {
  /** Завершает технический polling, даже если кандидаты требуют ручной проверки. */
  return ["complete", "ready_for_review", "approved", "failed"].includes(status);
}
export function validUuid(value) {
  /** Проверяет форму UUID до создания запроса; окончательная валидация остаётся на сервере. */
  return /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i.test(value);
}
export function newIdempotencyKey(cryptoSource = globalThis.crypto) {
  /** Создаёт UUID и на обычном HTTP в LAN, где crypto.randomUUID может отсутствовать. */
  const bytes = cryptoSource.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function selectionPayload(guid, revision) {
  /** Различает явный отказ null и отсутствие выбора undefined, сохраняя revision. */
  if (guid === undefined) throw new Error("Сначала выберите позицию или отсутствие соответствия");
  return { catalog_item_guid: guid, revision };
}
export function pageInfo(offset, count, total, limit) {
  /** Вычисляет доступность страниц по отфильтрованному total без изменения общих totals. */
  return { previous: offset > 0, next: offset + count < total, page: Math.floor(offset / limit) + 1,
    text: total ? `Показано ${offset + 1}–${offset + count} из ${total}` : "Нет позиций по выбранному фильтру" };
}
export function debounce(callback, delay = 300) {
  /** Уменьшает число запросов при вводе текста без изменения серверной логики поиска. */
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => callback(...args), delay); };
}
