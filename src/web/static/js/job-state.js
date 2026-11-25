// src/web/static/js/job-state.js
/** Чистые правила отображения lifecycle; не вычисляют business matching или финансовые итоги. */
export const jobLabels = {idle:"Новое задание",submitting:"Отправка файлов…",queued:"В очереди…",extracting:"Извлечение…",matching:"Сопоставление…",cancelling:"Отмена запрошена",awaiting_review:"Готово к проверке",approved:"Утверждено",cancelled:"Отменено",failed:"Ошибка",expired:"Срок хранения истёк"};
export const processingStates = new Set(["queued","extracting","matching","cancelling"]);
export class SubmissionState {
  /** Один logical запуск удерживает прежний ключ, включая неопределённый результат POST. */
  constructor(createKey) { this.createKey=createKey;this.reset(); }
  reset() { /** Только явное новое задание освобождает завершённую форму. */ this.state="idle";this.signature=null;this.key=this.createKey(); }
  changed() { /** Изменённый payload до отправки получает новую техническую идентичность. */ if(this.state==="idle"){this.key=this.createKey();this.signature=null;} }
  begin(signature) {
    /** Блокировка выполняется синхронно до первого await feature handler. */
    if(!["idle","uncertain"].includes(this.state))return null;
    if(this.state==="uncertain"&&this.signature!==signature)throw new Error("Исход отправки неизвестен. Восстановите задание или явно начните новое.");
    if(this.signature!==null&&this.signature!==signature)this.key=this.createKey();
    this.signature=signature;this.state="submitting";return this.key;
  }
  accepted() { /** 202 не сбрасывает logical lock и не меняет ключ. */ this.state="accepted"; }
  failed(status) { /** Сетевая/серверная ошибка не доказывает, что POST не принят. */ this.state=!status||status>=500?"uncertain":"idle"; }
}
export function knownState(state) {
  /** Не выдаёт неизвестный backend status за успех или завершённый этап. */
  return Object.hasOwn(jobLabels,state);
}
export function jobStage(value) {
  /** Различает собственную очередь, передачу и очередь 8083 по доказанным metadata. */
  if(value.stage==="transferring")return "Передача файлов";
  if(value.stage==="upstream_queued")return "Очередь извлечения";
  if(value.stage==="cancel_unknown")return "Отмена: исход отправки уточняется";
  if(value.stage==="cancel_retry")return "Отмена: сервис временно недоступен";
  return jobLabels[value.status]||`Неизвестное состояние: ${value.status}`;
}
export function progressView(value) {
  /** Проценты только по фактическим страницам/строкам; 0/0 и придуманного ETA нет. */
  if(value.status==="matching"&&value.total_items!==null){
    const total=value.total_items,done=value.processed_items;
    return {text:total?`Сопоставлено ${done} из ${total} позиций`:"В документе нет позиций",percent:total?Math.min(100,100*done/total):null};
  }
  if(!value.extracting_applicable)return {text:value.total_items===null?"Импорт готового JSON":"Готовый JSON · извлечение не требуется",percent:null};
  if(value.total_pages)return {text:`Обработано ${value.processed_pages} из ${value.total_pages} страниц`,percent:Math.min(100,100*value.processed_pages/value.total_pages)};
  return {text:value.status==="queued"?"Ожидание начала обработки":"Определяется количество страниц и позиций",percent:null};
}
export function fileProgress(file) {
  /** Учитывает только committed completed pages и выбранные физические номера. */
  const selected=new Set(file.selected_pages||[]);
  const done=(file.pages||[]).filter(page=>selected.has(page.pdf_page)&&page.state==="completed").length;
  const running=(file.pages||[]).some(page=>page.state==="running");
  const failed=(file.pages||[]).some(page=>page.state==="failed"),cancelled=(file.pages||[]).some(page=>page.state==="cancelled");
  return {done,total:selected.size,state:failed?"failed":cancelled?"cancelled":done&&done===selected.size?"completed":running?"running":"queued"};
}
