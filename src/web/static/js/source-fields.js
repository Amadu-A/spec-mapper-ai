// src/web/static/js/source-fields.js
/** Отображение новых полей без изменения сохранённого исходного JSON. */
export function sourceAttributes(source) {
  /** Возвращает непустые исходные реквизиты для таблицы и окна перевыбора. */
  return [['Brand','Марка'],['Article','Артикул'],['Note','Примечание']]
    .filter(([key])=>source[key]!==undefined&&source[key]!=='')
    .map(([key,label])=>`${label}: ${source[key]}`);
}
