// src/web/static/js/operator.js
/** Короткая операторская сессия: одноразовый код не сохраняется в браузере. */
import {api,initializeOperator} from './api.js';
export function createOperator(session,notify){
  /** Блокирует обмен кода через незащищённый LAN HTTP. */
  const form=document.querySelector('[data-operator-form]');if(!form)return;
  const status=document.querySelector('[data-operator-status]'),logout=document.querySelector('[data-operator-logout]'),login=document.querySelector('[data-operator-login]');
  function render(value){session=value;form.hidden=value.operator;logout.hidden=!value.operator;login.disabled=!value.can_login;status.textContent=value.operator?`Операторская сессия активна, максимум ${value.session_seconds||900} секунд`:(value.can_login?'Введите одноразовый код, полученный по SSH':'Для входа откройте HTTPS или localhost через SSH-туннель. Код здесь не принимается.');}
  form.addEventListener('submit',async event=>{event.preventDefault();login.disabled=true;const ticket=form.elements.ticket.value;form.elements.ticket.value='';try{await api('/api/v1/operator/session',{method:'POST',body:JSON.stringify({ticket})});render(await initializeOperator());notify('Сессия оператора начата');}catch(error){notify(error.message,true);}finally{login.disabled=!session.can_login;}});
  logout.addEventListener('click',async()=>{try{await api('/api/v1/operator/session',{method:'DELETE'});render(await initializeOperator());notify('Операторская сессия завершена');}catch(error){notify(error.message,true);}});render(session);
}
