// src/web/static/js/catalog.js
/** Поиск по действующему справочнику с отменой устаревшего ответа. */
import {api} from './api.js';
import {element} from './dom.js';
import {debounce} from './state.js';
export async function createCatalog(notify){
  /** Выводит проверенные метаданные и карантин, не обращаясь к внешним URL. */
  const input=document.querySelector('[data-catalog-query]');if(!input)return;
  const status=await api('/api/v1/catalog/status');document.querySelector('[data-catalog-info]').textContent=`${status.ready?'Каталог готов':'Каталог не готов'} · ${status.count||0} активных · версия ${status.version||'—'}${status.last_error?` · ошибка последнего обновления: ${status.last_error}`:''}`;
  const warnings=document.querySelector('[data-catalog-warnings]');warnings.replaceChildren(...(status.warnings||[]).map(v=>element('li','',v)));warnings.hidden=!warnings.children.length;
  let generation=0;input.addEventListener('input',debounce(async()=>{const current=++generation,node=document.querySelector('[data-catalog-items]'),query=input.value.trim();node.replaceChildren();if(!query)return;
    try{const data=await api(`/api/v1/catalog/items?q=${encodeURIComponent(query)}`);if(current!==generation)return;
      node.replaceChildren(...data.items.map(item=>{const row=element('article','workspace__item');row.append(element('p','',`${item.code} · ${item.name}`),element('small','',`Ед.: ${item.unit} · артикул: ${item.article||'—'} · ${item.onec_guid?`GUID 1С: ${item.onec_guid}`:`Внутренний UUID: ${item.guid}; GUID 1С отсутствует`}`));return row;}));if(!data.items.length)node.append(element('p','','Ничего не найдено'));
    }catch(error){if(current===generation)notify(error.message,true);}}));input.disabled=false;
}
