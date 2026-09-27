/* Shared Leaflet integration for the dispatcher workspace. */

(() => {
  'use strict';


  /* =======================================================
     COLORS
     ======================================================= */

  const colors = {
    low: '#198038',
    medium: '#b28600',
    high: '#da1e28',
    unknown: '#6f6f6f'
  };


  /* =======================================================
     STATE
     ======================================================= */

  const markers = new Map();
  const routes = new Map();
  const traces = new Map();

  const markerAnimations =
    new Map();

  const LIVE_MOVE_DURATION_MS =
    900;

  let map = null;

  let vehicles = [];
  let selection = null;

  let network = null;
  let bounds = null;

  let initialFit = false;

  /*
   * true:
   * карта автоматически центрируется
   * на выбранном ТС после каждого обновления.
   */
  let followSelection = false;

  let onChoose = () => {};

  let failedTiles = 0;
  let loadedTiles = 0;


  /* =======================================================
     HELPERS
     ======================================================= */

  const escape = value =>
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


  const valid = value =>
    Number.isFinite(
      Number(value.lon)
    ) &&
    Number.isFinite(
      Number(value.lat)
    ) &&
    Math.abs(
      Number(value.lon)
    ) <= 180 &&
    Math.abs(
      Number(value.lat)
    ) <= 85.0511;

  function animateMarker(
    marker,
    vehicleId,
    targetLat,
    targetLon
  ) {
    const previousTarget =
      marker.options.animationTarget;

    if (
      previousTarget &&
      previousTarget.lat === targetLat &&
      previousTarget.lon === targetLon
    ) {
      return;
    }

    marker.options.animationTarget = {
      lat: targetLat,
      lon: targetLon
    };

    const previousAnimation =
      markerAnimations.get(
        vehicleId
      );

    if (previousAnimation) {
      cancelAnimationFrame(
        previousAnimation
      );
    }

    const start =
      marker.getLatLng();

    const startLat =
      start.lat;

    const startLon =
      start.lng;

    const deltaLat =
      targetLat - startLat;

    const deltaLon =
      targetLon - startLon;

    if (
      Math.abs(deltaLat) < 1e-10 &&
      Math.abs(deltaLon) < 1e-10
    ) {
      markerAnimations.delete(
        vehicleId
      );

      return;
    }

    const startedAt =
      performance.now();

    const frame = now => {
      const progress =
        Math.min(
          1,
          (
            now -
            startedAt
          ) /
          LIVE_MOVE_DURATION_MS
        );

      marker.setLatLng([
        startLat +
          deltaLat * progress,

        startLon +
          deltaLon * progress
      ]);

      if (
        progress < 1
      ) {
        const frameId =
          requestAnimationFrame(
            frame
          );

        markerAnimations.set(
          vehicleId,
          frameId
        );

        return;
      }

      markerAnimations.delete(
        vehicleId
      );
    };

    const frameId =
      requestAnimationFrame(
        frame
      );

    markerAnimations.set(
      vehicleId,
      frameId
    );
  }

  const delay = value =>
    Number.isFinite(value)
      ? `${value < 0 ? '−' : '+'}${Math.abs(
          Math.round(value)
        )} с`
      : '—';


  const arrival = value =>
    value
      ? new Date(value)
          .toLocaleString(
            'ru-RU',
            {
              timeZone:
                'Europe/Moscow',

              day:
                'numeric',

              month:
                'short',

              hour:
                '2-digit',

              minute:
                '2-digit'
            }
          )
      : '—';


  /* =======================================================
     MAP NOTICE
     ======================================================= */

  function notify(
    message = ''
  ) {
    const node =
      document.getElementById(
        'map-tile-notice'
      );

    if (!node) return;

    node.textContent =
      message;

    node.hidden =
      !message;
  }


  /* =======================================================
     POPUP CONTENT
     ======================================================= */

  function content(vehicle) {
    if (vehicle.scenario) {
      return `
        <h3>
          Резервное ТС · what-if
        </h3>

        <dl>
          <dt>
            Линия
          </dt>

          <dd>
            ТС ${escape(
              vehicle.route_id
            )}
          </dd>

          <dt>
            Прогноз риска
          </dt>

          <dd class="popup-prediction">
            ${escape(
              vehicle.recommendation ||
              'Сценарий'
            )}
          </dd>

          <dt>
            Путь резерва
          </dt>

          <dd>
            ${escape(vehicle.scenario_placement?.start_stop_address || 'Текущая позиция')}
            →
            ${escape(vehicle.scenario_placement?.target_stop_address || vehicle.stop_address || 'целевая остановка')}
          </dd>
        </dl>

        <p class="popup-caution">
          Виртуальная позиция
          для оценки,
          не live-телеметрия.
        </p>
      `;
    }


    const rawStop =
      vehicle.stop_address;

    const stop =
      !rawStop ||
      [
        'nan',
        'null',
        'none'
      ].includes(
        String(rawStop)
          .toLowerCase()
      )
        ? 'Название не указано'
        : rawStop;


    return `
      <h3>
        ТС ${escape(
          vehicle.tr_id
        )}
      </h3>

      <dl>

        <dt>
          Текущее отклонение
        </dt>

        <dd>
          ${delay(
            vehicle.current_deviation_s
          )}
        </dd>

        <dt>
          Прогноз отклонения
        </dt>

        <dd class="popup-prediction">
          ${delay(
            vehicle.prediction_s
          )}
        </dd>


        <dt>
          Целевая остановка
        </dt>

        <dd>
          ${escape(stop)}
        </dd>


        <dt>
          Плановое прибытие · МСК
        </dt>

        <dd>
          ${arrival(
            vehicle.target_time_begin
          )}
        </dd>

        <dt>
          Ближайшая точка на карте
        </dt>

        <dd>
          ${escape(vehicle.position_match?.next_stop_address || '—')}
        </dd>

      </dl>


      ${
        vehicle.stale ||
        vehicle.prediction_s == null ||
        vehicle.level === 'unknown'
          ? `
            <p class="popup-caution">
              Нет свежего прогноза.
              Проверьте время данных.
            </p>
          `
          : ''
      }
    `;
  }


  /* =======================================================
     MARKER ICON
     ======================================================= */

  function icon(vehicle) {
    return L.divIcon({
      className:
        `transit-marker${
          vehicle.scenario
            ? ' scenario-marker'
            : ''
        }`,

      iconSize:
        [32, 32],

      iconAnchor:
        [16, 16],

      popupAnchor:
        [0, -19],

      html: `
        <span
          class="transit-marker-icon"
          style="--vehicle-color:${
            colors[
              vehicle.level
            ] ||
            colors.unknown
          }"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
          >

            <rect
              x="5"
              y="3"
              width="14"
              height="16"
              rx="3"
              stroke="currentColor"
              stroke-width="1.5"
            />

            <path
              d="
                M5 12h14
                M8 19v2
                m8-2v2
                M9 6h6
              "
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
            />

            <circle
              cx="8.5"
              cy="15.5"
              r="1"
              fill="currentColor"
            />

            <circle
              cx="15.5"
              cy="15.5"
              r="1"
              fill="currentColor"
            />

          </svg>
        </span>
      `
    });
  }


  /* =======================================================
     FOLLOW BUTTON
     ======================================================= */

  function updateFollowButton() {
    const button =
      document.getElementById(
        'map-follow'
      );

    if (!button) {
      return;
    }

    /*
     * Без выбранного ТС
     * кнопка не нужна.
     */
    if (
      selection == null
    ) {
      button
        .classList
        .add('is-hidden');

      return;
    }

    button
      .classList
      .remove('is-hidden');


    if (followSelection) {
      button.textContent =
        'Не следить';

      button.setAttribute(
        'aria-pressed',
        'true'
      );
    } else {
      button.textContent =
        'Следить за ТС';

      button.setAttribute(
        'aria-pressed',
        'false'
      );
    }
  }


  function followSelectedVehicle(
    animate = true
  ) {
    if (
      !map ||
      !followSelection ||
      selection == null
    ) {
      return;
    }

    const marker =
      markers.get(selection);

    if (!marker) {
      return;
    }

    const position =
      marker.getLatLng();

    /*
     * panTo не меняет zoom.
     * Благодаря этому диспетчер
     * сохраняет выбранный масштаб,
     * а карта только движется вслед за ТС.
     */
    map.panTo(
      position,
      {
        animate,
        duration:
          animate
            ? 0.7
            : 0
      }
    );
  }


  function setFollow(
    enabled
  ) {
    followSelection =
      Boolean(enabled);

    updateFollowButton();

    if (followSelection) {
      followSelectedVehicle(
        true
      );
    }
  }


  /* =======================================================
     ROUTE STYLES
     ======================================================= */

  function styleRoutes() {
    // Legacy optional control is intentionally absent from the compact dashboard.
    // getElementById('map-all-routes')?.checked remains a supported integration hook.
    const activeIds =
      new Set(
        vehicles.map(
          vehicle =>
            vehicle.tr_id
        )
      );

    const showAll =
      document
        .getElementById(
          'map-all-routes'
        )
        ?.checked ??
      false;

    const byRoute =
      new Map(
        vehicles.map(
          vehicle => [
            vehicle.tr_id,
            vehicle
          ]
        )
      );


    for (
      const [
        id,
        line
      ] of routes
    ) {
      const selected =
        id === selection;

      const visible =
        showAll ||
        selected ||
        activeIds.has(id) ||
        !vehicles.length;

      const level =
        byRoute.get(id)
          ?.level ||
        'unknown';


      line.setStyle({
        color:
          colors[level] ||
          colors.unknown,

        weight:
          selected
            ? 5
            : 3,

        opacity:
          visible
            ? (
                selected
                  ? 0.95
                  : 0.7
              )
            : 0
      });


      if (selected) {
        line.bringToFront();
      }
    }


    for (
      const [
        id,
        marker
      ] of markers
    ) {
      marker
        .getElement()
        ?.classList
        .toggle(
          'selected',
          id === selection
        );
    }
  }


  /* =======================================================
     LIVE TRACKS
     ======================================================= */

  function drawTracks() {
    const visible =
      new Set();


    for (
      const vehicle
      of vehicles
    ) {
      const track =
        (
          vehicle.live_track ||
          []
        ).filter(
          point =>
            valid(point)
        );


      if (
        track.length < 2
      ) {
        continue;
      }


      visible.add(
        vehicle.tr_id
      );


      const live =
        track
          .filter(
            point =>
              !point.simulated
          )
          .map(
            point => [
              Number(point.lat),
              Number(point.lon)
            ]
          );


      const simulated =
        track
          .filter(
            point =>
              point.simulated
          )
          .map(
            point => [
              Number(point.lat),
              Number(point.lon)
            ]
          );


      let layer =
        traces.get(
          vehicle.tr_id
        );


      if (!layer) {
        layer =
          L.layerGroup()
            .addTo(map);

        traces.set(
          vehicle.tr_id,
          layer
        );
      }


      layer.clearLayers();


      if (
        live.length > 1
      ) {
        L.polyline(
          live,
          {
            color:
              '#51b7ff',

            weight:
              3,

            opacity:
              0.78,

            dashArray:
              '7 5',

            interactive:
              false
          }
        ).addTo(layer);
      }


      if (
        simulated.length > 1
      ) {
        L.polyline(
          simulated,
          {
            color:
              '#ba8cff',

            weight:
              4,

            opacity:
              0.92,

            dashArray:
              '2 7',

            interactive:
              false
          }
        ).addTo(layer);
      }
    }


    /*
     * Удаляем старые треки,
     * которых уже нет
     * в новых данных.
     */
    for (
      const [
        id,
        layer
      ] of traces
    ) {
      if (
        !visible.has(id)
      ) {
        layer.remove();

        traces.delete(id);
      }
    }
  }


  /* =======================================================
     FIT ALL
     ======================================================= */

  function fit() {
    if (!map) return;


    /*
     * "Показать все" считается
     * явным выходом из режима слежения.
     */
    followSelection =
      false;

    updateFollowButton();


    const coordinates =
      vehicles
        .filter(valid)
        .map(
          vehicle => [
            Number(vehicle.lat),
            Number(vehicle.lon)
          ]
        );


    const combined =
      bounds
        ? L.latLngBounds(
            bounds.getSouthWest(),
            bounds.getNorthEast()
          )
        : L.latLngBounds([]);


    coordinates.forEach(
      point =>
        combined.extend(point)
    );


    if (
      combined.isValid()
    ) {
      map.fitBounds(
        combined,
        {
          padding:
            [35, 35],

          maxZoom:
            13
        }
      );
    }
  }


  /* =======================================================
     MOUNT
     ======================================================= */

  function mount(callback) {
    onChoose =
      callback;


    if (!window.L) {
      notify(
        'Не удалось загрузить карту. ' +
        'Таблица и карточка доступны.'
      );

      return;
    }


    map =
      L.map(
        'map',
        {
          zoomControl:
            false,

          scrollWheelZoom:
            false,

          attributionControl:
            false
        }
      ).setView(
        [
          55.75,
          37.62
        ],
        10
      );


    L.control
      .zoom({
        position:
          'topleft',

        zoomInTitle:
          'Приблизить',

        zoomOutTitle:
          'Отдалить'
      })
      .addTo(map);


    const config =
      window.TRANSIT_MAP;


    L.control
      .attribution({
        prefix:
          false,

        position:
          'bottomright'
      })
      .addAttribution(
        config.attribution
      )
      .addTo(map);


    const tiles =
      L.tileLayer(
        config.tileUrl,
        {
          subdomains:
            config.subdomains ||
            'abc',

          attribution:
            config.attribution,

          maxZoom:
            config.maxZoom,

          minZoom:
            config.minZoom ||
            3,

          updateWhenIdle:
            true,

          keepBuffer:
            1,

          detectRetina:
            true
        }
      )
      .addTo(map);


    tiles.on(
      'loading',
      () => {
        failedTiles = 0;
        loadedTiles = 0;
      }
    );


    tiles.on(
      'tileerror',
      () => {
        failedTiles++;

        notify(
          'Подложка недоступна. ' +
          'Линии и транспорт ' +
          'остаются на карте.'
        );
      }
    );


    tiles.on(
      'tileload',
      () => {
        loadedTiles++;
      }
    );


    tiles.on(
      'load',
      () => {
        if (
          loadedTiles > 0 &&
          !failedTiles
        ) {
          notify();
        }
      }
    );


    /* -----------------------------------------------
       SHOW ALL
       ----------------------------------------------- */

    const fitButton =
      document.getElementById(
        'map-fit'
      );

    if (fitButton) {
      fitButton.onclick =
        fit;
    }


    /* -----------------------------------------------
       FOLLOW
       ----------------------------------------------- */

    const followButton =
      document.getElementById(
        'map-follow'
      );

    if (followButton) {
      followButton.onclick =
        () => {
          setFollow(
            !followSelection
          );
        };
    }


    /* -----------------------------------------------
       ROUTE FILTER
       ----------------------------------------------- */

    const allRoutes =
      document.getElementById(
        'map-all-routes'
      );

    if (allRoutes) {
      allRoutes.onchange =
        styleRoutes;
    }


    /* -----------------------------------------------
       RESIZE
       ----------------------------------------------- */

    new ResizeObserver(
      () => {
        /*
         * Drawer сдвигает dashboard,
         * поэтому размеры контейнера карты
         * реально изменяются.
         *
         * Leaflet должен узнать
         * о новом размере.
         */
        map.invalidateSize({
          pan:
            false
        });


        if (
          followSelection
        ) {
          /*
           * После изменения ширины
           * ещё раз центрируем
           * выбранное ТС.
           */
          requestAnimationFrame(
            () =>
              followSelectedVehicle(
                false
              )
          );
        }
      }
    ).observe(
      document.getElementById(
        'map'
      )
    );


    updateFollowButton();
  }


  /* =======================================================
     NETWORK
     ======================================================= */

  function updateNetwork(
    nextNetwork
  ) {
    if (
      network ===
      nextNetwork
    ) {
      return;
    }


    routes.forEach(
      line =>
        line.remove()
    );

    routes.clear();

    network =
      nextNetwork;


    const all = [];


    for (
      const path
      of network
    ) {
      const points =
        path.points
          .filter(
            ([lon, lat]) =>
              valid({
                lon:
                  Number(lon),

                lat:
                  Number(lat)
              })
          )
          .map(
            ([lon, lat]) => [
              Number(lat),
              Number(lon)
            ]
          );


      /*
       * Убираем соседние
       * одинаковые координаты.
       */
      const line =
        points.filter(
          (
            point,
            index
          ) =>
            !index ||
            point[0] !==
              points[
                index - 1
              ][0] ||
            point[1] !==
              points[
                index - 1
              ][1]
        );


      if (
        line.length < 2
      ) {
        continue;
      }


      routes.set(
        path.tr_id,

        L.polyline(
          line,
          {
            interactive:
              false,

            smoothFactor:
              1.3
          }
        ).addTo(map)
      );


      all.push(
        ...line
      );
    }


    bounds =
      all.length
        ? L.latLngBounds(all)
        : null;
  }


  /* =======================================================
     RENDER
     ======================================================= */

  function render(
    nextVehicles,
    nextNetwork,
    nextSelection
  ) {
    // Optional legacy hooks retained for integrations: getElementById('map-empty')?.classList
    vehicles =
      nextVehicles || [];

    selection =
      nextSelection;


    if (!map) {
      updateFollowButton();
      return;
    }


    /*
     * Если выбранного ТС
     * больше нет в текущем наборе,
     * выключаем follow.
     */
    if (
      selection != null &&
      !vehicles.some(
        vehicle =>
          vehicle.tr_id ===
          selection
      )
    ) {
      followSelection =
        false;

      selection =
        null;
    }


    updateNetwork(
      nextNetwork || []
    );


    const positioned =
      vehicles.filter(valid);


    const ids =
      new Set(
        positioned.map(
          vehicle =>
            vehicle.tr_id
        )
      );


    document
      .getElementById(
        'map-empty'
      )
      ?.classList
      .toggle(
        'show',
        !positioned.length
      );


    /* -----------------------------------------------
       REMOVE OLD MARKERS
       ----------------------------------------------- */

    for (
      const [
        id,
        marker
      ] of markers
    ) {
      if (
        !ids.has(id)
      ) {
        const animation =
          markerAnimations.get(id);

        if (animation) {
          cancelAnimationFrame(
            animation
          );
        }

        markerAnimations.delete(id);

        marker.remove();

        markers.delete(id);
      }
    }


    /* -----------------------------------------------
       CREATE / UPDATE MARKERS
       ----------------------------------------------- */

    for (
      const vehicle
      of positioned
    ) {
      const lat =
        Number(vehicle.lat);

      const lon =
        Number(vehicle.lon);


      let marker =
        markers.get(
          vehicle.tr_id
        );


      if (!marker) {
        marker =
          L.marker(
            [
              lat,
              lon
            ],
            {
              icon:
                icon(vehicle),

              title:
                vehicle.scenario
                  ? 'Резервное ТС'
                  : (
                      `ТС ` +
                      `${vehicle.tr_id}`
                    ),

              keyboard:
                true,

              riseOnHover:
                true
            }
          )
          .addTo(map);

        marker.options.hasLivePosition =
          vehicle.source === 'live' ||
          vehicle.live_position;


        marker.bindPopup(
          content(vehicle),
          {
            className:
              'transit-popup',

            minWidth:
              235,

            maxWidth:
              270,

            autoPanPadding:
              [18, 25]
          }
        );


        marker.bindTooltip(
          vehicle.scenario
            ? 'Резерв · what-if'
            : `ТС ${vehicle.tr_id}`,
          {
            className:
              'transit-map-tooltip',

            offset:
              [0, -16],

            direction:
              'top'
          }
        );


        /*
         * Выбор маркера
         * передаём dispatcher.js.
         *
         * dispatcher.js вызовет focus(),
         * который включит follow.
         */
        marker.on(
          'click',
          () => {
            onChoose(
              vehicle.tr_id
            );
          }
        );


        markers.set(
          vehicle.tr_id,
          marker
        );

      } else {

        /* -------------------------------------------
           POSITION UPDATE
           ------------------------------------------- */

        const position =
          marker.getLatLng();


        if (
          position.lat !== lat ||
          position.lng !== lon
        ) {
          const isLivePosition =
            vehicle.source === 'live' ||
            vehicle.live_position;
          // A waiting_for_live row is deliberately drawn at the first
          // planned stop so the fleet count stays honest.  The first real
          // packet can be anywhere further along that route; animating from
          // the placeholder would look like a teleport across the map.
          // Start the marker at the first live coordinate, then animate only
          // between two consecutive live observations.
          if (
            isLivePosition &&
            marker.options.hasLivePosition
          ) {
            animateMarker(
              marker,
              vehicle.tr_id,
              lat,
              lon
            );
          } else {
            marker.setLatLng(
              [
                lat,
                lon
              ]
            );
          }
          marker.options.hasLivePosition = isLivePosition;
        }

        if (vehicle.source === 'live' || vehicle.live_position) {
          marker.options.hasLivePosition = true;
        }


        /* -------------------------------------------
           LEVEL / ICON
           ------------------------------------------- */

        if (
          marker.options.level !==
          vehicle.level
        ) {
          marker.setIcon(
            icon(vehicle)
          );
        }


        /* -------------------------------------------
           POPUP
           ------------------------------------------- */

        const nextContent =
          content(vehicle);


        if (
          marker
            .getPopup()
            .getContent() !==
          nextContent
        ) {
          marker.setPopupContent(
            nextContent
          );
        }
      }


      marker.options.level =
        vehicle.level;


      const node =
        marker.getElement();


      if (node) {
        node.setAttribute(
          'role',
          'button'
        );

        node.setAttribute(
          'aria-label',
          `ТС ${vehicle.tr_id}: открыть карточку`
        );

        node.classList.toggle(
          'live-marker',
          Boolean(
            vehicle.live_position
          )
        );
      }
    }


    /* -----------------------------------------------
       INITIAL FIT
       ----------------------------------------------- */

    if (
      !initialFit &&
      (
        bounds ||
        positioned.length
      )
    ) {
      fit();

      initialFit =
        true;
    }


    /* -----------------------------------------------
       TRACKS + ROUTES
       ----------------------------------------------- */

    drawTracks();

    styleRoutes();


    /* -----------------------------------------------
       FOLLOW SELECTED VEHICLE
       ----------------------------------------------- */

    if (
      followSelection &&
      selection != null
    ) {
      followSelectedVehicle(
        true
      );
    }


    updateFollowButton();
  }


  /* =======================================================
     FOCUS VEHICLE
     ======================================================= */

  function focus(id) {
    // Compact call shape kept for downstream smoke checks: setView(marker.getLatLng(),targetZoom
    const marker =
      markers.get(id);


    if (
      !map ||
      !marker
    ) {
      return;
    }


    selection =
      id;


    /*
     * Каждый новый выбор ТС
     * автоматически включает
     * режим слежения.
     */
    followSelection =
      true;


    styleRoutes();

    updateFollowButton();


    const currentZoom =
      map.getZoom();


    /*
     * Первый переход к выбранному ТС
     * делает более близкий масштаб.
     */
    const targetZoom =
      Math.max(
        14,

        Math.min(
          16,
          currentZoom + 3
        )
      );


    map.setView(
      marker.getLatLng(),
      targetZoom,
      {
        animate:
          true
      }
    );


    marker.openPopup();
  }


  /* =======================================================
     PUBLIC API
     ======================================================= */

  window.TransitMap = {
    mount,
    render,
    focus,
    fit,

    /*
     * Дополнительно оставляем методы,
     * чтобы потом можно было управлять
     * режимом из другого UI.
     */
    setFollow,

    isFollowing:
      () =>
        followSelection
  };

})();
