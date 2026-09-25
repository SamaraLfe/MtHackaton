/* Map integration only. Replay, live monitoring, inspector and filters are owned
 * by app.js and retain the behavior of the existing operations monitor. */
(() => {
  'use strict';
  const colors={low:'#198038',medium:'#b28600',high:'#da1e28',unknown:'#6f6f6f'};
  const markers=new Map(), routes=new Map();
  let map=null,vehicles=[],selection=null,network=null,bounds=null,initialFit=false;
  let onChoose=()=>{},failedTiles=0,loadedTiles=0;
  const escape=value=>String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const valid=v=>Number.isFinite(v.lon)&&Number.isFinite(v.lat)&&Math.abs(v.lon)<=180&&Math.abs(v.lat)<=85.0511;
  const delay=v=>Number.isFinite(v)?`${v<0?'−':'+'}${Math.abs(Math.round(v))} с`:'—';
  const arrival=v=>v?new Date(v).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'—';
  function notify(message='') {
    const node=document.getElementById('map-tile-notice');
    if(!node)return;
    node.textContent=message;node.hidden=!message;
  }
  function content(v) {
    const stop=!v.stop_address||['nan','null','none'].includes(String(v.stop_address).toLowerCase())?'Название не указано':v.stop_address;
    return `<h3>ТС ${escape(v.tr_id)}</h3><dl><dt>Прогноз отклонения</dt><dd class="popup-prediction">${delay(v.prediction_s)}</dd><dt>Целевая остановка</dt><dd>${escape(stop)}</dd><dt>Плановое прибытие · МСК</dt><dd>${arrival(v.target_time_begin)}</dd></dl>${v.stale||v.prediction_s==null||v.level==='unknown'?'<p class="popup-caution">Нет свежего прогноза. Проверьте время данных.</p>':''}`;
  }
  function icon(v) {
    return L.divIcon({className:'transit-marker',iconSize:[32,32],iconAnchor:[16,16],popupAnchor:[0,-19],html:`<span class="transit-marker-icon" style="--vehicle-color:${colors[v.level]||colors.unknown}"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="5" y="3" width="14" height="16" rx="3" stroke="currentColor" stroke-width="1.5"/><path d="M5 12h14M8 19v2m8-2v2M9 6h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="8.5" cy="15.5" r="1" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1" fill="currentColor"/></svg></span>`});
  }
  function styleRoutes() {
    const activeIds=new Set(vehicles.map(v=>v.tr_id)),all=document.getElementById('map-all-routes')?.checked??false,byRoute=new Map(vehicles.map(v=>[v.tr_id,v]));
    for(const [id,line] of routes) {
      const selected=id===selection,visible=all||selected||activeIds.has(id)||!vehicles.length,level=byRoute.get(id)?.level||'unknown';
      line.setStyle({color:colors[level]||colors.unknown,weight:selected?5:3,opacity:visible ? (selected ? .95 : .7) : 0});
      if(selected)line.bringToFront();
    }
    for(const [id,marker] of markers)marker.getElement()?.classList.toggle('selected',id===selection);
  }
  function fit() {
    if(!map)return;
    const coordinates=vehicles.filter(valid).map(v=>[v.lat,v.lon]);
    const combined=bounds?L.latLngBounds(bounds.getSouthWest(),bounds.getNorthEast()):L.latLngBounds([]);
    coordinates.forEach(p=>combined.extend(p));
    if(combined.isValid())map.fitBounds(combined,{padding:[35,35],maxZoom:13});
  }
  function mount(callback) {
    onChoose=callback;
    if(!window.L){notify('Не удалось загрузить карту. Таблица и карточка доступны.');return;}
    map=L.map('map',{zoomControl:false,scrollWheelZoom:false,attributionControl:false}).setView([55.75,37.62],10);
    L.control.zoom({position:'topleft',zoomInTitle:'Приблизить',zoomOutTitle:'Отдалить'}).addTo(map);
    const config=window.TRANSIT_MAP;
    L.control.attribution({prefix:false,position:'bottomright'}).addAttribution(config.attribution).addTo(map);
    const tiles=L.tileLayer(config.tileUrl,{subdomains:config.subdomains||'abc',attribution:config.attribution,maxZoom:config.maxZoom,minZoom:config.minZoom||3,updateWhenIdle:true,keepBuffer:1,detectRetina:true}).addTo(map);
    tiles.on('loading',()=>{failedTiles=0;loadedTiles=0;});
    tiles.on('tileerror',()=>{failedTiles++;notify('Подложка недоступна. Линии и транспорт остаются на карте.');});
    tiles.on('tileload',()=>{loadedTiles++;});
    tiles.on('load',()=>{if(loadedTiles>0&&!failedTiles)notify();});
    const fitButton=document.getElementById('map-fit');if(fitButton)fitButton.onclick=fit;
    const allRoutes=document.getElementById('map-all-routes');if(allRoutes)allRoutes.onchange=styleRoutes;
    new ResizeObserver(()=>map.invalidateSize({pan:false})).observe(document.getElementById('map'));
  }
  function render(nextVehicles,nextNetwork,nextSelection) {
    vehicles=nextVehicles;selection=nextSelection;
    if(!map)return;
    if(network!==nextNetwork) {
      routes.forEach(line=>line.remove());routes.clear();network=nextNetwork;
      const all=[];
      for(const path of network) {
        const points=path.points.filter(([lon,lat])=>valid({lon,lat})).map(([lon,lat])=>[lat,lon]);
        const line=points.filter((p,i)=>!i||p[0]!==points[i-1][0]||p[1]!==points[i-1][1]);
        if(line.length<2)continue;
        routes.set(path.tr_id,L.polyline(line,{interactive:false,smoothFactor:1.3}).addTo(map));all.push(...line);
      }
      bounds=all.length?L.latLngBounds(all):null;
    }
    const positioned=vehicles.filter(valid),ids=new Set(positioned.map(v=>v.tr_id));
    document.getElementById('map-empty')?.classList.toggle('show',!positioned.length);
    for(const [id,marker] of markers)if(!ids.has(id)){marker.remove();markers.delete(id);}
    for(const vehicle of positioned) {
      let marker=markers.get(vehicle.tr_id);
      if(!marker) {
        marker=L.marker([vehicle.lat,vehicle.lon],{icon:icon(vehicle),title:`ТС ${vehicle.tr_id}`,keyboard:true,riseOnHover:true}).addTo(map);
        marker.bindPopup(content(vehicle),{className:'transit-popup',minWidth:235,maxWidth:270,autoPanPadding:[18,25]});
        marker.bindTooltip(`ТС ${vehicle.tr_id}`,{className:'transit-map-tooltip',offset:[0,-16],direction:'top'});
        marker.on('click',()=>onChoose(vehicle.tr_id));
        markers.set(vehicle.tr_id,marker);
      } else {
        const position=marker.getLatLng();
        if(position.lat!==vehicle.lat||position.lng!==vehicle.lon)marker.setLatLng([vehicle.lat,vehicle.lon]);
        if(marker.options.level!==vehicle.level)marker.setIcon(icon(vehicle));
        if(marker.getPopup().getContent()!==content(vehicle))marker.setPopupContent(content(vehicle));
      }
      marker.options.level=vehicle.level;
      const node=marker.getElement();if(node){node.setAttribute('role','button');node.setAttribute('aria-label',`ТС ${vehicle.tr_id}: открыть карточку`);}
    }
    if(!initialFit&&(bounds||positioned.length)){fit();initialFit=true;}
    styleRoutes();
  }
  function focus(id) {
    const marker=markers.get(id);if(!map||!marker)return;
    selection=id;styleRoutes();
    if(!map.getBounds().contains(marker.getLatLng()))map.panTo(marker.getLatLng());
    marker.openPopup();
  }
  window.TransitMap={mount,render,focus};
})();
