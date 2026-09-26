(() => {
  'use strict';

  const $ = id => document.getElementById(id);

  const PROFILE_KEY = 'takt-dispatcher-profile';
  const PREVIOUS_PROFILE_KEY = 'takt-dispatcher-previous-profile';
  const AUTH_KEY = 'takt-dispatcher-auth';

  const labels = {
    low: 'В графике',
    medium: 'Нужно проверить',
    high: 'Риск опоздания',
    unknown: 'Нет оценки'
  };

  const actionText = {
    contact:
      'Просьба подтвердить текущую обстановку, причину отклонения и возможность следовать графику.',

    accelerate_safely:
      'При возможности сократите отставание без нарушения ПДД, скоростного режима и требований безопасности.',

    maintain:
      'Подтвердите возможность выдерживать интервал движения и следовать графику безопасно.'
  };

  let vehicles = [];
  let baseVehicles = [];
  let archivedVehicles = [];
  let paths = [];

  let selectedId = null;

  let profiles = [];
  let profileId =
    localStorage.getItem(PROFILE_KEY) || '';

  const isAuthenticated = () =>
    sessionStorage.getItem(AUTH_KEY) === '1' && Boolean(profileId);

  let reserveScenario = null;
  let lastRuntime = {};

  /*
   * Состояние раскрытых блоков карточки.
   * renderDetail() пересоздаёт HTML каждые 5 секунд,
   * поэтому эти значения храним отдельно.
   */
  let explanationOpen = false;
  let commandHistoryOpen = false;


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
            minute: '2-digit'
          }
        )
      : '—';


  const delay = value =>
    value == null
      ? '—'
      : `${value >= 0 ? '+' : '−'}${Math.round(
          Math.abs(value)
        )} с`;


  const predictionText = vehicle => {
    if (vehicle.prediction_s == null) {
      return 'Прогноз пока недоступен';
    }

    if (vehicle.prediction_s >= 0) {
      return `Ожидается опоздание на ${Math.round(
        vehicle.prediction_s
      )} с`;
    }

    return `Ожидается прибытие раньше плана на ${Math.abs(
      Math.round(vehicle.prediction_s)
    )} с`;
  };


  const riskText = vehicle => {
    if (vehicle.late_probability == null) {
      return (
        'Уровень внимания не рассчитан: ' +
        'не хватает подтверждённых свежих данных.'
      );
    }

    return (
      `${Math.round(
        vehicle.late_probability * 100
      )}% — расчётная вероятность опоздать ` +
      'более чем на 120 с. ' +
      'Это индикатор для проверки, а не подтверждённая причина.'
    );
  };


  const sourceText = vehicle => {
    if (vehicle.scenario) {
      return (
        'Виртуальное резервное ТС: ' +
        'позиция добавлена только для оценки сценария ' +
        'и не отправляется в live-контур.'
      );
    }

    if (vehicle.source === 'waiting_for_live') {
      const label = vehicle.telemetry_source === 'custom_ndtp_nav00'
        ? 'custom-emulator'
        : 'Оригинальный NDTP';
      return `${label} настроен, но первый пакет ещё не пришёл; точка на карте — начало плановой траектории, прогноз не рассчитывается.`;
    }

    const source =
      vehicle.telemetry_source === 'custom_ndtp_nav00'
        ? 'custom-emulator → NDTP Nav00 → CRC → backend'
        : vehicle.telemetry_source === 'ndtp_nav00'
          ? 'Оригинальный NDTP → Nav00 → CRC → backend'
        : vehicle.telemetry_source === 'http_json'
          ? 'HTTP JSON → backend'
          : 'живая телеметрия → backend';
    const positionNote = vehicle.position_adjusted
      ? ' Позиция оригинального NDTP приведена к плановой траектории: внешний образ генерирует синтетический GPS без маршрута, исходная точка сохраняется как raw для аудита и не используется для карты.'
      : '';

    if (vehicle.live_position) {
      return (
        `Свежая позиция: ${source}${positionNote} ` +
        `(${time(vehicle.live_position_time)}). ` +
        'Прогноз отдельно взят из архивного V5 fallback.'
      );
    }

    if (vehicle.source === 'live') {
      return (
        `${source}${positionNote} → расписание → ` +
        '102 causal-признака → V5.'
      );
    }

    return (
      'Архивная телеметрия (Архивный fallback): validate-телеметрия → ' +
      'расписание → сохранённый V5.'
    );
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
      options
    );

    const data = await response
      .json()
      .catch(() => ({
        detail: 'Ошибка ответа'
      }));

    if (!response.ok) {
      throw Error(
        data.detail ||
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
      const sourceStatus = status => ({running: 'Работает', paused: 'Пауза', unavailable: 'Недоступен', not_configured: 'Не настроен'}[status] || status || 'Неизвестно');
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
      reserveScenario?.routeId ||
      select.value;

    const sorted = [
      ...baseVehicles
    ].sort(
      (a, b) =>
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

    // The reserve is placed on the selected vehicle's current position so it
    // remains on the same planned line instead of appearing as an arbitrary
    // offset marker beside the map geometry.
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

      // What-if uses the backend's bounded placement estimate; a risk
      // probability delta must not be presented as seconds of delay.
      prediction_s:
        placement?.after_prediction_s ??
        route.prediction_s ??
        null,

      current_deviation_s:
        placement?.after_current_deviation_s ??
        route.current_deviation_s ??
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
            : before * 0.85;

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
      result.innerHTML =
        `<b>Резерв размещён на плановом сегменте</b>
         <span>
           Основное ТС ${routeId}: риск ${change}; прогноз ${delay(placement.before_prediction_s)} → ${delay(placement.after_prediction_s)}.
           Следующая точка: ${esc(placement.next_stop_address || 'не определена')}.
           Пунктир показывает путь резерва до цели.
         </span>
         <button
           type="button"
           class="link-button"
           id="reserve-clear"
         >
           Убрать сценарий
         </button>`;

      $('reserve-clear').onclick =
        clearReserveScenario;

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
    runtime = {}
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
      vehicles.length;

    $('kpi-attention').textContent =
      baseVehicles.filter(
        vehicle =>
          vehicle.level === 'high' ||
          vehicle.level === 'medium'
      ).length;

    $('kpi-predictions').textContent =
      liveForecasts ||
      counters.predictions ||
      0;

    $('kpi-latency').textContent =
      counters.last_inference_ms ==
      null
        ? (
            live
              ? 'ожидание свежих данных'
              : 'архивный снимок'
          )
        : (
            'последний расчёт ' +
            `${Math.round(
              counters
                .last_inference_ms
            )} мс`
          );


    /* stream status */

    $('stream-title').textContent =
      live
        ? (
            counters.ndtp_packets
              ? 'NDTP-поток активен'
              : 'Ожидание NDTP-потока'
          )
        : 'Исторический replay (архив)';
    $('source-control-open').textContent = live ? 'Источники данных · live' : 'Источники данных · архив';

    $('archive-time').textContent =
      live
        ? (
            `${counters.ndtp_packets || 0} пакетов · ` +
            `${liveForecasts} live-прогнозов` +
            (archivedVehicles.length ? ` · ${archivedVehicles.length} архивных без live скрыто` : '')
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
    renderTable();

    window.TransitMap.render(
      vehicles,
      paths,
      selectedId
    );

    /*
     * Если drawer открыт,
     * обновляем данные внутри карточки,
     * но сохраняем состояние details.
     */
    if (
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
      baseVehicles
        .filter(
          vehicle =>
            vehicle.level === 'high' ||
            vehicle.level === 'medium'
        )
        .concat(
          baseVehicles.filter(
            vehicle =>
              vehicle.level === 'low'
          )
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
                      )}
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
                        labels[
                          vehicle.level
                        ]
                      }
                    </span>
                  </span>
                </button>`
            )
            .join('')
        : (
            '<p class="empty">' +
            'Нет рейсов для отображения' +
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
                          : labels[
                              vehicle.level
                            ]
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
                    <small class="table-current">
                      сейчас: ${vehicle.current_deviation_s == null ? 'нет оценки' : delay(vehicle.current_deviation_s)}
                    </small>
                  </td>

                  <td>
                    <b>${esc(vehicle.stop_address)}</b>
                    <small class="table-current">на карте: ${esc(vehicle.position_match?.next_stop_address || 'нет сопоставления')}</small>
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
      vehicles,
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


  /* =======================================================
     DETAIL
     ======================================================= */

  async function renderDetail() {
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
            Риск ${change}
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

    const risk =
      vehicle.late_probability ==
      null
        ? 'Не рассчитан'
        : `${Math.round(
            vehicle
              .late_probability *
            100
          )}%`;


    $('vehicle-detail').innerHTML =
      `<div class="vehicle-title">

        <p class="eyebrow">
          КАРТОЧКА РЕЙСА
        </p>

        <h2>
          ТС ${vehicle.tr_id}
        </h2>

        <p>
          ${esc(
            vehicle.stop_address
          )}
        </p>

      </div>


      <div class="detail-status">

        <span
          class="tag ${vehicle.level}"
        >
          ${
            labels[
              vehicle.level
            ]
          }
        </span>

        <b>
          ${delay(
            vehicle.prediction_s
          )}
        </b>

      </div>


      <section class="prediction-panel">

        <p class="eyebrow">
          ПРОГНОЗ НА КОНТРОЛЬНОЙ ТОЧКЕ
        </p>

        <h3>
          ${esc(
            predictionText(
              vehicle
            )
          )}
        </h3>

        <p>
          Плановая точка:
          <b>
            ${esc(
              vehicle.stop_address ||
              'не определена'
            )}
          </b>
          ·
          ${time(
            vehicle
              .target_time_begin
          )}
        </p>

        <p class="interpretation-note">
          «Сейчас» и прогноз относятся к разным моментам: текущее отклонение
          считается по позиции на сегменте, прогноз — к этой точке через
          10–15 минут.
        </p>

        ${vehicle.prediction_adjusted ? `
          <p class="interpretation-note warning-note">
            Прогноз ограничен по допустимой скорости восстановления: исходная
            модель обещала слишком резкую коррекцию за этот горизонт.
          </p>
        ` : ''}

      </section>


      <div class="detail-grid">

        <div>
          <span>
            ТЕКУЩЕЕ ОТКЛОНЕНИЕ
          </span>

          <b>
            ${vehicle.current_deviation_s == null
              ? '—'
              : `${delay(vehicle.current_deviation_s)} · ${vehicle.deviation_estimated ? 'по положению на маршруте' : 'у медленной точки'}`}
          </b>
        </div>

        <div>
          <span>
            ПРЕДЫДУЩАЯ ОСТАНОВКА
          </span>

          <b>
            ${esc(
              vehicle
                .previous_stop ||
              '—'
            )}
          </b>
          <small class="stop-time">
            расчётное прохождение: ${time(vehicle.previous_stop_time)}
          </small>
        </div>


        <div>
          <span>
            БЛИЖАЙШАЯ ПЛАНОВАЯ ОСТАНОВКА
          </span>

          <b>
            ${esc(
              vehicle.position_match?.next_stop_address ||
              vehicle.next_stop ||
              '—'
            )}
          </b>
          <small class="stop-time">
            расчётное прибытие: ${time(vehicle.next_stop_time)}
          </small>
        </div>


        <div>
          <span>
            ТЕКУЩИЙ УЧАСТОК
          </span>

          <b>
            ${esc(
              vehicle.position_match?.segment_start_stop_address ||
              vehicle.previous_stop ||
              '—'
            )}
            →
            ${esc(
              vehicle.position_match?.next_stop_address ||
              vehicle.next_stop ||
              vehicle.stop_address ||
              '—'
            )}
          </b>
        </div>


        <div>
          <span>
            ПОЗИЦИЯ
          </span>

          <b>
            ${
              time(
                vehicle.position_time
                  ? new Date(
                      vehicle
                        .position_time *
                      1000
                    ).toISOString()
                  : vehicle.T
              )
            }

            ${
              vehicle.live_position
                ? (
                    ' · LIVE ' +
                    time(
                      vehicle
                        .live_position_time
                    )
                  )
                : ''
            }
          </b>
        </div>

        <div>
          <span>
            ИСТОЧНИК ПОЗИЦИИ
          </span>

          <b>
            ${esc(
              vehicle.telemetry_source === 'custom_ndtp_nav00'
                ? 'custom-emulator'
                : vehicle.telemetry_source === 'ndtp_nav00'
                  ? 'Оригинальный NDTP (Live NDTP)'
                  : vehicle.live_position
                    ? 'HTTP / live-телеметрия'
                    : 'Архивная телеметрия'
            )}
          </b>
        </div>

      </div>


      <details
        class="explanation-section"
        ${
          explanationOpen
            ? 'open'
            : ''
        }
      >

        <summary>
          Почему так и откуда данные
        </summary>

        <p>
          <b>
            ${risk}
          </b>.
          ${esc(
            riskText(vehicle)
          )}
        </p>

        <p>
          <b>
            Сигнал для проверки:
          </b>

          ${esc(
            vehicle.reason ||
            'Нет дополнительного сигнала'
          )}

          <span class="hypothesis">${esc(vehicle.reason_explanation || (vehicle.reason_is_hypothesis ? 'Это не установленная причина: V5 видит устойчивый паттерн в телеметрии, но подтверждающих событий (например, причина простоя или команда водителя) в прототипе нет.' : 'Сигнал подтверждён потоком данных.'))}</span>
        </p>

        <p>
          <b>
            Признаки:
          </b>

          ${esc(
            featureText(vehicle)
          )}.
        </p>

        <p>
          <b>
            Источник:
          </b>

          ${esc(
            sourceText(vehicle)
          )}
        </p>

      </details>


      <section class="action-section">

        <h3>
          Действие диспетчера
        </h3>

        <div class="action-buttons">

          <button
            type="button"
            data-action="contact"
          >
            Связаться и уточнить
          </button>

          <button
            type="button"
            data-action="accelerate_safely"
          >
            Предложить безопасно
            сократить отставание
          </button>

          <button
            type="button"
            data-action="maintain"
          >
            Согласовать выдерживание
            интервала
          </button>

        </div>


        <textarea
          class="command-text"
          id="command-text"
        >${esc(
          actionText.contact
        )}</textarea>


        <button
          class="send-command"
          id="send-command"
          data-action="contact"
        >
          Зарегистрировать указание
        </button>


        <div
          class="command-result"
          id="command-result"
        ></div>

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
        >
          <li>
            Загрузка…
          </li>
        </ul>

      </details>`;


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


    /* command buttons */

    document
      .querySelectorAll(
        '[data-action]'
      )
      .forEach(
        button => {
          button.onclick = () => {
            $('command-text').value =
              actionText[
                button.dataset.action
              ];

            $('send-command')
              .dataset
              .action =
              button.dataset.action;

            $('send-command')
              .textContent =
              button.textContent
                .trim();
          };
        }
      );


    $('send-command').onclick =
      sendCommand;


    /*
     * Загружаем журнал.
     * Проверяем selectedId после await,
     * чтобы не записать журнал старого ТС,
     * если пользователь успел выбрать другое.
     */
    const requestedVehicleId =
      vehicle.tr_id;

    try {
      const items =
        await commandHistory(
          requestedVehicleId
        );

      if (
        selectedId !==
        requestedVehicleId
      ) {
        return;
      }

      const list =
        $('command-history');

      if (!list) return;

      list.innerHTML =
        items.length
          ? items
              .map(
                item =>
                  `<li>
                    <b>
                      ${esc(
                        item.dispatcher
                          ?.name ||
                        'Диспетчер'
                      )}
                    </b>:
                    ${esc(
                      item.action_title
                    )}
                    <small>
                      ${time(
                        item.created_at
                      )}
                    </small>
                  </li>`
              )
              .join('')
          : (
              '<li>' +
              'Указаний пока нет.' +
              '</li>'
            );

    } catch (error) {
      if (
        selectedId !==
        requestedVehicleId
      ) {
        return;
      }

      const list =
        $('command-history');

      if (list) {
        list.innerHTML =
          '<li>' +
          'Журнал временно недоступен.' +
          '</li>';
      }
    }
  }


  /* =======================================================
     SEND COMMAND
     ======================================================= */

  async function sendCommand() {
    const vehicle =
      vehicles.find(
        item =>
          item.tr_id === selectedId
      );

    const button =
      $('send-command');

    const message =
      $('command-text')
        .value
        .trim();

    if (
      !vehicle ||
      !message
    ) {
      return;
    }

    button.disabled = true;

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
                button
                  .dataset
                  .action,

              message
            })
          }
        );

      /*
       * После отправки автоматически
       * раскрываем журнал.
       */
      commandHistoryOpen = true;

      const result =
        $('command-result');

      if (result) {
        result.textContent =
          `Указание зарегистрировано: ` +
          `${data.action_title}.`;
      }

      await renderDetail();

    } catch (error) {
      const result =
        $('command-result');

      if (result) {
        result.textContent =
          `Не удалось зарегистрировать: ` +
          `${error.message}`;
      }

    } finally {
      button.disabled = false;
    }
  }


  /* =======================================================
     REFRESH
     ======================================================= */

  async function refresh() {
    try {
      const [
        state,
        network
      ] =
        await Promise.all([
          api(
            `/api/state?dispatcher_id=${encodeURIComponent(
              profileId
            )}`
          ),

          api('/api/network')
        ]);

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

      render(state);

    } catch (error) {
      $('attention-list').innerHTML =
        `<p class="empty">
          Не удалось загрузить данные:
          ${esc(error.message)}
        </p>`;
    }
  }


  /* =======================================================
     EVENTS
     ======================================================= */

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

  $('source-control-open')?.addEventListener('click', async () => {
    $('source-dialog')?.showModal();
    await refreshSourceStatus();
  });

  $('source-close')?.addEventListener('click', () => $('source-dialog')?.close());
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


  $('profile-open').onclick =
    () =>
      $('profile-dialog')
        .showModal();


  $('profile-close').onclick =
    () => {
      if (isAuthenticated()) $('profile-dialog').close();
    };

  $('profile-cancel').onclick =
    () => {
      if (isAuthenticated()) $('profile-dialog').close();
    };


  $('profile-save').onclick =
    event => {
      event.preventDefault();

      const selectedProfile = profiles.find(profile => profile.id === $('profile-select').value);
      if (selectedProfile?.role === 'Администратор') {
        const previous = profiles.find(profile => profile.id === profileId && profile.role !== 'Администратор');
        if (previous) localStorage.setItem(PREVIOUS_PROFILE_KEY, previous.id);
      }
      profileId = $('profile-select').value;

      updateProfile();
      sessionStorage.setItem(AUTH_KEY, '1');

      $('profile-dialog')
        .close();

      if (currentProfile()?.role === 'Администратор') {
        window.location.href = '/admin';
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

      refresh();
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

      setInterval(
        refresh,
        2000
      );
    }
  )().catch(
    error =>
      console.error(error)
  );

})();
