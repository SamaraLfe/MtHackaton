'use strict';
const $=id=>document.getElementById(id), ns='http://www.w3.org/2000/svg';
let current=[],paths=[],selected=null,playing=false,busy=false;
const colors={low:'#3d8b6b',medium:'#d3a245',high:'#c65851',unknown:'#94a5a6'};
const labels={low:'Низкий риск',medium:'Наблюдение',high:'Высокий риск',unknown:'Нет свежего прогноза'};
const fmt=n=>n==null?'—':`${n>=0?'+':''}${Math.round(n)} с`;
const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,body){const r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,160)}`);return r.json()}
function el(name,attrs,parent=$('map')){const e=document.createElementNS(ns,name);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);parent.appendChild(e);return e}
function draw(){
 const svg=$('map');svg.replaceChildren();
 const all=paths.flatMap(p=>p.points).concat(current.filter(v=>v.lon!=null&&v.lat!=null).map(v=>[v.lon,v.lat])).filter(p=>Number.isFinite(p[0])&&Number.isFinite(p[1]));
 if(!all.length)return;
 const xs=all.map(p=>p[0]),ys=all.map(p=>p[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
 const x=v=>40+(v-minX)/Math.max(.01,maxX-minX)*770,y=v=>470-(v-minY)/Math.max(.01,maxY-minY)*430;
 for(let i=0;i<6;i++){el('line',{x1:40+i*154,y1:25,x2:40+i*154,y2:475,stroke:'#dce5df','stroke-dasharray':'3 6'});el('line',{x1:40,y1:40+i*86,x2:810,y2:40+i*86,stroke:'#dce5df','stroke-dasharray':'3 6'});const text=el('text',{x:40+i*154,y:505,fill:'#8ca297','font-size':10});text.textContent=(minX+i/5*(maxX-minX)).toFixed(2)+'°E'}
 for(const path of paths){const v=current.find(v=>v.tr_id===path.tr_id);const color=v?colors[v.level]:'#c9d7cf';el('polyline',{points:path.points.map(p=>`${x(p[0])},${y(p[1])}`).join(' '),fill:'none',stroke:color,'stroke-width':v?2:1,opacity:v?.6:.25})}
 for(const v of current){if(v.lon==null||v.lat==null)continue;const g=el('g',{class:'vehicle',tabindex:0,role:'button','aria-label':`ТС ${v.tr_id}, ${labels[v.level]}`});el('circle',{cx:x(v.lon),cy:y(v.lat),r:selected===v.tr_id?13:10,fill:colors[v.level],stroke:'#fff','stroke-width':3},g);const title=el('title',{},g);title.textContent=`ТС ${v.tr_id} · ${labels[v.level]}`;g.addEventListener('click',()=>choose(v.tr_id));g.addEventListener('keydown',e=>{if(e.key==='Enter')choose(v.tr_id)})}
}
function choose(id){selected=id;draw();detail()}
function detail(){const v=current.find(v=>v.tr_id===selected);if(!v)return;
 $('detail').innerHTML=`<h3>ТС ${esc(v.tr_id)}</h3><span class="status-pill ${esc(v.level)}">${labels[v.level]}</span><div class="forecast-value">${fmt(v.prediction_s)}</div><p>Прогноз отклонения от расписания</p><dl><dt>90% номинальный прогнозный интервал</dt><dd>${fmt(v.lower_s)} … ${fmt(v.upper_s)}</dd><dt>Целевая остановка</dt><dd>${esc(v.stop_address)}</dd><dt>Плановое прибытие</dt><dd>${v.target_time_begin?new Date(v.target_time_begin).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow'}):'—'}</dd><dt>Наблюдаемый паттерн · гипотеза</dt><dd>${esc(v.reason)}</dd><dt>Источник и модель</dt><dd>${esc(v.source)} / ${esc(v.model)}</dd></dl><div class="recommend">${esc(v.recommendation||'Проверить расписание и связь с ТС')}</div>`;
}
function render(data){current=data.vehicles;const priority={high:0,medium:1,low:2,unknown:3};current.sort((a,b)=>priority[a.level]-priority[b.level]);$('total').textContent=current.length;$('high').textContent=current.filter(v=>v.level==='high').length;$('latency').textContent=data.counters.last_inference_ms==null?'—':`${Math.round(data.counters.last_inference_ms)} мс`;$('clock').textContent=data.state.clock?new Date(data.state.clock).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow'}):'—';$('progress').textContent=data.state.mode==='replay'?`${data.state.index} / ${data.total_points} точек`:'';
 const filtered=$('risk-only').checked?current.filter(v=>v.level==='high'):current;
 $('rows').innerHTML=filtered.length?filtered.map(v=>`<tr data-id="${v.tr_id}"><td><b>ТС ${v.tr_id}</b></td><td><span class="status-pill ${v.level}">${labels[v.level]}</span></td><td>${esc(v.stop_address)}</td><td><b>${fmt(v.prediction_s)}</b></td><td>${v.late_probability==null?'—':Math.round(v.late_probability*100)+'%'}</td><td>${v.horizon_s?Math.round(v.horizon_s/60)+' мин':'—'}</td></tr>`).join(''):'<tr><td colspan="6" class="empty-row">Нет прогнозов для выбранного режима.</td></tr>';
 for(const row of $('rows').querySelectorAll('[data-id]'))row.onclick=()=>choose(Number(row.dataset.id));
 if(!current.some(v=>v.tr_id===selected))selected=current[0]?.tr_id??null;draw();detail();
}
async function refresh(){try{const data=await api('/api/state');render(data);if($('status').textContent==='Связь с API потеряна'){$('status').textContent='Связь восстановлена';$('notice').textContent=data.state.mode==='replay'?'Данные 6 января 2026. Исторический прогон; не текущая дорожная ситуация.':'Живой поток NDTP · TCP 9201'}}catch(e){$('status').textContent='Связь с API потеряна';$('notice').textContent=e.message}}
async function step(){if(busy)return;busy=true;try{const r=await api('/api/replay/step',{});if(r.done){playing=false;$('play').textContent='▶ Запустить прогон';$('status').textContent='Прогон завершён'}else $('status').textContent='Исторический поток активен';await refresh()}catch(e){playing=false;$('play').textContent='▶ Запустить прогон';$('notice').textContent=e.message}finally{busy=false}}
$('step').onclick=step;$('play').onclick=()=>{playing=!playing;$('play').textContent=playing?'Ⅱ Пауза':'▶ Запустить прогон';if(playing)step()};
$('mode').onchange=async()=>{playing=false;$('play').textContent='▶ Запустить прогон';try{const mode=$('mode').value;await api('/api/mode',{mode});const live=mode==='live';$('step').disabled=live;$('play').disabled=live;$('mode-label').textContent=live?'ЖИВОЙ ПОТОК NDTP':'ИСТОРИЧЕСКИЙ ПРОГОН';$('notice').textContent=live?'TCP 9201. Для текущих дат необходимо загрузить соответствующее плановое расписание.':'Данные 6 января 2026. Исторический прогон; не текущая дорожная ситуация.';$('status').textContent='Ожидание данных';$('detail').innerHTML='<h3>Ожидание данных</h3>';await refresh()}catch(e){$('notice').textContent=e.message}};
$('risk-only').onchange=refresh;
(async()=>{try{paths=(await api('/api/network')).paths;const m=await api('/api/metrics');$('mae').textContent=m.test_mae_s.toFixed(1)+' с';$('baseline').textContent=`Текущая задержка: ${m.persistence_mae_s.toFixed(1)} с`;await refresh()}catch(e){$('notice').textContent=e.message}})();
setInterval(()=>{if(playing)step();else refresh()},4000);
