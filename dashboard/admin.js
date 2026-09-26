(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const PROFILE_KEY = 'takt-dispatcher-profile';
  let dispatchers = [];
  let vehicles = [];
  let assignmentsDirty = false;
  let selectedDispatcher = '';
  let assignmentSaveInFlight = false;

  const api = async (url, options = {}) => {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({detail: 'Ошибка ответа'}));
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    return data;
  };

  const esc = value => String(value ?? '—').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const sourceStatus = source => ({running: 'Работает', paused: 'Пауза', unavailable: 'Недоступен', not_configured: 'Не настроен'}[source] || source || 'Неизвестно');
  const note = (id, value, error = false) => {
    const element = $(id);
    if (!element) return;
    element.textContent = value;
    element.classList.toggle('error', error);
  };

  function operatorId() {
    const profile = localStorage.getItem(PROFILE_KEY);
    return dispatchers.find(item => item.id === profile && item.role === 'Администратор')?.id || 'admin-01';
  }

  function renderEmulators(data, archiveView = false) {
    const overall = $('emulator-overall');
    const sources = $('emulator-sources');
    if (!data) return;
    overall.textContent = data.ingest_paused ? 'Пауза входящего контура' : 'Приём разрешён';
    sources.innerHTML = (data.sources || []).map(source => `
      <article class="emulator-source">
        <div><b>${esc(source.label)}${archiveView ? ' (архив)' : ''}</b><small>${archiveView ? 'Источник не участвует в архивном отображении' : source.id === 'custom-emulator' ? 'Собственная NDTP Nav00 · CRC' : 'Внешний оригинальный образ · /api/config'}</small></div>
        <span class="tag ${source.status === 'running' ? 'low' : source.status === 'paused' ? 'medium' : 'unknown'}">${esc(sourceStatus(source.status))}</span>
        <div class="actions"><button class="secondary" data-emulator-id="${esc(source.id)}" data-emulator-action="pause">Остановить поток</button><button class="secondary" data-emulator-id="${esc(source.id)}" data-emulator-action="resume">Запустить поток</button></div>
      </article>`).join('') || '<p class="muted">Источники не отвечают.</p>';
    document.querySelectorAll('[data-emulator-action]').forEach(button => {
      button.onclick = () => controlEmulator(button.dataset.emulatorId, button.dataset.emulatorAction);
    });
  }

  async function controlEmulator(id, action) {
    try {
      await api(`/api/admin/emulators/${id}/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dispatcher_id: operatorId()})
      });
      note('emulator-notice', `${id}: ${action === 'resume' ? 'поток возобновлён' : 'поток поставлен на паузу'}.`);
      await refresh();
    } catch (error) {
      note('emulator-notice', error.message, true);
    }
  }

  function renderAccounts() {
    const dispatchersOnly = dispatchers.filter(item => item.role !== 'Администратор');
    $('dispatcher-count').textContent = dispatchersOnly.length;
    $('accounts').innerHTML = dispatchers.map(item => `
      <article class="account">
        <div><b>${esc(item.name)}</b><small>${esc(item.role)} · ${esc(item.login)} · назначено: ${item.assigned_tr_ids.length}</small></div>
        <div class="account-actions"><span class="tag">${item.status === 'active' ? 'Активен' : 'Отключён'}</span>${item.role !== 'Администратор' ? `<button class="danger-link" data-delete-dispatcher="${esc(item.id)}" type="button">Удалить</button>` : ''}</div>
      </article>`).join('') || '<p class="muted">Профилей пока нет.</p>';
    document.querySelectorAll('[data-delete-dispatcher]').forEach(button => {
      button.onclick = () => deleteDispatcher(button.dataset.deleteDispatcher);
    });
    // Polling refreshes the account cards every few seconds.  Never rebuild
    // the assignment selector while an operator is editing checkboxes: doing
    // so used to replace the DOM and silently lose the pending selection.
    if (!assignmentsDirty) {
      const select = $('assignment-dispatcher');
      const previous = selectedDispatcher || select.value;
      select.innerHTML = dispatchersOnly.map(item => `<option value="${item.id}">${esc(item.name)} · ${esc(item.role)}</option>`).join('');
      if ([...select.options].some(option => option.value === previous)) selectedDispatcher = previous;
      if (!selectedDispatcher && select.options.length) selectedDispatcher = select.options[0].value;
      select.value = selectedDispatcher;
      renderAssignments();
    }
  }

  function renderAssignments(force = false) {
    if (assignmentsDirty && !force) return;
    const profile = dispatchers.find(item => item.id === $('assignment-dispatcher').value);
    if (!profile) {
      $('route-list').innerHTML = '<p class="muted">Нет доступных диспетчеров.</p>';
      $('assignment-count').textContent = '';
      return;
    }
    const assigned = new Set(profile.assigned_tr_ids || []);
    $('route-list').innerHTML = vehicles.map(vehicle => `
      <label title="${esc(vehicle.stop_address || '')}"><input type="checkbox" value="${vehicle.tr_id}" ${assigned.has(vehicle.tr_id) ? 'checked' : ''}> <span>ТС ${vehicle.tr_id}</span><small>${esc(vehicle.stop_address || 'Остановка не указана')}</small></label>`).join('') || '<p class="muted">Нет ТС в текущей картине.</p>';
    $('assignment-count').textContent = `${assigned.size} назначено`;
    document.querySelectorAll('#route-list input').forEach(input => {
      input.onchange = () => {
        assignmentsDirty = true;
        $('assignment-count').textContent = `${document.querySelectorAll('#route-list input:checked').length} выбрано · не сохранено`;
      };
    });
  }

  async function refresh() {
    try {
      const [profiles, state, emulators] = await Promise.all([
        api('/api/dispatchers'),
        api('/api/state'),
        api('/api/admin/emulators')
      ]);
      dispatchers = profiles.profiles || [];
      vehicles = state.vehicles || [];
      $('mode').textContent = state.state?.mode === 'live' ? 'Live-данные' : 'Архивный снимок';
      $('fleet-count').textContent = vehicles.length;
      renderAccounts();
      renderEmulators(emulators, state.state?.mode !== 'live');
    } catch (error) {
      note('emulator-notice', error.message, true);
    }
  }

  async function deleteDispatcher(id) {
    const profile = dispatchers.find(item => item.id === id);
    if (!profile || !window.confirm(`Удалить кабинет «${profile.name}» и его назначения?`)) return;
    try {
      await api(`/api/admin/dispatchers/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({operator_id: operatorId()})
      });
      if (selectedDispatcher === id) selectedDispatcher = '';
      note('account-notice', `Кабинет «${profile.name}» удалён.`);
      await refresh();
    } catch (error) {
      note('account-notice', error.message, true);
    }
  }

  $('create-dispatcher').onclick = async event => {
    event?.preventDefault();
    try {
      const profile = await api('/api/admin/dispatchers', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: $('new-name').value.trim(), login: $('new-login').value.trim(), role: $('new-role').value})
      });
      $('new-name').value = '';
      $('new-login').value = '';
      note('account-notice', `Кабинет «${profile.name}» создан.`);
      await refresh();
    } catch (error) {
      note('account-notice', error.message, true);
    }
  };

  $('assignment-dispatcher').onchange = event => {
    selectedDispatcher = event.target.value;
    assignmentsDirty = false;
    renderAssignments(true);
  };

  async function saveAssignments(event) {
    event?.preventDefault();
    if (assignmentSaveInFlight) return;
    assignmentSaveInFlight = true;
    const id = $('assignment-dispatcher').value;
    const tr_ids = [...document.querySelectorAll('#route-list input:checked')].map(input => Number(input.value));
    try {
      const profile = await api(`/api/admin/dispatchers/${id}/assignments`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tr_ids})
      });
      assignmentsDirty = false;
      note('assignment-notice', `${profile.name}: сохранено ${profile.assigned_tr_ids.length} назначений.`);
      await refresh();
      renderAssignments(true);
    } catch (error) {
      note('assignment-notice', error.message, true);
    } finally {
      assignmentSaveInFlight = false;
    }
  }

  $('assignment-form').addEventListener('submit', saveAssignments);
  // Keep a direct click path as well as native form submission. This covers
  // embedded browsers that do not synthesize submit for a dynamically focused
  // button; the in-flight guard prevents a double PUT in normal browsers.
  $('save-assignment').addEventListener('click', saveAssignments);

  $('pause-all').onclick = () => controlEmulator('all', 'pause');
  $('resume-all').onclick = () => controlEmulator('all', 'resume');
  $('refresh').onclick = refresh;

  refresh();
  // Do not redraw the assignment checkboxes while an operator is editing them.
  setInterval(() => { if (!assignmentsDirty) refresh(); }, 5000);
})();
