// src/web/static/js/app.js
/** Точка входа настоящих страниц: сессия, роль и независимые рабочие модули. */
import {initializeSession,initializeOperator} from './api.js';
import {createMapping} from './mapping.js';
import {validUuid} from './state.js';
import {refreshSummary,refreshIntegrations,refreshTimeline} from './dashboard.js';
import {createSync} from './sync-settings.js';
import {createJobs} from './jobs.js';
import {createEvents} from './events.js';
import {createOperator} from './operator.js';
import {createCatalog} from './catalog.js';
function notify(message,error=false){
  /** Безопасное сообщение без интерпретации пользовательского HTML. */
  const node=document.querySelector('[data-notice]');node.textContent=message;node.className=error?'notice notice--error':'notice';node.hidden=false;
}
try{
  await initializeSession();const session=await initializeOperator(),page=document.body.dataset.pageKind;
  if(page==='mapping'){const open=createMapping(notify),job=new URLSearchParams(location.search).get('job');if(job&&validUuid(job))await open(job);}
  const sync=createSync(session.operator,notify),jobs=createJobs(session.operator,notify);createEvents(session.operator,notify);createOperator(session,notify);
  if(page==='catalog')await createCatalog(notify);
  const refresh=async()=>{const tasks=[refreshSummary(),sync()];if(page==='home')tasks.push(refreshIntegrations(),refreshTimeline(session.operator));const results=await Promise.allSettled(tasks);for(const result of results)if(result.status==='rejected')notify(result.reason.message,true);};
  await refresh();setInterval(()=>{if(!document.hidden){refresh();if(page==='home'||page==='jobs')jobs();}},10000);
}catch(error){notify(error.message,true);}
