// src/web/static/js/dom.js
/** Безопасные DOM-компоненты: все исходные и каталожные строки вставляются через textContent. */
export function element(tag, className, text) {
  /** Создаёт узел с текстом, не интерпретируя HTML из спецификаций и XML. */
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
export function option(item, candidate, selected, onChange) {
  /** Создаёт доступный radio с реальными кодом, единицей, score и причинами конфликтов. */
  const label = element("label", "candidate");
  const radio = element("input"); radio.type = "radio"; radio.name = "catalog-choice";
  radio.value = item.guid; radio.checked = selected === item.guid;
  radio.addEventListener("change", () => onChange(item.guid));
  const text = element("span", "", `${item.code} · ${item.name}`);
  text.append(element("small", "", `Ед.: ${item.unit || "не указана"} · ${item.onec_guid ? `GUID 1С: ${item.onec_guid}` : `Внутренний UUID: ${item.guid}; GUID 1С отсутствует`}`));
  if (candidate) {
    text.append(element("small", "", `Cosine: ${candidate.score.toFixed(3)} · Ранг правил: ${candidate.rank.toFixed(3)}`));
    for (const reason of candidate.conflicts) text.append(element("small", "candidate__conflict", reason));
    for (const evidence of candidate.evidence) text.append(element("small", "", evidence));
  }
  label.append(radio, text); return label;
}
