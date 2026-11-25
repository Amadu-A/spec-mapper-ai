// src/web/static/js/invoice-table.js
/** Read-only финансовые колонки и общие действия выбора; никакого пересчёта денег. */
import {element} from "./dom.js";
import {invoiceLabels,invoiceValue as value,warningLabels} from "./invoice-state.js";

export function invoiceRow(row, {choose,clear,preview,locked,previewAvailable}) {
  /** Безопасно отображает один source вместе с сохранёнными кандидатами и объяснениями. */
  const tr=element("tr"),source=row.source,doc=row.document;
  tr.dataset.invoiceRow=String(row.index);
  const file=element("td","",`${row.file.source_file}\n${value(doc.document_type)} · № ${value(doc.number)} · ${value(doc.date)}`);
  file.append(element("div","match-table__name",value(doc.supplier?.name)));
  const previewButton=element("button","button","Исходный документ");
  previewButton.type="button";previewButton.disabled=!previewAvailable;
  previewButton.title=previewAvailable?"Открыть физическую страницу":"Исходные файлы недоступны для импорта готового JSON";
  previewButton.addEventListener("click",()=>preview(row));file.append(previewButton);tr.append(file);
  const title=element("td","match-table__name",`${source.row_number}. ${source.caption}`);
  title.append(element("small","match-table__meta",`Вид: ${value(source.row_kind || "product")} · стр. ${(source.source_pages || []).join(", ")}`));
  if(source.row_kind==="advance_payment"||source.row_kind==="final_payment")title.append(element("span","status status--review","Не номенклатура"));
  tr.append(title,element("td","",`Артикул: ${value(source.article)}\nКандидат: ${value(source.article_candidate)}\nКод поставщика: ${value(source.supplier_code)}\nБренд: ${value(source.brand)}\nПроизводитель: ${value(source.manufacturer)}`));
  tr.append(element("td","",`${value(source.units)} · ${value(source.quantity)}`),
    element("td","",`${value(source.unit_price_without_vat)} / ${value(source.unit_price_with_vat)}`),
    element("td","",`${value(source.line_total_without_vat)} / ${value(source.line_total_with_vat)}`),
    element("td","",`${value(source.vat_rate)}% · ${value(source.vat_amount)}\n${value(doc.currency)}\nПроверка: ${value(doc.validation?.status)}`));
  const target=element("td"),head=element("div","match-table__target");
  head.append(element("div","match-table__name",row.selection?.name || "Соответствие не выбрано"));
  if(row.selection){
    const remove=element("button","match-table__clear","×");remove.type="button";
    remove.setAttribute("aria-label",`Очистить соответствие для строки ${row.index+1}`);remove.disabled=locked;
    remove.dataset.invoiceMutable="true";
    remove.addEventListener("click",()=>clear(row));head.append(remove);
  }
  target.append(head);
  if(row.selection)target.append(element("div","match-table__code",row.selection.code),element("small","match-table__meta",row.selection.onec_guid || "GUID 1С отсутствует"));
  tr.append(target);
  const state=element("td"),candidate=row.candidates.find(item=>item.item.guid===row.selection?.guid);
  state.append(element("div","match-table__score",candidate?candidate.score.toFixed(3):"—"),element("span",`status status--${row.status}`,invoiceLabels[row.status] || row.status));
  if(row.confirmed)state.append(element("small","match-table__meta","Подтверждено человеком"));
  for(const code of row.warnings || [])state.append(element("small","invoice-table__warning",warningLabels[code] || code));
  for(const conflict of candidate?.conflicts || [])state.append(element("small","invoice-table__warning",conflict));
  tr.append(state);
  const action=element("td","invoice-table__actions"),button=element("button","button","Top-5 · Выбрать");
  button.type="button";button.setAttribute("aria-label",`Выбрать соответствие для строки ${row.index+1}`);
  button.disabled=locked||!row.selectable;button.addEventListener("click",()=>choose(row));action.append(button);tr.append(action);
  button.dataset.invoiceMutable="true";button.dataset.invoiceSelectable=String(row.selectable);
  return tr;
}
