(() => {
  'use strict';

  const $ = id => document.getElementById(id);

  const PROFILE_KEY = 'takt-dispatcher-profile';
  const PREVIOUS_PROFILE_KEY = 'takt-dispatcher-previous-profile';
  const AUTH_KEY = 'takt-dispatcher-auth';
  const THEME_KEY = 'takt-dispatcher-theme';

  const labels = {
    low: 'В графике',
    medium: 'Нужно проверить',
    high: 'Риск опоздания',
    unknown: 'Нет оценки'
  };
  const commandStatuses = {
    queued_for_integration: 'отправляем водителю',
    executing: 'исполняется',
    completed: 'закрыто · ТС вышло из зоны риска',
    blocked_by_guardrail: 'не отправлено',
    integration_timeout: 'не доставлено',
    simulated_completed: 'выполнено'
  };

  const actionText = {
    contact:
      'Просьба подтвердить текущую обстановку, причину отклонения и возможность следовать графику.',

    accelerate_safely:
      'При возможности сократите отставание без нарушения ПДД, скоростного режима и требований безопасности.',

    maintain:
      'Подтвердите возможность выдерживать интервал движения и следовать графику безопасно.',

    slow_down_safely:
      'Согласуйте мягкое снижение темпа, чтобы вернуть интервал к плану без резкого торможения.'
  };

  function actionBenefitReason(option) {
    const action = option?.action;
    const effect = Number(option?.utility?.expected_saved_delay_s || 0);
    if (action === 'contact') {
      return 'Поможет уточнить причину отклонения и выбрать следующий шаг.';
    }
    if (action === 'accelerate_safely') {
      return effect > 0
        ? `Может безопасно сократить ожидаемое отставание примерно на ${duration(effect)}.`
        : 'Подходит только при подтверждённом отставании и достаточном запасе времени.';
    }
    if (action === 'slow_down_safely') {
      return 'Поможет убрать опережение и выровнять интервал движения.';
    }
    return 'Сохраняет текущий режим движения; дополнительного эффекта не ожидается.';
  }

  let vehicles = [];
  let baseVehicles = [];
  let archivedVehicles = [];
  let paths = [];
  let actionCenter = [];
  let actionCenterSummary = {};
  let actionCenterExpiryTimer = null;

  let selectedId = null;

  let profiles = [];
  let profileId =
    localStorage.getItem(PROFILE_KEY) || '';

  const isAuthenticated = () =>
    sessionStorage.getItem(AUTH_KEY) === '1' && Boolean(profileId);

  function applyTheme(theme) {
    const dark = theme === 'dark';
    document.documentElement.classList.toggle('theme-dark', dark);
    const toggle = $('theme-toggle');
    const label = $('theme-toggle-label');
    if (toggle) {
      toggle.setAttribute('aria-pressed', String(dark));
      toggle.setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
      toggle.title = dark ? 'Переключить на светлую тему' : 'Переключить на тёмную тему';
    }
    if (label) label.textContent = dark ? 'Светлая' : 'Тёмная';
  }

  function toggleTheme() {
    const next = document.documentElement.classList.contains('theme-dark')
      ? 'light'
      : 'dark';
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (_) {
      // The UI still switches when local storage is unavailable.
    }
    applyTheme(next);
  }

  let reserveScenario = null;
  let lastRuntime = {};
  let commandFeedback = null;
  let commandDraft = null;
  const CUSTOM_TR_ID_OFFSET = 1000000;
  const debugSpeedOverrides = new Map();
  let debugSpeedDraft = null;
  let debugSpeedFeedback = null;

  /*
   * Состояние раскрытых блоков карточки.
   * Фоновое обновление карточки пересоздаёт часть HTML,
   * поэтому эти значения храним отдельно.
   */
  let explanationOpen = false;
  let commandHistoryOpen = false;

  /*
   * Опрос запускается и после первого выбора профиля, и после перезагрузки.
   * Один цикл намеренно ждёт завершения refresh() перед следующим запросом.
   */
  const REFRESH_INTERVAL_MS = 1000;
  let refreshTimer = null;
  let pollingActive = false;
  let pollingGeneration = 0;
  let refreshPromise = null;
  let networkCache = null;
  let networkLoadedAt = 0;

  /* Данные, которые не должны мигать при каждом обновлении карты. */
  const DETAIL_CACHE_TTL_MS = 3000;
  const detailSupplementCache = new Map();
  let detailRenderVersion = 0;

  function resetCommandDraft() {
    commandDraft = null;
    commandFeedback = null;
  }

  function commandState(vehicle, initialAction = 'contact') {
    if (commandDraft?.trId !== vehicle.tr_id) {
      const action = actionText[initialAction] ? initialAction : 'contact';
      commandDraft = {
        trId: vehicle.tr_id,
        action,
        message: actionText[action],
        sending: false,
        touched: false
      };
    }
    return commandDraft;
  }

  function commandFeedbackHtml(vehicleId) {
    let feedback = commandFeedback?.tr_id === vehicleId ? commandFeedback : null;
    const ticket = actionCenter.find(item => item.tr_id === vehicleId && item.kind === 'driver_command');
    if (ticket?.status === 'pending' && feedback?.kind !== 'error' && feedback?.kind !== 'blocked') {
      feedback = {tr_id: vehicleId, message: 'Отправляем водителю. Ожидаем подтверждение приёма.',
        commandId: ticket.attempt_id, simulatable: true};
    } else if (ticket?.status === 'executing') {
      feedback = {tr_id: vehicleId, kind: 'simulation', commandId: ticket.attempt_id,
        response: feedback?.response || 'Водитель подтвердил приём команды.'};
    } else if (ticket?.status === 'not_delivered') {
      feedback = {tr_id: vehicleId, kind: 'error', message: ticket.result_detail || 'Приём команды не подтверждён.'};
    } else if (ticket?.tone === 'success') {
      return '<span>Закрыт · ТС вышло из зоны риска. Зелёный тикет будет удалён через 15 секунд после закрытия.</span>';
    }
    const journal = detailSupplementCache.get(detailCacheKey(vehicleId))?.items || [];
    if (!ticket && journal.some(item => item.id === feedback?.commandId && item.status === 'completed')) {
      return '<span>ТС вышло из зоны риска. Тикет закрыт и удалён.</span>';
    }
    if (!feedback || feedback.tr_id !== vehicleId) return '';
    if (feedback.kind === 'simulation') {
      return `<b>Исполняется · водитель принял указание</b><span>${esc(feedback.response || 'Водитель подтвердил указание.')}</span><small>Тикет закроется после выхода ТС из зоны риска по новой телеметрии.</small>`;
    }
    if (feedback.kind === 'error') {
      return `<span class="reserve-blocked">${esc(feedback.message)}</span>`;
    }
    const simulate = feedback.simulatable
      ? `<button type="button" class="secondary simulate-command" id="simulate-command" data-command-id="${esc(feedback.commandId)}">Показать реакцию водителя (демо)</button>`
      : '';
    const detail = feedback.detail ? `<small>${esc(feedback.detail)}</small>` : '';
    const demoHint = feedback.simulatable
      ? `<small>${vehicleId >= CUSTOM_TR_ID_OFFSET ? 'Демо подтвердит приём и повысит debug-скорость этого custom-ТС. Оверлей можно сбросить внизу карточки.' : 'У оригинального потока демо подтвердит только приём; debug-ускорение доступно у custom-ТС.'}</small>`
      : '';
    return `<span>${esc(feedback.message)}</span>${simulate}${demoHint}${detail}`;
  }

  const isCustomEmulatorVehicle = vehicle =>
    Number(vehicle?.tr_id) >= CUSTOM_TR_ID_OFFSET;

  function debugSpeedControlHtml(vehicle) {
    const supported = isCustomEmulatorVehicle(vehicle);
    const override = debugSpeedOverrides.get(vehicle.tr_id);
    const draft = debugSpeedDraft?.trId === vehicle.tr_id
      ? debugSpeedDraft.value
      : (override?.speed ?? Math.round(Number(vehicle.features?.speed_last) || 0));
    const feedback = debugSpeedFeedback?.trId === vehicle.tr_id
      ? debugSpeedFeedback.message
      : '';

    if (!supported) {
      return `<section class="action-section debug-speed-section">
        <p class="eyebrow">DEBUG · СКОРОСТЬ ТС</p>
        <p>Для этого ТС источник — оригинальный NDTP-эмулятор. Скорость можно задавать только у отдельной копии из custom-emulator, чтобы не вмешиваться в внешний источник.</p>
      </section>`;
    }

    return `<section class="action-section debug-speed-section">
      <p class="eyebrow">DEBUG · СКОРОСТЬ ТС</p>
      <p>Временный оверлей custom-emulator: ТС продолжает двигаться по той же плановой траектории. Расписание, модель и правила диспетчера не изменяются.</p>
      <div class="debug-speed-controls">
        <label>Скорость, км/ч<input id="debug-speed-value" type="number" min="0" max="130" step="1" value="${esc(draft)}" inputmode="decimal"></label>
        <button type="button" class="secondary" id="debug-speed-apply">Применить</button>
        <button type="button" class="secondary" id="debug-speed-reset">Вернуть темп</button>
      </div>
      <small id="debug-speed-status">${esc(feedback || (override ? `Активен debug-оверлей: ${override.speed} км/ч.` : '«Вернуть темп» сбросит любой debug-оверлей этого ТС, включая демо-ускорение.'))}</small>
    </section>`;
  }


  /* =======================================================
     HELPERS
     ======================================================= */

  const esc = value =>
    String(value ?? '—').replace(
      /[&<>"']/g,
      char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      })[char]
    );


  const time = value =>
    value
      ? new Date(value).toLocaleTimeString(
          'ru-RU',
          {
            timeZone: 'Europe/Moscow',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
          }
        )
      : '—';


  const duration = value => {
    const seconds = Math.round(Math.abs(value));
    return seconds < 60 ? `${seconds} с` : `${Math.floor(seconds / 60)} мин${seconds % 60 ? ` ${seconds % 60} с` : ''}`;
  };
  const delay = value => !Number.isFinite(value) ? '—' : `${value < 0 ? '−' : '+'}${duration(value)}`;
  const expectedDelaySeconds = vehicle => {
    if (vehicle.stale || vehicle.degraded || !Number.isFinite(vehicle.prediction_s) || !Number.isFinite(vehicle.late_probability)) return null;
    return Math.max(0, vehicle.prediction_s) * Math.min(1, Math.max(0, vehicle.late_probability));
  };
  const businessFromVehicles = items => {
    const active = items.filter(vehicle => vehicle.trip_status !== 'completed' && vehicle.trip_status !== 'not_started' && vehicle.on_route !== false);
    const forecasted = active.filter(vehicle => expectedDelaySeconds(vehicle) != null);
    const expected = forecasted.reduce((sum, vehicle) => sum + expectedDelaySeconds(vehicle), 0);
    return {
      coverage_pct: active.length ? (forecasted.length / active.length) * 100 : null,
      expected_delay_minutes: expected / 60
    };
  };
  const predictionText = vehicle => {
    if (vehicle.trip_status === 'completed') return 'Рейс завершён';
    if (!Number.isFinite(vehicle.prediction_s) || vehicle.stale || vehicle.degraded) return vehicle.status_label || 'Нет актуального прогноза';
    if (Math.round(vehicle.prediction_s) === 0) return 'По расписанию';
    return `${vehicle.prediction_s > 0 ? 'Опоздание' : 'Раньше плана'} на ${duration(vehicle.prediction_s)}`;
  };
  const sourceText = vehicle => {
    if (vehicle.scenario) return 'Сценарий резерва';
    if (vehicle.source === 'waiting_for_live') return 'Ожидание телеметрии';
    const source = vehicle.telemetry_source === 'custom_ndtp_nav00'
      ? 'Собственный эмулятор · синтетическая телеметрия'
      : vehicle.telemetry_source === 'ndtp_nav00'
        ? 'Оригинальный эмулятор · синтетическая телеметрия'
        : 'HTTP · телеметрия';
    if (vehicle.live_position) return `${source}. Прогноз: архивный V5`;
    if (vehicle.source !== 'live') return 'Архивная телеметрия · V5';
    return source + (vehicle.position_adjusted ? '. Позиция рассчитана по плану' : '');
  };

  const featureText = vehicle => {
    const features = vehicle.features || {};
    const result = [];

    if (Number.isFinite(features.speed_last)) {
      result.push(
        `скорость ${Math.round(
          features.speed_last
        )} км/ч`
      );
    }

    if (Number.isFinite(features.speed_trend)) {
      result.push(
        `изменение скорости ${
          features.speed_trend >= 0 ? '+' : ''
        }${Math.round(
          features.speed_trend
        )} км/ч/мин`
      );
    }

    if (
      Number.isFinite(features.idle_s) &&
      features.idle_s > 0
    ) {
      result.push(
        `простой ${Math.round(
          features.idle_s
        )} с`
      );
    }

    if (Number.isFinite(features.age_s)) {
      result.push(
        `возраст GPS ${Math.round(
          features.age_s
        )} с`
      );
    }

    return result.length
      ? result.join(' · ')
      : 'Диагностические признаки не переданы.';
  };


  const api = async (
    url,
    options = {}
  ) => {
    const response = await fetch(
      url,
      {cache: 'no-store', ...options}
    );

    const data = await response
      .json()
      .catch(() => ({
        detail: 'Ошибка ответа'
      }));

    if (!response.ok) {
      const detail = typeof data.detail === 'string'
        ? data.detail
        : data.detail?.message || JSON.stringify(data.detail || `HTTP ${response.status}`);
      throw Error(
        detail ||
        `HTTP ${response.status}`
      );
    }

    return data;
  };


  /* =======================================================
     PROFILE
     ======================================================= */

  const currentProfile = () =>
    profiles.find(
      profile => profile.id === profileId
    ) || profiles[0];


  function updateProfile() {
    const profile = currentProfile();

    if (!profile) return;

    $('profile-name').textContent =
      profile.name;

    profileId = profile.id;

    localStorage.setItem(
      PROFILE_KEY,
      profile.id
    );

    $('profile-select').value =
      profile.id;
  }


  async function loadProfiles() {
    const data =
      await api('/api/dispatchers');

    profiles = data.profiles || [];

    $('profile-select').innerHTML =
      profiles
        .map(
          profile =>
            `<option value="${profile.id}">
              ${esc(profile.name)} ·
              ${esc(profile.role)}
            </option>`
        )
        .join('');

    if (!profiles.some(profile => profile.id === profileId)) {
      profileId = '';
    }

    // Администратор не остаётся в рабочем диспетчерском контуре. После
    // возврата со страницы админки восстанавливаем последнее рабочее место.
    const storedProfile = profiles.find(profile => profile.id === profileId);
    if (storedProfile?.role === 'Администратор') {
      const previous = localStorage.getItem(PREVIOUS_PROFILE_KEY);
      if (profiles.some(profile => profile.id === previous && profile.role !== 'Администратор')) {
        profileId = previous;
        localStorage.setItem(PROFILE_KEY, profileId);
      } else {
        profileId = '';
      }
    }

    if (profileId) updateProfile();
  }

  async function refreshSourceStatus() {
    const box = $('source-status');
    if (!box) return;
    try {
      const [data, runtime] = await Promise.all([api('/api/admin/emulators'), api('/api/state')]);
      const archiveView = runtime.state?.mode !== 'live';
      const canControl = Boolean(currentProfile()?.id);
      const sourceStatus = status => ({running: 'Работает', paused: 'Пауза', unavailable: 'Недоступен', not_configured: 'Не настроен', unknown: 'Неизвестно'}[status] || 'Неизвестно');
      box.innerHTML = (data.sources || []).map(source => `
        <div class="source-status-row">
          <span><b>${esc(source.label)}${archiveView ? ' (архив)' : ''}</b><small>${archiveView ? 'Источник не участвует в архивном отображении' : source.id === 'custom-emulator' ? 'Собственная NDTP Nav00' : 'Оригинальный NDTP-образ'}</small></span>
          <strong class="tag ${source.status === 'running' ? 'low' : source.status === 'paused' ? 'medium' : 'unknown'}">${esc(sourceStatus(source.status))}</strong>
          ${canControl ? '<span class="source-actions"><button type="button" data-source-id="' + esc(source.id) + '" data-source-action="pause">Остановить поток</button><button type="button" data-source-id="' + esc(source.id) + '" data-source-action="resume">Запустить поток</button></span>' : ''}
        </div>`).join('') || '<p>Нет доступных источников.</p>';
      box.querySelectorAll('[data-source-action]').forEach(button => {
        button.addEventListener('click', () => controlSource(button.dataset.sourceId, button.dataset.sourceAction));
      });
    } catch (error) {
      box.textContent = `Статус источников недоступен: ${error.message}`;
    }
  }

  async function controlSources(action) {
    const profile = currentProfile();
    if (!profile?.id) {
      const box = $('source-status');
      if (box) box.textContent = 'Сначала выберите рабочий профиль.';
      return;
    }
    const operator = profile.id;
    try {
      await api(`/api/admin/emulators/all/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dispatcher_id: operator})
      });
      await refreshSourceStatus();
    } catch (error) {
      const box = $('source-status');
      if (box) box.textContent = error.message;
    }
  }

  async function controlSource(sourceId, action) {
    const profile = currentProfile();
    if (!profile?.id) {
      const box = $('source-status');
      if (box) box.textContent = 'Сначала выберите рабочий профиль.';
      return;
    }
    const operator = profile.id;
    try {
      await api(`/api/admin/emulators/${sourceId}/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dispatcher_id: operator})
      });
      await refreshSourceStatus();
    } catch (error) {
      const box = $('source-status');
      if (box) box.textContent = error.message;
    }
  }


  /* =======================================================
     RESERVE SCENARIO
     ======================================================= */

  function renderReserveOptions() {
    const select =
      $('reserve-route');

    if (!select) return;

    const previous =
      select.value ||
      reserveScenario?.routeId;

    const sorted = [
      ...baseVehicles
    ].sort(
      (a, b) =>
        Number(b.attention_level === 'critical') - Number(a.attention_level === 'critical') ||
        (
          a.level === 'high'
            ? -1
            : 1
        ) -
        (
          b.level === 'high'
            ? -1
            : 1
        ) ||
        Math.abs(
          b.prediction_s || 0
        ) -
        Math.abs(
          a.prediction_s || 0
        )
    );

    select.innerHTML =
      sorted
        .map(
          vehicle =>
            `<option value="${vehicle.tr_id}">
              ТС ${vehicle.tr_id} ·
              ${esc(
                vehicle.position_match?.next_stop_address ||
                vehicle.stop_address ||
                'линия'
              )}
            </option>`
        )
        .join('');

    if (
      sorted.some(
        vehicle =>
          String(vehicle.tr_id) ===
          String(previous)
      )
    ) {
      select.value = previous;
    } else if (sorted[0]) {
      select.value =
        sorted[0].tr_id;
    }
  }


  function scenarioVehicle(
    route,
    projected,
    placement,
    reason,
    headway
  ) {
    const id =
      -Math.abs(
        Number(route.tr_id)
      );

    // The backend chooses a stop near half of the remaining route distance.
    const lon = Number(placement?.lon ?? route.position_match?.projected_lon ?? route.lon);
    const lat = Number(placement?.lat ?? route.position_match?.projected_lat ?? route.lat);

    return {
      ...route,

      tr_id: id,
      route_id: route.tr_id,

      scenario: true,

      scenario_reason: reason,
      scenario_headway: headway,

      level:
        projected?.projected_level ||
        'low',

      late_probability:
        projected
          ?.projected_late_probability ??
        null,

      // A reserve has no independent ML forecast.
      prediction_s:
        placement?.reserve_prediction_s ??
        null,

      current_deviation_s:
        placement?.reserve_current_deviation_s ??
        null,

      deviation_estimated: true,

      recommendation:
        'Резервное ТС · what-if',

      source:
        'what_if',

      telemetry_source:
        'scenario',

      lon,
      lat,

      live_track: placement?.track || [{lon, lat, simulated: true}],

      scenario_placement: placement,

      stop_address:
        placement?.target_stop_address ||
        route.stop_address ||
        'Линия'
    };
  }


  async function runReserveScenario() {
    const routeId =
      Number(
        $('reserve-route').value
      );

    const route =
      baseVehicles.find(
        vehicle =>
          Number(vehicle.tr_id) ===
          routeId
      );

    const reason = 'reserve';
    const headway = 0;

    const result =
      $('reserve-result');

    if (!route) {
      result.innerHTML =
        '<span>Нет линии с доступной позицией для сценария.</span>';

      return;
    }

    const button =
      $('reserve-run');

    button.disabled = true;

    result.innerHTML =
      '<span>Считаю влияние на риск линии…</span>';

    try {
      const data =
        await api(
          '/api/what-if',
          {
            method: 'POST',

            headers: {
              'Content-Type':
                'application/json'
            },

            body: JSON.stringify({
              tr_id: routeId,
              extra_vehicles: 1,
              headway_reduction_pct:
                headway
            })
          }
        );

      let projected =
        (
          data.projected || []
        ).find(
          item =>
            Number(item.tr_id) ===
            routeId
        );

      if (!projected) {
        const before =
          Number.isFinite(
            route.late_probability
          )
            ? route.late_probability
            : null;

        const after =
          before == null
            ? null
            : before;

        const level =
          after == null
            ? 'unknown'
            : after >= 0.7
              ? 'high'
              : after >= 0.35
                ? 'medium'
                : 'low';

        projected = {
          tr_id: routeId,
          max_late_probability:
            before,
          projected_late_probability:
            after,
          projected_level:
            level
        };
      }

      const before =
        projected
          ?.max_late_probability;

      const after =
        projected
          ?.projected_late_probability;

      reserveScenario = {
        routeId,
        reason,
        headway,
        before,
        after,
        decision: data.decision || {},

        vehicle:
          scenarioVehicle(
            route,
            projected,
            data.placement,
            reason,
            headway
          )
      };

      vehicles = [
        ...baseVehicles,
        reserveScenario.vehicle
      ];

      selectedId =
        reserveScenario
          .vehicle
          .tr_id;

      render(lastRuntime);

      const change =
        before != null &&
        after != null
          ? `${Math.round(
              before * 100
            )}% → ${Math.round(
              after * 100
            )}%`
          : 'риск не рассчитан';

      const placement = data.placement || {};
      const savedExpectedDelay = Number(data.impact?.expected_stop_compensation_minutes);
      const impactText = Number.isFinite(savedExpectedDelay)
        ? `Ожидаемая компенсация по остановкам: ${savedExpectedDelay.toFixed(1)} мин.`
        : 'Компенсация не рассчитана.';
      const decision = data.decision || {};
      const blockers = (decision.blockers || []).join('; ');
      const releaseControl = decision.allowed
        ? `<button type="button" class="primary" id="reserve-release">Зарегистрировать выпуск</button>`
        : `<span class="reserve-blocked">Выпуск заблокирован: ${esc(blockers || 'недостаточно подтверждений')}</span>`;
      result.innerHTML =
        `<b>Резерв мгновенно размещён около середины оставшегося пути</b>
         <span>
           Основное ТС ${routeId}: прогноз ${delay(placement.before_prediction_s)} (без изменения). ${impactText}
           Остановка размещения: ${esc(placement.next_stop_address || 'не определена')}. Компенсировано остановок: ${placement.compensation?.compensated_stops || 0}. Сценарная вероятность пользы: ${Math.round((placement.compensation?.benefit_probability || 0) * 100)}%.
           Пунктир показывает оставшийся путь резерва.
         </span>
         <span>ETA резерва ${duration(placement.reserve_eta_s)} · запас ${placement.slack_s >= 0 ? '+' : '−'}${duration(Math.abs(placement.slack_s || 0))} · уверенность ${Math.round((placement.confidence || 0) * 100)}%.</span>
         ${releaseControl}
         <button
           type="button"
           class="link-button"
           id="reserve-clear"
         >
           Убрать сценарий
         </button>`;

      $('reserve-clear').onclick =
        clearReserveScenario;
      if (decision.allowed) $('reserve-release').onclick = releaseReserve;

      openVehicle(
        reserveScenario
          .vehicle
          .tr_id
      );

    } catch (error) {
      result.innerHTML =
        `<span>
          Расчёт не выполнен:
          ${esc(error.message)}
        </span>`;
    } finally {
      button.disabled = false;
    }
  }


  async function releaseReserve() {
    if (!reserveScenario) return;
    const result = $('reserve-result');
    const button = $('reserve-release');
    if (button) button.disabled = true;
    try {
      const action = await api('/api/reserve-dispatches', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dispatcher_id: profileId, tr_id: reserveScenario.routeId})
      });
      reserveScenario.release = action;
      const saved = Number(action.decision?.expected_effect?.saved_expected_delay_s);
      result.innerHTML =
        `<b>Выпуск резерва зарегистрирован</b>
         <span>Заявка ${esc(action.id)} ожидает подтверждения флота. ${Number.isFinite(saved) ? `Ожидаемое снижение задержки: ${saved.toFixed(1)} с.` : ''}</span>
         <button type="button" class="primary" id="reserve-simulate">Показать подтверждение флота (демо)</button>
         <button type="button" class="link-button" id="reserve-clear">Убрать сценарий</button>`;
      $('reserve-simulate').onclick = () => simulateReserve(action.id);
      $('reserve-clear').onclick = clearReserveScenario;
      await refresh();
    } catch (error) {
      if (button) button.disabled = false;
      result.innerHTML += `<span class="reserve-blocked">Заявка не зарегистрирована: ${esc(error.message)}</span>`;
    }
  }

  async function simulateReserve(actionId) {
    const button = $('reserve-simulate');
    if (button) button.disabled = true;
    try {
      const action = await api(`/api/reserve-dispatches/${encodeURIComponent(actionId)}/simulate`, {method: 'POST'});
      const response = action.simulated_response || {};
      const projection = response.projection || {};
      const before = projection.prediction_before_s;
      const after = projection.prediction_after_s;
      $('reserve-result').innerHTML =
        `<b>Флот подтвердил выпуск резерва</b>
         <span>${esc(response.response || 'Подтверждение получено.')} Прогноз основного ТС: ${delay(before)} (без изменения). Ожидаемая компенсация по остановкам: ${(Number(projection.expected_saved_delay_s || 0) / 60).toFixed(1)} мин.</span>
         <button type="button" class="link-button" id="reserve-clear">Убрать сценарий</button>`;
      $('reserve-clear').onclick = clearReserveScenario;
      await refresh();
    } catch (error) {
      if (button) button.disabled = false;
      $('reserve-result').insertAdjacentHTML('beforeend', `<span class="reserve-blocked">Подтверждение не получено: ${esc(error.message)}</span>`);
    }
  }


  function clearReserveScenario() {
    reserveScenario = null;

    vehicles = [
      ...baseVehicles
    ];

    selectedId =
      vehicles[0]?.tr_id ||
      null;

    $('reserve-result').innerHTML =
      '<span>Выберите линию, чтобы проверить выпуск резервного ТС.</span>';

    render(lastRuntime);
  }


  /* =======================================================
     MAIN RENDER
     ======================================================= */

  function render(
    runtime = {},
    { refreshDetail = true } = {}
  ) {
    lastRuntime = runtime;

    const priority = {
      high: 0,
      medium: 1,
      low: 2,
      unknown: 3
    };

    vehicles.sort(
      (a, b) =>
        Number(b.attention_level === 'critical') - Number(a.attention_level === 'critical') ||
        (expectedDelaySeconds(b) ?? -1) - (expectedDelaySeconds(a) ?? -1) ||
        (
          priority[a.level] ?? 9
        ) -
        (
          priority[b.level] ?? 9
        ) ||
        Math.abs(
          b.prediction_s || 0
        ) -
        Math.abs(
          a.prediction_s || 0
        )
    );

    if (
      !vehicles.some(
        vehicle =>
          vehicle.tr_id ===
          selectedId
      )
    ) {
      selectedId =
        vehicles[0]?.tr_id ||
        null;
    }

    const counters =
      runtime.counters || {};

    const business = runtime.business || businessFromVehicles(baseVehicles);

    const live =
      runtime.state
        ?.mode === 'live';

    const liveForecasts =
      baseVehicles.filter(
        vehicle =>
          vehicle.source === 'live' &&
          vehicle.prediction_s != null
      ).length;


    /* KPI */

    $('kpi-total').textContent =
      vehicles.filter(v => v.on_route !== false).length;

    $('kpi-attention').textContent =
      baseVehicles.filter(
        vehicle =>
          vehicle.level === 'high' ||
          vehicle.level === 'medium'
      ).length;

    $('kpi-predictions').textContent = baseVehicles.filter(v => v.source === 'live' && !v.stale && !v.degraded && Number.isFinite(v.prediction_s)).length;

    const processingMs = Number(counters.last_inference_ms);
    $('kpi-processing').textContent = Number.isFinite(processingMs) ? `${processingMs} мс` : '—';
    $('kpi-coverage').textContent = Number.isFinite(business.coverage_pct)
      ? `${Math.round(Number(business.coverage_pct))}%`
      : '—';

    /* stream status */

    $('stream-title').textContent =
      live
        ? (
            counters.ndtp_packets
              ? 'Поток данных активен'
              : 'Ожидание данных'
          )
        : 'Исторические данные';
    $('source-control-open').textContent = live ? 'Источники данных · сейчас' : 'Источники данных · архив';

    $('archive-time').textContent =
      live
        ? (
            `${counters.ndtp_packets || 0} пакетов · ` +
            `${liveForecasts} актуальных прогнозов` +
            (archivedVehicles.length ? ` · ${archivedVehicles.length} архивных рейсов скрыто` : '')
          )
        : (
            baseVehicles[0]?.T
              ? (
                  'Данные на ' +
                  new Date(
                    baseVehicles[0].T
                  ).toLocaleDateString(
                    'ru-RU',
                    {
                      timeZone:
                        'Europe/Moscow'
                    }
                  )
                )
              : 'Нет данных'
          );

    $('stream-dot')
      .classList
      .toggle(
        'waiting',
        live &&
        !counters.ndtp_packets
      );


    renderReserveOptions();
    renderAttention();
    renderActionCenter();
    renderTable();

    window.TransitMap.render(
      vehicles.filter(v => v.on_route !== false),
      paths,
      selectedId
    );

    /*
     * Если drawer открыт,
     * обновляем данные внутри карточки,
     * но сохраняем состояние details.
     */
    if (
      refreshDetail &&
      $('drawer')
        .classList
        .contains('open')
    ) {
      renderDetail();
    }
  }


  /* =======================================================
     ATTENTION LIST
     ======================================================= */

  function renderAttention() {
    const attention =
      [...baseVehicles]
        .filter(
          vehicle =>
            vehicle.level === 'high' ||
            vehicle.level === 'medium'
        )
        .sort((a,b) =>
        Number(b.attention_level === 'critical') - Number(a.attention_level === 'critical') ||
        (expectedDelaySeconds(b) ?? -1) - (expectedDelaySeconds(a) ?? -1) ||
        ({high:0, medium:1, low:2, unknown:3}[a.level] ?? 9) - ({high:0, medium:1, low:2, unknown:3}[b.level] ?? 9)
      )
        .slice(
          0,
          6
        );

    $('attention-list').innerHTML =
      attention.length
        ? attention
            .map(
              vehicle =>
                `<button
                  class="attention-item"
                  data-id="${vehicle.tr_id}"
                >
                  <i
                    class="attention-bar ${vehicle.level}"
                  ></i>

                  <span>
                    <b>
                      ТС ${vehicle.tr_id}
                    </b>

                    <small>
                      ${esc(
                        vehicle.position_match?.next_stop_address ||
                        vehicle.stop_address
                      )}${expectedDelaySeconds(vehicle) > 0 ? ` · эффект ${duration(expectedDelaySeconds(vehicle))}` : ''}
                    </small>
                  </span>

                  <span
                    class="attention-time"
                  >
                    ${delay(
                      vehicle
                        .prediction_s
                    )}

                    <span>
                      ${
                        esc(vehicle.status_label || labels[vehicle.level])
                      }
                    </span>
                  </span>
                </button>`
            )
            .join('')
        : (
            '<p class="empty">' +
            'Нет рейсов, требующих вмешательства' +
            '</p>'
          );

    document
      .querySelectorAll(
        '.attention-item'
      )
      .forEach(
        item => {
          item.onclick = () =>
            openVehicle(
              Number(
                item.dataset.id
              )
            );
        }
      );
  }


  /* =======================================================
     ACTION CENTER
     ======================================================= */

  function renderActionCenter() {
    const list = $('action-center-list');
    const summary = $('action-center-summary');
    if (!list) return;

    if (actionCenterExpiryTimer) {
      clearTimeout(actionCenterExpiryTimer);
      actionCenterExpiryTimer = null;
    }
    const now = Date.now();
    actionCenter = actionCenter.filter(item =>
      item.tone !== 'success' || !item.visible_until || new Date(item.visible_until).getTime() > now
    );
    const expiries = actionCenter
      .filter(item => item.tone === 'success' && item.visible_until)
      .map(item => new Date(item.visible_until).getTime())
      .filter(value => Number.isFinite(value) && value > now);
    if (expiries.length) {
      actionCenterExpiryTimer = setTimeout(renderActionCenter, Math.max(50, Math.min(...expiries) - now + 20));
    }

    if (summary) {
      const attention = actionCenter.filter(item => ['worsened', 'no_result', 'not_delivered'].includes(item.tone)).length;
      const pending = actionCenter.filter(item => item.status === 'pending').length;
      const executing = actionCenter.filter(item => item.status === 'executing').length;
      const success = actionCenter.filter(item => item.tone === 'success').length;
      summary.textContent = [
        attention ? `${attention} требуют решения` : '',
        pending ? `${pending} ожидают ответа` : '',
        executing ? `${executing} исполняются` : '',
        success ? `${success} успешно` : ''
      ].filter(Boolean).join(' · ') || 'Нет активных результатов';
    }

    if (!actionCenter.length) {
      list.innerHTML = '<p class="empty">Ваших активных действий пока нет. Выберите ТС и отправьте указание или выпустите резерв.</p>';
      return;
    }

    list.innerHTML = actionCenter.slice(0, 8).map(item => {
      const tone = ['success', 'no_result', 'not_delivered', 'worsened', 'pending', 'executing'].includes(item.tone)
        ? item.tone
        : 'pending';
      const icons = {success: '✓', no_result: '!', not_delivered: '↛', worsened: '↑', pending: '…', executing: '▶'};
      const attempt = Number(item.revision || 1);
      return `<button class="action-center-item is-${tone}" data-id="${item.tr_id}" data-case-id="${esc(item.id)}" type="button" aria-label="ТС ${item.tr_id}: ${esc(item.status_label)}">
        <span class="action-center-mark" aria-hidden="true">${icons[tone]}</span>
        <span class="action-center-copy">
          <b>ТС ${item.tr_id} · ${esc(item.action_title)}</b>
          <small>${esc(item.result_detail || '')}</small>
          <small class="action-center-meta">Ваше действие · попытка ${attempt} · ${time(item.updated_at)}</small>
        </span>
        <span class="action-center-status">${esc(item.status_label)}</span>
      </button>`;
    }).join('');

    document.querySelectorAll('#action-center-list .action-center-item').forEach(item => {
      item.onclick = () => openVehicle(Number(item.dataset.id));
    });
  }


  /* =======================================================
     TABLE
     ======================================================= */

  function renderTable() {
    const query =
      $('search')
        .value
        .trim();

    const rows =
      vehicles.filter(
        vehicle =>
          !query ||
          String(
            vehicle.tr_id
          ).includes(query)
      );

    $('fleet-rows').innerHTML =
      rows.length
        ? rows
            .map(
              vehicle =>
                `<tr
                  data-id="${vehicle.tr_id}"
                  class="${
                    vehicle.scenario
                      ? 'scenario-row'
                      : ''
                  }"
                >
                  <td>
                    <b>
                      ${
                        vehicle.scenario
                          ? 'РЕЗЕРВ'
                          : (
                              'ТС ' +
                              vehicle.tr_id
                            )
                      }
                    </b>

                    <small>
                      ${
                        vehicle.scenario
                          ? 'what-if · не live'
                          : ''
                      }
                    </small>
                  </td>

                  <td>
                    <span
                      class="tag ${vehicle.level}"
                    >
                      ${
                        vehicle.scenario
                          ? 'Сценарий'
                          : esc(vehicle.status_label || labels[vehicle.level])
                      }
                    </span>
                  </td>

                  <td>
                    <b>
                      ${delay(
                        vehicle
                          .prediction_s
                      )}
                    </b>

                  </td>

                  <td>
                    <b>${esc(vehicle.stop_address)}</b>

                  </td>

                  <td>
                    ${esc(
                      vehicle
                        .recommendation ||
                      'Продолжить наблюдение'
                    )}
                  </td>
                </tr>`
            )
            .join('')
        : (
            '<tr>' +
            '<td colspan="5">' +
            'По этому номеру рейсов нет.' +
            '</td>' +
            '</tr>'
          );

    document
      .querySelectorAll(
        '#fleet-rows tr[data-id]'
      )
      .forEach(
        row => {
          row.onclick = () =>
            openVehicle(
              Number(
                row.dataset.id
              )
            );
        }
      );
  }


  /* =======================================================
     DRAWER
     ======================================================= */

  function openVehicle(id) {
    /*
     * Если выбран другой ТС —
     * раскрытые секции начинаем заново.
     */
    if (selectedId !== id) {
      explanationOpen = false;
      commandHistoryOpen = false;
      resetCommandDraft();
    }

    selectedId = id;

    const selected =
      vehicles.find(
        vehicle =>
          vehicle.tr_id === id
      );

    const routeId =
      selected?.route_id ||
      id;

    if (
      !vehicles.find(
        vehicle =>
          vehicle.scenario
      )
    ) {
      $('reserve-card')
        ?.classList
        .remove(
          'is-hidden'
        );

      if ($('reserve-route')) {
        $('reserve-route').value =
          routeId;

        renderReserveOptions();

        $('reserve-route').value =
          routeId;
      }
    }

    window.TransitMap.render(
      vehicles.filter(v => v.on_route !== false),
      paths,
      selectedId
    );

    /*
     * focus() включает слежение
     * за выбранным ТС.
     */
    window.TransitMap.focus(id);

    renderDetail();

    $('drawer')
      .classList
      .add('open');

    $('drawer')
      .setAttribute(
        'aria-hidden',
        'false'
      );

    /*
     * Сдвигаем desktop-layout,
     * чтобы drawer не перекрывал экран.
     */
    document.body
      .classList
      .add('drawer-open');
  }


  function closeDrawer() {
    $('drawer')
      .classList
      .remove('open');

    $('drawer')
      .setAttribute(
        'aria-hidden',
        'true'
      );

    document.body
      .classList
      .remove('drawer-open');

    explanationOpen = false;
    commandHistoryOpen = false;
    resetCommandDraft();
  }


  /* =======================================================
     COMMAND HISTORY
     ======================================================= */

  async function commandHistory(id) {
    const data =
      await api(
        `/api/driver-commands?tr_id=${id}`
      );

    return data.items.slice(
      0,
      4
    );
  }

  const detailCacheKey = (vehicleId, dispatcherId = profileId) =>
    `${dispatcherId}:${vehicleId}`;

  const commandHistoryHtml = items =>
    items.length
      ? items.map(item =>
          `<li>
            <b>${esc(item.dispatcher?.name || 'Диспетчер')}</b>:
            ${esc(item.action_title)}
            <small>${time(item.created_at)} · ${esc(commandStatuses[item.status] || item.status || 'статус не указан')}</small>
          </li>`
        ).join('')
      : '<li>Указаний пока нет.</li>';

  function syncRecommendedAction(plan, draft) {
    const allowedOptions = plan?.options?.filter(option => option.decision?.allowed === true) || [];
    const recommended = allowedOptions.find(option => option.action === plan.recommended_action);
    if (!draft.touched && recommended && actionText[recommended.action]) {
      draft.action = recommended.action;
      draft.message = actionText[recommended.action];
    }
    return allowedOptions;
  }

  function actionPlanHtml(plan, draft) {
    const allowedOptions = syncRecommendedAction(plan, draft);
    if (!allowedOptions.length) {
      const blocker = plan?.options?.flatMap(option => option.decision?.blockers || [])[0];
      return `<p class="action-unavailable">Сейчас безопасное действие не определено.${blocker ? ` ${esc(blocker)}.` : ''}</p>`;
    }
    return allowedOptions.map(option => {
      const score = Number(option.utility?.score);
      const scoreText = Number.isFinite(score) ? `${Math.round(score)}% пользы` : 'Польза не рассчитана';
      const recommended = option.action === plan.recommended_action;
      const selected = option.action === draft.action;
      return `<button type="button" class="action-plan-option ${recommended ? 'is-recommended' : ''} ${selected ? 'is-selected' : ''}" data-plan-action="${esc(option.action)}" aria-pressed="${selected}">
        <span><span class="action-plan-title"><b>${esc(option.title)}</b>${recommended ? '<em>Лучший вариант</em>' : ''}</span><small>${esc(actionBenefitReason(option))}</small></span>
        <strong>${scoreText}</strong>
      </button>`;
    }).join('');
  }

  function bindActionPlanOptions(actionPlan, draft) {
    actionPlan?.querySelectorAll('[data-plan-action]').forEach(button => {
      button.onclick = () => {
        if (button.disabled) return;
        const target = button.dataset.planAction;
        if (!actionText[target]) return;
        draft.action = target;
        draft.message = actionText[target];
        draft.touched = true;
        commandFeedback = null;
        renderDetail();
      };
    });
  }

  function loadDetailSupplement(vehicleId, dispatcherId = profileId) {
    const key = detailCacheKey(vehicleId, dispatcherId);
    const cached = detailSupplementCache.get(key) || {loadedAt: 0, promise: null};
    detailSupplementCache.set(key, cached);

    if (cached.promise || Date.now() - cached.loadedAt < DETAIL_CACHE_TTL_MS) {
      return cached.promise || Promise.resolve(cached);
    }

    cached.promise = Promise.all([
      commandHistory(vehicleId),
      api(`/api/action-plan?tr_id=${vehicleId}&dispatcher_id=${encodeURIComponent(dispatcherId)}`).catch(() => null)
    ]).then(([items, plan]) => {
      cached.items = items;
      cached.plan = plan;
      cached.loadedAt = Date.now();
      return cached;
    }).finally(() => {
      cached.promise = null;
    });

    return cached.promise;
  }

  function invalidateDetailSupplement(vehicleId, dispatcherId = profileId) {
    detailSupplementCache.delete(detailCacheKey(vehicleId, dispatcherId));
  }


  /* =======================================================
     DETAIL
     ======================================================= */

  async function renderDetail() {
    const renderVersion = ++detailRenderVersion;
    const vehicle =
      vehicles.find(
        item =>
          item.tr_id === selectedId
      );

    if (!vehicle) return;


    /* -----------------------------------------------------
       SCENARIO
       ----------------------------------------------------- */

    if (vehicle.scenario) {
      const change =
        reserveScenario?.before != null &&
        reserveScenario?.after != null
          ? (
              `${Math.round(
                reserveScenario.before *
                100
              )}% → ` +
              `${Math.round(
                reserveScenario.after *
                100
              )}%`
            )
          : 'риск не рассчитан';

      $('vehicle-detail').innerHTML =
        `<div class="vehicle-title">

          <p class="eyebrow">
            WHAT-IF · РЕЗЕРВ
          </p>

          <h2>
            Резервное ТС
          </h2>

          <p>
            Линия ТС ${vehicle.route_id}
            ·
            ${esc(
              vehicle.stop_address
            )}
          </p>

        </div>

        <div
          class="detail-status scenario-status"
        >
          <span class="tag low">
            Сценарий
          </span>

          <b>
            Сценарная вероятность пользы ${Math.round((vehicle.scenario_placement?.compensation?.benefit_probability || 0) * 100)}%
          </b>
        </div>

        <section class="prediction-panel">
          <h3>
            Добавлено на карту
          </h3>

          <p>
            Маркер и пунктирная линия
            показывают виртуальное ТС.
            Это расчёт для решения
            диспетчера, не команда
            в live-контур.
          </p>
        </section>

        <div class="detail-grid">

          <div>
            <span>
              ПРИЧИНА
            </span>

            <b>
              Дополнительное ТС на плановой траектории
            </b>
          </div>

          <div>
            <span>
              РАЗМЕЩЕНИЕ
            </span>

            <b>
              На позиции выбранной линии
            </b>
          </div>

          <div>
            <span>
              БЫЛО → СТАЛО
            </span>

            <b>
              ${change}
            </b>
          </div>

          <div>
            <span>
              ИСТОЧНИК
            </span>

            <b>
              POST /api/what-if
            </b>
          </div>

        </div>

        <section class="action-section">
          <button
            class="primary"
            type="button"
            id="scenario-remove"
          >
            Убрать сценарий
          </button>
        </section>`;

      $('scenario-remove').onclick =
        clearReserveScenario;

      return;
    }


    /* -----------------------------------------------------
       NORMAL VEHICLE
       ----------------------------------------------------- */

    const expectedArrival = Number.isFinite(vehicle.prediction_s) && vehicle.target_time_begin && !vehicle.stale && !vehicle.degraded
      ? new Date(new Date(vehicle.target_time_begin).getTime() + vehicle.prediction_s * 1000).toISOString() : null;
    const risk = Number.isFinite(vehicle.late_probability) && !vehicle.stale
      ? `${Math.round(vehicle.late_probability * 100)}%` : '—';
    const recommendation = vehicle.dispatcher_recommendation || {};
    const initialAction = actionText[recommendation.action] ? recommendation.action : 'contact';
    const draft = commandState(vehicle, initialAction);
    const supplementKey = detailCacheKey(vehicle.tr_id);
    const supplement = detailSupplementCache.get(supplementKey);
    const initialActionPlan = supplement?.plan?.options?.length
      ? actionPlanHtml(supplement.plan, draft)
      : '<small>Сравниваю доступные действия…</small>';
    const activeCommand = document.activeElement?.id === 'command-text'
      ? {
          start: $('command-text').selectionStart,
          end: $('command-text').selectionEnd
        }
      : null;
    const activeDebugSpeed = document.activeElement?.id === 'debug-speed-value';
    const previousActionPlan = $('action-plan')?.dataset.cacheKey === supplementKey ? $('action-plan') : null;
    $('vehicle-detail').innerHTML =
      `<div class="vehicle-title"><p class="eyebrow">РЕЙС</p><h2>ТС ${vehicle.tr_id}</h2><p>${esc(vehicle.route_start_stop || '—')} → ${esc(vehicle.route_end_stop || '—')}</p></div>
      <div class="detail-status"><span class="tag ${vehicle.level}">${esc(vehicle.status_label || labels[vehicle.level])}</span></div>
      <section class="prediction-panel">
        <p class="eyebrow">ПРОГНОЗ НА КОНТРОЛЬНОЙ ТОЧКЕ</p>
        <h3>${esc(predictionText(vehicle))}</h3>
        <p>${esc(Number.isFinite(vehicle.prediction_s) ? vehicle.stop_address : vehicle.reason)}</p>
      </section>
      <div class="detail-grid">
        <div><span>ПО ПЛАНУ · МСК</span><b>${time(vehicle.target_time_begin)}</b></div>
        <div><span>ОЖИДАЕТСЯ · МСК</span><b>${time(expectedArrival)}</b></div>
        <div><span>ПРЕДЫДУЩАЯ ОСТАНОВКА</span><b>${esc(vehicle.previous_stop || '—')}</b><small class="stop-time">Расчётное время · ${time(vehicle.previous_stop_time)}</small></div>
        <div><span>СЛЕДУЮЩАЯ ОСТАНОВКА</span><b>${esc(vehicle.trip_status === 'completed' ? 'Рейс завершён' : vehicle.next_stop || '—')}</b><small class="stop-time">Расчётное время · ${time(vehicle.next_stop_time)}</small></div>
        <div><span>ПОЗИЦИЯ ОБНОВЛЕНА · МСК</span><b>${time(vehicle.position_time ? new Date(vehicle.position_time * 1000).toISOString() : vehicle.T)}</b></div>
      </div>
      <details class="explanation-section" ${explanationOpen ? 'open' : ''}>
        <summary>Почему так и откуда данные</summary>
        <dl class="prediction-facts">
          <dt>Сигнал${vehicle.reason_is_hypothesis ? ' · гипотеза' : ''}</dt><dd>${esc(vehicle.reason || 'Недостаточно данных')}</dd>
          <dt>Телеметрия на момент расчёта</dt><dd>${esc(featureText(vehicle))}</dd>
          <dt>Отклонение по позиции</dt><dd>${delay(vehicle.current_deviation_s)} · оценка по плану</dd>
          <dt>Риск задержки &gt; 2 мин</dt><dd>${risk} · оценка по архивным ошибкам модели</dd>
          <dt>ИСТОЧНИК ПОЗИЦИИ</dt><dd>${esc(sourceText(vehicle))}</dd>
          <dt>Прогноз рассчитан · МСК</dt><dd>${time(vehicle.T)}${Number.isFinite(vehicle.horizon_s) ? ` · горизонт ${duration(vehicle.horizon_s)}` : ''}</dd>
        </dl>
      </details>

      <section class="action-section">
        <p class="eyebrow">РЕАГИРОВАНИЕ ДИСПЕТЧЕРА</p>
        <h3>Какое действие выбрать</h3>
        <div class="action-plan" id="action-plan">${initialActionPlan}</div>


        <label class="command-label" for="command-text">Сообщение водителю</label>
        <textarea
          class="command-text"
          id="command-text"
        >${esc(draft.message)}</textarea>


        <button
          class="send-command"
          id="send-command"
          ${draft.sending ? 'disabled' : ''}
        >
          Зарегистрировать указание
        </button>


        <div
          class="command-result"
          id="command-result"
          aria-live="polite"
        >${commandFeedbackHtml(vehicle.tr_id)}</div>

      </section>


      <details
        class="action-section command-history-section"
        ${
          commandHistoryOpen
            ? 'open'
            : ''
        }
      >

        <summary>
          Журнал указаний по рейсу
        </summary>

        <ul
          class="command-history"
          id="command-history"
        >${Array.isArray(supplement?.items) ? commandHistoryHtml(supplement.items) : '<li>Загрузка…</li>'}</ul>

      </details>

      ${debugSpeedControlHtml(vehicle)}`;

    // Keep the reaction node itself between frequent metric redraws. This
    // preserves hover/focus and prevents CSS transitions from restarting.
    if (previousActionPlan) $('action-plan').replaceWith(previousActionPlan);
    $('action-plan').dataset.cacheKey = supplementKey;

    /*
     * Состояние details обновляем
     * сразу по пользовательскому toggle.
     */
    const explanation =
      document.querySelector(
        '.explanation-section'
      );

    if (explanation) {
      explanation.addEventListener(
        'toggle',
        () => {
          explanationOpen =
            explanation.open;
        }
      );
    }


    const historySection =
      document.querySelector(
        '.command-history-section'
      );

    if (historySection) {
      historySection.addEventListener(
        'toggle',
        () => {
          commandHistoryOpen =
            historySection.open;
        }
      );
    }

    $('command-text').addEventListener(
      'input',
      event => {
        draft.message = event.target.value;
        draft.touched = true;
      }
    );

    if (activeCommand) {
      const input = $('command-text');
      input.focus();
      input.setSelectionRange(
        Math.min(activeCommand.start, input.value.length),
        Math.min(activeCommand.end, input.value.length)
      );
    }


    $('send-command').onclick =
      sendCommand;

    const debugSpeedInput = $('debug-speed-value');
    if (debugSpeedInput) {
      if (activeDebugSpeed) debugSpeedInput.focus({preventScroll: true});
      debugSpeedInput.oninput = event => {
        debugSpeedDraft = {trId: vehicle.tr_id, value: event.target.value};
      };
    }
    $('debug-speed-apply')?.addEventListener('click', () => setDebugSpeed(vehicle.tr_id));
    $('debug-speed-reset')?.addEventListener('click', () => clearDebugSpeed(vehicle.tr_id));

    const simulateButton = $('simulate-command');
    if (simulateButton?.dataset.commandId) {
      simulateButton.onclick = () => simulateCommand(simulateButton.dataset.commandId);
    }


    const actionPlan = $('action-plan');
    bindActionPlanOptions(actionPlan, draft);

    /*
     * Карточка получает быстрый кэш сразу, а ответ запроса применяем только
     * к тому же экземпляру карточки. Так старый ответ не может перерисовать
     * уже обновлённый или выбранный пользователем другой рейс.
     */
    loadDetailSupplement(vehicle.tr_id, profileId)
      .then(data => {
        if (renderVersion !== detailRenderVersion || selectedId !== vehicle.tr_id) return;

        const list = $('command-history');
        if (list) list.innerHTML = commandHistoryHtml(data.items || []);

        const plan = $('action-plan');
        if (plan && data.plan?.options?.length) {
          const previousAction = draft.action;
          const html = actionPlanHtml(data.plan, draft);
          if (plan.innerHTML !== html) plan.innerHTML = html;
          if (draft.action !== previousAction) {
            const commandInput = $('command-text');
            if (commandInput) commandInput.value = draft.message;
          }
          bindActionPlanOptions(plan, draft);
        } else if (plan) {
          plan.innerHTML = '<p class="action-unavailable">Не удалось сравнить пользу действий. Обновите данные рейса.</p>';
        }
      })
      .catch(() => {
        if (renderVersion !== detailRenderVersion || selectedId !== vehicle.tr_id) return;
        const list = $('command-history');
        if (list) list.innerHTML = '<li>Журнал временно недоступен.</li>';
        const plan = $('action-plan');
        if (plan) plan.innerHTML = '<p class="action-unavailable">Не удалось сравнить пользу действий. Обновите данные рейса.</p>';
      });
  }


  /* =======================================================
     SEND COMMAND
     ======================================================= */

  async function setDebugSpeed(vehicleId) {
    const input = $('debug-speed-value');
    const speed = Number(input?.value);
    if (!Number.isFinite(speed) || speed < 0 || speed > 130) {
      debugSpeedFeedback = {trId: vehicleId, message: 'Введите скорость от 0 до 130 км/ч.'};
      await renderDetail();
      return;
    }

    debugSpeedFeedback = {trId: vehicleId, message: 'Применяю debug-оверлей…'};
    try {
      const result = await api('/api/debug/custom-emulator/speed', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dispatcher_id: profileId, tr_id: vehicleId, speed_kmh: speed})
      });
      debugSpeedOverrides.set(vehicleId, {speed: result.speed_kmh});
      debugSpeedDraft = null;
      debugSpeedFeedback = {trId: vehicleId, message: `Активен debug-оверлей: ${result.speed_kmh} км/ч.`};
      await refresh();
    } catch (error) {
      debugSpeedFeedback = {trId: vehicleId, message: `Не удалось применить оверлей: ${error.message}`};
    }
    if (selectedId === vehicleId) await renderDetail();
  }

  async function clearDebugSpeed(vehicleId) {
    debugSpeedFeedback = {trId: vehicleId, message: 'Возвращаю штатный темп…'};
    try {
      await api(`/api/debug/custom-emulator/speed/${encodeURIComponent(vehicleId)}`, {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dispatcher_id: profileId})
      });
      debugSpeedOverrides.delete(vehicleId);
      debugSpeedDraft = null;
      debugSpeedFeedback = {trId: vehicleId, message: 'Возвращён штатный темп custom-emulator.'};
      await refresh();
    } catch (error) {
      debugSpeedFeedback = {trId: vehicleId, message: `Не удалось вернуть темп: ${error.message}`};
    }
    if (selectedId === vehicleId) await renderDetail();
  }

  async function sendCommand() {
    const vehicle =
      vehicles.find(
        item =>
          item.tr_id === selectedId
      );

    const draft = commandState(vehicle || {tr_id: null});
    const message = draft.message.trim();

    if (
      !vehicle ||
      !message
    ) {
      return;
    }

    draft.sending = true;
    commandFeedback = null;
    await renderDetail();

    try {
      const data =
        await api(
          '/api/driver-commands',
          {
            method: 'POST',

            headers: {
              'Content-Type':
                'application/json'
            },

            body: JSON.stringify({
              role:
                'dispatcher',

              dispatcher_id:
                profileId,

              tr_id:
                vehicle.tr_id,

              action:
                draft.action,

              message
            })
          }
        );

      /*
       * После отправки автоматически
       * раскрываем журнал.
       */
      commandHistoryOpen = true;

      const decision = data.decision || {};
      const commandMessage = decision.allowed
        ? `Указание зарегистрировано и прошло проверку. Следующая проверка: ${time(decision.next_check_at)}.`
        : `Указание сохранено как заблокированное: ${(decision.blockers || []).join('; ') || 'недостаточно данных'}.`;
      commandFeedback = decision.allowed
        ? {
            tr_id: vehicle.tr_id,
            kind: 'queued',
            message: commandMessage,
            commandId: data.id,
            simulatable: true,
          }
        : {
            tr_id: vehicle.tr_id,
            kind: 'blocked',
            message: commandMessage,
            detail: 'Указание сохранено в журнале и не отправлено водителю.',
            simulatable: false,
          };
      invalidateDetailSupplement(vehicle.tr_id);
    } catch (error) {
      commandFeedback = {
        tr_id: vehicle.tr_id,
        kind: 'error',
        message: `Не удалось зарегистрировать: ${error.message}`,
      };
    } finally {
      if (commandDraft?.trId === vehicle.tr_id) {
        draft.sending = false;
        await renderDetail();
      }
    }
  }

  async function simulateCommand(commandId) {
    const requestedVehicleId = selectedId;
    const button = $('simulate-command');
    if (button) button.disabled = true;
    try {
      const data = await api(`/api/driver-commands/${encodeURIComponent(commandId)}/simulate`, {method: 'POST'});
      commandHistoryOpen = true;
      const response = data.simulated_response || {};
      if (response.debug_speed) {
        debugSpeedOverrides.set(data.tr_id, {speed: response.debug_speed.speed_kmh});
        if (debugSpeedDraft?.trId === data.tr_id) debugSpeedDraft = null;
        debugSpeedFeedback = {trId: data.tr_id, message: `Демо-реакция водителя: debug-скорость ${response.debug_speed.speed_kmh} км/ч. Можно вернуть штатный темп.`};
      }
      const projection = response.projection || {};
      const next = response.next_step || {};
      commandFeedback = {
        tr_id: data.tr_id,
        commandId: data.id,
        kind: 'simulation',
        response: response.response,
        note: response.note || 'Локальная симуляция, live-контур не изменён.',
        reason: next.reason || '',
        checkAt: next.check_at,
        expectedSavedDelayS: projection.expected_saved_delay_s,
      };
      invalidateDetailSupplement(data.tr_id);
      await refresh();
    } catch (error) {
      if (button) button.disabled = false;
      commandFeedback = {
        tr_id: requestedVehicleId,
        kind: 'error',
        message: `Симуляция не выполнена: ${error.message}`,
      };
      const result = $('command-result');
      if (result && selectedId === requestedVehicleId) result.innerHTML = `<span class="reserve-blocked">Симуляция не выполнена: ${esc(error.message)}</span>`;
    }
  }


  /* =======================================================
     REFRESH
     ======================================================= */

  function refresh() {
    const requestedProfileId = profileId;

    if (refreshPromise) {
      if (refreshPromise.profileId === requestedProfileId) {
        return refreshPromise.promise;
      }
      return refreshPromise.promise
        .catch(() => undefined)
        .then(() => refresh());
    }

    const promise = refreshForProfile(requestedProfileId)
      .finally(() => {
        if (refreshPromise?.promise === promise) refreshPromise = null;
      });
    refreshPromise = {profileId: requestedProfileId, promise};
    return promise;
  }

  async function refreshForProfile(requestedProfileId) {
    try {
      const [
        state,
        network,
        actionData
      ] =
        await Promise.all([
          api(
            `/api/state?dispatcher_id=${encodeURIComponent(
              requestedProfileId
            )}`
          ),

          networkCache && Date.now() - networkLoadedAt < 60000
            ? Promise.resolve(networkCache)
            : api('/api/network').then(network => {
                networkCache = network;
                networkLoadedAt = Date.now();
                return network;
              }),

          api(
            `/api/action-center?dispatcher_id=${encodeURIComponent(
              requestedProfileId
            )}`
          ).catch(() => ({ items: [] }))
        ]);

      /* Пользователь мог успеть сменить профиль во время запроса. */
      if (profileId !== requestedProfileId) return;

      actionCenter = actionData.items || [];
      actionCenterSummary = actionData.summary || {};

      const allVehicles = state.vehicles || [];
      const liveMode = state.state?.mode === 'live';
      archivedVehicles = liveMode
        ? allVehicles.filter(vehicle => vehicle.source === 'historical_fallback' || vehicle.connection_state === 'historical')
        : [];
      baseVehicles = liveMode
        ? allVehicles.filter(vehicle => !archivedVehicles.includes(vehicle))
        : allVehicles;

      vehicles =
        reserveScenario
          ? [
              ...baseVehicles,
              reserveScenario.vehicle
            ]
          : [
              ...baseVehicles
            ];

      paths =
        network.paths || [];

      render(state, { refreshDetail: false });
      if ($('drawer')?.classList.contains('open')) {
        await renderDetail();
      }

    } catch (error) {
      if (profileId !== requestedProfileId) return;
      $('attention-list').innerHTML =
        `<p class="empty">
          Не удалось загрузить данные:
          ${esc(error.message)}
        </p>`;
    }
  }

  function startRefreshLoop() {
    if (pollingActive) return;
    pollingActive = true;
    const generation = ++pollingGeneration;

    const schedule = () => {
      if (!pollingActive || generation !== pollingGeneration) return;
      refreshTimer = window.setTimeout(async () => {
        refreshTimer = null;
        await refresh();
        schedule();
      }, REFRESH_INTERVAL_MS);
    };

    schedule();
  }

  function stopRefreshLoop() {
    pollingActive = false;
    pollingGeneration += 1;
    if (refreshTimer !== null) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
  }


  /* =======================================================
     EVENTS
     ======================================================= */

  applyTheme(
    document.documentElement.classList.contains('theme-dark')
      ? 'dark'
      : 'light'
  );
  $('theme-toggle')?.addEventListener('click', toggleTheme);

  window.TransitMap.mount(
    id =>
      openVehicle(id)
  );


  if ($('map-fit')) {
    $('map-fit').onclick =
      () =>
        window.TransitMap.fit();
  }


  $('search').oninput =
    renderTable;


  $('reserve-run').onclick =
    runReserveScenario;

  function selectReserveVehicle(event) {
    const vehicleId = Number(event.target.value);
    if (baseVehicles.some(vehicle => vehicle.tr_id === vehicleId)) openVehicle(vehicleId);
  }
  $('reserve-route')?.addEventListener('change', selectReserveVehicle);

  $('source-control-open')?.addEventListener('click', async () => {
    $('source-dialog')?.showModal();
    await refreshSourceStatus();
  });

  $('source-close')?.addEventListener('click', () => $('source-dialog')?.close());
  function bindSourceDialogDismiss(dialog) {
    if (!dialog) return;
    let backdropPressed = false;
    const outside = event => {
      const bounds = dialog.getBoundingClientRect();
      return event.target === dialog &&
        (event.clientX < bounds.left || event.clientX > bounds.right ||
         event.clientY < bounds.top || event.clientY > bounds.bottom);
    };
    dialog.addEventListener('pointerdown', event => { backdropPressed = outside(event); });
    dialog.addEventListener('pointerup', event => {
      if (backdropPressed && outside(event)) dialog.close();
      backdropPressed = false;
    });
    dialog.addEventListener('pointercancel', () => { backdropPressed = false; });
    dialog.addEventListener('close', () => { backdropPressed = false; });
  }
  bindSourceDialogDismiss($('source-dialog'));
  $('source-pause-all')?.addEventListener('click', () => controlSources('pause'));
  $('source-resume-all')?.addEventListener('click', () => controlSources('resume'));


  document.addEventListener(
    'click',
    event => {
      if (
        event.target.closest(
          '[data-close-drawer]'
        )
      ) {
        closeDrawer();
      }
    }
  );


  /*
   * ESC закрывает карточку.
   */
  document.addEventListener(
    'keydown',
    event => {
      if (
        event.key === 'Escape' &&
        $('drawer')
          .classList
          .contains('open') &&
        !$('profile-dialog').open
      ) {
        closeDrawer();
      }
    }
  );


  const profileDialog = $('profile-dialog');
  const resetProfileChoice = () => {
    $('profile-select').value = profileId || profiles[0]?.id || '';
  };
  const dismissProfile = () => {
    resetProfileChoice();
    profileDialog.close('cancel');
  };
  $('profile-open').onclick = () => {
    resetProfileChoice();
    profileDialog.showModal();
  };
  $('profile-close').onclick = dismissProfile;
  $('profile-cancel').onclick = dismissProfile;
  profileDialog.addEventListener('cancel', event => {
    event.preventDefault();
    dismissProfile();
  });
  profileDialog.addEventListener('close', resetProfileChoice);
  const outsideProfile = event => {
    const bounds = profileDialog.getBoundingClientRect();
    return event.target === profileDialog &&
      (event.clientX < bounds.left || event.clientX > bounds.right ||
       event.clientY < bounds.top || event.clientY > bounds.bottom);
  };
  let backdropPressed = false;
  profileDialog.addEventListener('pointerdown', event => {
    backdropPressed = outsideProfile(event);
  });
  profileDialog.addEventListener('pointerup', event => {
    if (backdropPressed && outsideProfile(event)) dismissProfile();
    backdropPressed = false;
  });
  profileDialog.addEventListener('pointercancel', () => { backdropPressed = false; });


  $('profile-save').onclick =
    async event => {
      event.preventDefault();

      const selectedProfile = profiles.find(profile => profile.id === $('profile-select').value);
      if (selectedProfile?.role === 'Администратор') {
        const previous = profiles.find(profile => profile.id === profileId && profile.role !== 'Администратор');
        if (previous) localStorage.setItem(PREVIOUS_PROFILE_KEY, previous.id);
      }
      profileId = $('profile-select').value;

      /* Новый профиль не должен делить цикл со старым. */
      stopRefreshLoop();

      updateProfile();
      sessionStorage.setItem(AUTH_KEY, '1');

      $('profile-dialog')
        .close();

      if (currentProfile()?.role === 'Администратор') {
        window.open('/admin', '_blank', 'noopener,noreferrer');
        return;
      }

      /*
       * При смене диспетчера
       * закрываем карточку,
       * потому что набор ТС может измениться.
       */
      closeDrawer();

      selectedId = null;

      explanationOpen = false;
      commandHistoryOpen = false;

      await refresh();
      startRefreshLoop();
    };


  /* =======================================================
     START
     ======================================================= */

  (
    async () => {
      await loadProfiles();
      if (!isAuthenticated()) {
        $('profile-dialog').showModal();
        return;
      }
      await refresh();
      startRefreshLoop();
    }
  )().catch(
    error =>
      console.error(error)
  );

})();
