(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const labels={low:'Низкий риск',medium:'Наблюдение',high:'Высокий риск',unknown:'Нет прогноза'};
  const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=n=>n==null?'—':`${n>=0?'+':''}${Math.round(n)} с`;
  const source=v=>v==='live'?'NDTP live':v==='historical_replay'?'Replay':v||'—';
  const api=async url=>{const r=await fetch(url);if(!r.ok)throw Error(`${r.status}`);return r.json()};
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
  document.addEventListener('click',event=>{
    const close=event.target.closest('[data-close-detail]');if(close){closeDrawer();return;}
    const fleet=event.target.closest('[data-fleet-id]');
    if(fleet){window.choose?.(Number(fleet.dataset.fleetId));openDrawer();return;}
    if(event.target.closest('#rows [data-id],#incident-list [data-id],#map .transit-marker'))openDrawer();
  });
  async function refresh(){try{const [state,risk]=await Promise.all([api('/api/state'),api('/api/risk')]);renderFleet(state.vehicles||[]);renderRisk(risk)}catch(error){if($('fleet-footer'))$('fleet-footer').textContent='Backend недоступен'}}
  refresh();setInterval(refresh,3000);
})();
