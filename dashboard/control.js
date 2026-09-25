(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const labels={low:'Низкий риск',medium:'Наблюдение',high:'Высокий риск',unknown:'Нет прогноза'};
  const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=n=>n==null?'—':`${n>=0?'+':''}${Math.round(n)} с`;
  const source=v=>v==='live'?'NDTP live':v==='historical_replay'?'Replay':v||'—';
  const api=async(url,options={})=>{const r=await fetch(url,options);const data=await r.json().catch(()=>({detail:'Некорректный ответ API'}));if(!r.ok)throw Error(data.detail||`${r.status}`);return data};
  const actions={
    contact:'Просьба подтвердить текущую обстановку и возможность следовать графику.',
    maintain:'Продолжайте движение по графику с соблюдением ПДД и требований безопасности.',
    accelerate_safely:'При возможности сократите отставание без нарушения ПДД, скоростного режима и требований безопасности.',
    slow_down_safely:'При необходимости снизьте темп движения безопасно, не создавая помех и не нарушая ПДД.'
  };
  function renderFleet(items){
    const list=$('fleet-list');if(!list)return;
    $('fleet-count').textContent=items.length;
    $('fleet-footer').textContent=items.length?`${items.filter(v=>v.source==='live').length} из live-потока · обновлено сейчас`:'Ожидание телеметрии';
    list.innerHTML=items.length?items.slice(0,7).map(v=>`<div class="fleet-item" data-fleet-id="${v.tr_id}"><span class="fleet-dot ${v.level}">${v.level==='high'?'!':'ТС'}</span><div><b>ТС ${esc(v.tr_id)}</b><small>${esc(v.stop_address||'Целевая остановка не определена')}</small></div><div class="fleet-value"><strong>${fmt(v.prediction_s)}</strong><small>${labels[v.level]||labels.unknown}</small></div></div>`).join(''):'<div class="empty-state">Нет активного транспорта</div>';
  }
  function renderRisk(data){
    const routes=data.routes||[];
    if($('risk-high'))$('risk-high').textContent=data.high_routes||0;
    if($('risk-medium'))$('risk-medium').textContent=data.medium_routes||0;
    if($('risk-total'))$('risk-total').textContent=routes.length;
  }
  function openDrawer(){ $('detail-drawer')?.classList.add('open'); }
  function closeDrawer(){ $('detail-drawer')?.classList.remove('open'); }
  function selectedVehicleId(){const text=$('detail')?.querySelector('h3')?.textContent||'';const match=text.match(/ТС\s+(\d+)/);return match?Number(match[1]):null}
  async function loadHistory(trId){const list=$('driver-command-history');if(!list)return;try{const data=await api(`/api/driver-commands?tr_id=${trId}`);list.innerHTML=data.items.length?data.items.slice(0,3).map(item=>`<li><b>${esc(item.action_title)}</b><span>${new Date(item.created_at).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'})} · в очереди</span></li>`).join(''):'<li class="command-empty">Команд по этому ТС ещё не зарегистрировано.</li>'}catch(error){list.innerHTML='<li class="command-empty">Журнал команд временно недоступен.</li>'}}
  function addDriverPanel(){const detail=$('detail'),trId=selectedVehicleId();if(!detail||!trId||detail.querySelector('.driver-panel'))return;detail.insertAdjacentHTML('beforeend',`<section class="driver-panel" data-driver-id="${trId}"><div class="driver-head"><div><span class="section-label">СВЯЗЬ С ВОДИТЕЛЕМ</span><b>ТС ${trId}</b></div><span class="command-channel">очередь интеграции</span></div><p>Команда фиксируется в локальном журнале. Внешний радио-/телематический канал в MVP не подключён.</p><div class="driver-actions" role="group" aria-label="Диспетчерская команда"><button type="button" class="active" data-driver-action="contact">Запросить связь</button><button type="button" data-driver-action="accelerate_safely">Сократить отставание</button><button type="button" data-driver-action="slow_down_safely">Снизить темп</button></div><form id="driver-command-form"><textarea id="driver-command-message" maxlength="300" aria-label="Текст сообщения">${esc(actions.contact)}</textarea><input id="driver-command-action" type="hidden" value="contact"><button class="btn btn-primary" type="submit">Зарегистрировать команду</button></form><div class="command-feedback" id="driver-command-feedback" aria-live="polite"></div><ul class="command-history" id="driver-command-history"><li class="command-empty">Загрузка журнала…</li></ul></section>`);loadHistory(trId)}
  async function queueCommand(form){const panel=form.closest('.driver-panel'),trId=Number(panel?.dataset.driverId),action=$('driver-command-action').value,message=$('driver-command-message').value.trim(),feedback=$('driver-command-feedback');if(!trId||!message)return;const submit=form.querySelector('button[type="submit"]');submit.disabled=true;feedback.textContent='Регистрация команды…';feedback.className='command-feedback';try{const result=await api('/api/driver-commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:'dispatcher',tr_id:trId,action,message})});feedback.className='command-feedback ok';feedback.textContent=`Команда зарегистрирована: ${result.action_title}. Статус: очередь интеграции.`;await loadHistory(trId)}catch(error){feedback.className='command-feedback error';feedback.textContent=`Команда не зарегистрирована: ${error.message}`}finally{submit.disabled=false}}
  document.addEventListener('click',event=>{
    const close=event.target.closest('[data-close-detail]');if(close){closeDrawer();return;}
    const fleet=event.target.closest('[data-fleet-id]');
    if(fleet){window.choose?.(Number(fleet.dataset.fleetId));openDrawer();return;}
    const action=event.target.closest('[data-driver-action]');
    if(action){const panel=action.closest('.driver-panel');panel.querySelectorAll('[data-driver-action]').forEach(button=>button.classList.toggle('active',button===action));panel.querySelector('#driver-command-action').value=action.dataset.driverAction;panel.querySelector('#driver-command-message').value=actions[action.dataset.driverAction];return;}
    if(event.target.closest('#rows [data-id],#incident-list [data-id],#map .transit-marker'))openDrawer();
  });
  document.addEventListener('submit',event=>{if(event.target.id==='driver-command-form'){event.preventDefault();queueCommand(event.target);}});
  new MutationObserver(addDriverPanel).observe($('detail'),{childList:true,subtree:true});
  async function refresh(){try{const [state,risk]=await Promise.all([api('/api/state'),api('/api/risk')]);renderFleet(state.vehicles||[]);renderRisk(risk)}catch(error){if($('fleet-footer'))$('fleet-footer').textContent='Backend недоступен'}}
  refresh();setInterval(refresh,3000);
})();
