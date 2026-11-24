// src/web/static/js/invoice-state.js
/** Отображение invoice API; исходные финансы и решения backend не пересчитываются. */
export const invoiceStages = {queued:"В очереди",extracting:"Извлечение счетов",matching:"Сопоставление",awaiting_review:"Готово к проверке",approved:"Результат утверждён",failed:"Ошибка обработки"};
export const invoiceLabels = {matched:"Подобрано",review:"Проверить",not_found:"Не найдено",manual:"Вручную",not_applicable:"Не номенклатура"};
export const warningLabels = {
  financial_failed:"Финансовая проверка выявила расхождение",financial_partially_checked:"Финансы проверены частично",
  financial_not_checkable:"Недостаточно данных для проверки",financial_check_failed:"Есть неуспешная арифметическая проверка",
  source_financial_warnings:"Сохранены предупреждения источника",financial_fields_missing:"Не все суммы/ставки присутствуют в источнике",
  currency_missing:"Валюта не указана",supplier_missing:"Не указан поставщик",different_currencies:"Документы в разных валютах",
  different_suppliers:"Документы разных поставщиков",different_vat_rates:"Разные ставки НДС",
  payment_row:"Платёжная строка требует отдельного решения в 1С",unsupported_row_kind:"Неизвестный вид строки: нужно решение в 1С",
  service_requires_review:"Услуга требует проверки",onec_guid_missing:"Отсутствует реальный GUID 1С",source_requires_review:"Источник требует ручной проверки",
};

export function invoiceCanApprove(status, warnings, acknowledged, busy = false) {
  /** Разрешает действие по готовности и отметкам серверных предупреждений, без их переоценки. */
  return !busy && status === "awaiting_review" && warnings.every(warning => acknowledged.has(warning.document_id));
}

export function invoiceSelectionSource(row) {
  /** Представляет реквизиты в общем dialog; не является входом matching engine. */
  const source = row.source;
  return { Caption: source.caption, Units: source.units ?? "", Quantity: source.quantity ?? "",
    Brand: source.brand ?? source.manufacturer ?? "", Article: source.article ?? "", Note: source.availability_note ?? "",
    Pages: (source.source_pages || []).join(", "), UserComment1: `${row.file.source_file} · ${row.document.number ?? ""}` };
}

export function invoiceValue(value) {
  /** null показывается явно; исходные строки, пробелы и ведущие нули сохраняются. */
  return value === null || value === undefined ? "—" : String(value);
}

export function invoiceApprovalPayload(revision, acknowledged) {
  /** Передаёт только revision и явные отметки пользователя, без финансовых patches. */
  return {revision, acknowledged_document_warnings:[...acknowledged].sort()};
}
