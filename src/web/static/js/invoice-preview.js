// src/web/static/js/invoice-preview.js
/** Просмотр исходных страниц через защищённый 8087 proxy, без upstream URL/ключей. */
export function createInvoicePreview() {
  /** Возвращает открытие preview с физическими координатами, уже проверенными backend. */
  const dialog = document.querySelector("[data-invoice-preview]");
  const image = dialog.querySelector("[data-invoice-preview-image]");
  const status = dialog.querySelector("[data-invoice-preview-status]");
  const previous = dialog.querySelector("[data-invoice-preview-previous]");
  const next = dialog.querySelector("[data-invoice-preview-next]");
  let active;
  function render() {
    /** Загружает только известный same-origin маршрут; страницы не рендерятся в браузере заново. */
    status.textContent = `Страница ${active.page} из ${active.count} · загрузка…`;
    image.hidden = true;
    previous.disabled = active.page <= 1;
    next.disabled = active.page >= active.count;
    image.src = `/api/v1/invoices/${active.jobId}/files/${active.fileIndex}/pages/${active.page}/preview`;
  }
  previous.addEventListener("click", () => { if(active.page > 1){active.page--;render();} });
  next.addEventListener("click", () => { if(active.page < active.count){active.page++;render();} });
  image.addEventListener("load", () => {if(!active||!dialog.open)return;image.hidden=false;status.textContent=`Страница ${active.page} из ${active.count}`;});
  image.addEventListener("error", () => {if(!active||!dialog.open)return;status.textContent="Страница недоступна. Проверьте срок хранения источника и повторите просмотр.";});
  dialog.querySelector("[data-invoice-preview-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {active=undefined;image.removeAttribute("src");status.textContent="";document.querySelector("#invoice-preview-title").textContent="Исходный документ";});
  return function open(jobId, row) {
    /** Сохраняет имя как literal text и открывает native dialog с возвратом фокуса. */
    active={jobId,fileIndex:row.reference.file_index,page:row.reference.source_pages[0] || 1,count:row.file.page_count};
    document.querySelector("#invoice-preview-title").textContent=row.file.source_file;
    render();dialog.showModal();
  };
}
