// src/web/static/js/operator.js
/** Кнопка выдачи кода через SSH gateway и короткая роль без хранения секретов в браузере. */
import {api,initializeOperator} from './api.js';
export function createOperator(session,notify){
  /** Разделяет доказанный SSH-вход и обычный web/HTTPS вход с уже полученным кодом. */
  const form=document.querySelector('[data-operator-form]');if(!form)return;
  const find=selector=>document.querySelector(selector),status=find('[data-operator-status]'),logout=find('[data-operator-logout]'),login=find('[data-operator-login]');
  const issue=find('[data-get-ticket]'),issued=find('[data-issued-ticket]'),issuedPanel=find('[data-issued-ticket-panel]'),instructions=find('[data-ssh-instructions]'),username=find('[data-ssh-user]');
  let expiry=0;
  function clearTicket(){
    /** Убирает выданный код после входа, истечения срока или ухода со страницы. */
    issued.value='';form.elements.ticket.value='';issuedPanel.hidden=true;expiry=0;
  }
  function command(){
    /** Подставляет только логин в подсказку, не исполняя произвольные shell-команды. */
    const value=username.value.trim(),user=/^[a-zA-Z0-9._-]+$/.test(value)?value:'ВАШ_ЛОГИН';
    const host=session.ssh_host||'АДРЕС_СЕРВЕРА';
    find('[data-ssh-command]').textContent=`ssh -N -L 18087:127.0.0.1:${session.operator_port||8088} ${user}@${host}`;
  }
  function render(value){
    /** Кнопка выдачи появляется только по серверному признаку, а не по location.hostname. */
    session=value;form.hidden=value.operator;logout.hidden=!value.operator;login.disabled=!value.can_login;
    find('[data-ticket-issue]').hidden=value.operator||!value.can_issue_ticket;issue.disabled=!value.can_issue_ticket;
    instructions.hidden=!!value.can_issue_ticket||value.operator;
    status.textContent=value.operator?`Операторская сессия активна, максимум ${value.session_seconds||900} секунд`:(value.can_issue_ticket?'SSH-вход подтверждён. Получите код кнопкой и введите его ниже.':value.can_login?'Введите готовый код либо откройте SSH-вход для получения кода кнопкой.':'Откройте SSH-вход через localhost, чтобы получить одноразовый код.');
    if(value.operator)clearTicket();command();
  }
  issue.addEventListener('click',async()=>{
    issue.disabled=true;clearTicket();
    try{const result=await api('/api/v1/operator/tickets',{method:'POST'});issued.value=result.ticket;issuedPanel.hidden=false;expiry=performance.now()+result.expires_in*1000;issued.focus();issued.select();find('[data-ticket-expiration]').textContent=`Код действует ${result.expires_in} секунд и используется один раз.`;}
    catch(error){notify(error.message,true);if(error.status===403)render(await initializeOperator());}
    finally{issue.disabled=!session.can_issue_ticket;}
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();login.disabled=true;const ticket=form.elements.ticket.value;form.elements.ticket.value='';
    try{await api('/api/v1/operator/session',{method:'POST',body:JSON.stringify({ticket})});render(await initializeOperator());notify('Сессия оператора начата');}
    catch(error){notify(error.message,true);}finally{login.disabled=!session.can_login;}
  });
  logout.addEventListener('click',async()=>{try{await api('/api/v1/operator/session',{method:'DELETE'});render(await initializeOperator());notify('Операторская сессия завершена');}catch(error){notify(error.message,true);}});
  username.addEventListener('input',command);window.addEventListener('pagehide',clearTicket);
  setInterval(()=>{if(expiry&&performance.now()>=expiry){clearTicket();find('[data-ticket-expiration]').textContent='Срок кода истёк. Получите новый код.';}},1000);render(session);
}
