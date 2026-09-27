// Execute the actual polling functions with deterministic timers, no browser.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync('dashboard/dispatcher.js', 'utf8');
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let index = open; index < source.length; index++) {
    if (source[index] === '{') depth++;
    if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Missing end of ${name}`);
}

function harness() {
  const timers = new Map();
  let timerId = 0, calls = 0, release;
  const context = vm.createContext({
    window: {
      setTimeout(callback, delay) { assert.equal(delay, 1000); timers.set(++timerId, callback); return timerId; },
      clearTimeout(id) { timers.delete(id); }
    },
    refreshForProfile() { calls++; return new Promise(resolve => { release = resolve; }); }
  });
  const declarations = source.slice(source.indexOf('const REFRESH_INTERVAL_MS'), source.indexOf('/* Данные, которые'));
  vm.runInContext(`${declarations}
    let profileId = 'dispatcher-01';
    ${functionSource('refresh')}
    ${functionSource('startRefreshLoop')}
    ${functionSource('stopRefreshLoop')}
    globalThis.poll = {refresh, startRefreshLoop, stopRefreshLoop};`, context);
  return {
    poll: context.poll, timers, calls: () => calls, release: () => release(),
    tick() { const [id, callback] = timers.entries().next().value; timers.delete(id); return callback(); }
  };
}

test('repeated starts create only one timer', () => {
  const h = harness();
  h.poll.startRefreshLoop(); h.poll.startRefreshLoop();
  assert.equal(h.timers.size, 1);
  h.poll.stopRefreshLoop();
  assert.equal(h.timers.size, 0);
});

test('slow refresh is awaited and overlapping refresh requests are deduplicated', async () => {
  const h = harness(); h.poll.startRefreshLoop();
  const tick = h.tick();
  assert.equal(h.calls(), 1); assert.equal(h.timers.size, 0);
  const manual = h.poll.refresh(); const duplicate = h.poll.refresh();
  assert.equal(manual, duplicate); assert.equal(h.calls(), 1);
  h.release(); await tick;
  assert.equal(h.timers.size, 1);
});

test('stop during an in-flight request prevents its loop from restarting', async () => {
  const h = harness(); h.poll.startRefreshLoop();
  const tick = h.tick(); h.poll.stopRefreshLoop();
  h.release(); await tick;
  assert.equal(h.timers.size, 0);
});

test('stop and restart during a request cannot revive the old generation', async () => {
  const h = harness(); h.poll.startRefreshLoop();
  const oldTick = h.tick(); h.poll.stopRefreshLoop(); h.poll.startRefreshLoop();
  const newTick = h.tick();
  assert.equal(h.calls(), 1);
  h.release(); await Promise.all([oldTick, newTick]);
  assert.equal(h.timers.size, 1);
});

test('reserve selection opens the chosen real vehicle and ignores stale selections', () => {
  const opened = [];
  const context = vm.createContext({baseVehicles: [{tr_id: 11}, {tr_id: 22}], openVehicle: id => opened.push(id)});
  vm.runInContext(`${functionSource('selectReserveVehicle')}; globalThis.selectVehicle = selectReserveVehicle;`, context);
  context.selectVehicle({target: {value: '22'}});
  context.selectVehicle({target: {value: '11'}});
  context.selectVehicle({target: {value: '999'}});
  context.selectVehicle({target: {value: '-22'}});
  context.selectVehicle({target: {value: ''}});
  assert.deepEqual(opened, [22, 11]);
});

function commandHarness(ticket, feedback = null, journal = [], vehicleId = 22) {
  const context = vm.createContext({
    actionCenter: ticket ? [ticket] : [], commandFeedback: feedback,
    CUSTOM_TR_ID_OFFSET: 1000000,
    detailSupplementCache: new Map([[String(vehicleId), {items: journal}]]),
    detailCacheKey: id => String(id), esc: text => String(text)
  });
  vm.runInContext(`${functionSource('commandFeedbackHtml')}; globalThis.feedbackHtml = commandFeedbackHtml;`, context);
  return context.feedbackHtml(vehicleId);
}

test('pending demo receipt button survives reload using the active ticket id', () => {
  const html = commandHarness({tr_id: 22, kind: 'driver_command', status: 'pending', attempt_id: 'command-2'});
  assert.match(html, /Отправляем водителю/);
  assert.match(html, /data-command-id="command-2"/);
});

test('executing ticket survives reload without claiming a predicted saving', () => {
  const html = commandHarness({tr_id: 22, kind: 'driver_command', status: 'executing', attempt_id: 'command-2'});
  assert.match(html, /Исполняется/);
  assert.doesNotMatch(html, /simulate-command|Ожидаемое сохранение/);
});

test('completed journal does not render a deleted ticket as still executing', () => {
  const html = commandHarness(null, {tr_id: 22, kind: 'simulation', commandId: 'command-2'},
    [{id: 'command-2', status: 'completed'}]);
  assert.match(html, /Тикет закрыт и удалён/);
  assert.doesNotMatch(html, /Исполняется|simulate-command/);
});

test('green terminal ticket is labelled closed during its fifteen-second retention', () => {
  const html = commandHarness({tr_id: 22, kind: 'driver_command', tone: 'success', status: 'completed_success'},
    {tr_id: 22, kind: 'simulation', commandId: 'command-2'});
  assert.match(html, /Закрыт/);
  assert.match(html, /15 секунд/);
  assert.doesNotMatch(html, /Исполняется|simulate-command|закрыт и удалён/);
});

test('demo button distinguishes real acceleration on custom TS from receipt-only on original TS', () => {
  assert.match(commandHarness({tr_id: 22, kind: 'driver_command', status: 'pending', attempt_id: 'cmd'}), /только приём/);
  assert.match(commandHarness({tr_id: 1000022, kind: 'driver_command', status: 'pending', attempt_id: 'cmd'}, null, [], 1000022), /повысит debug-скорость/);
});

test('action center renders separate waiting, executing and closed colors without extending expiry', () => {
  let now = Date.now();
  const start = now;
  const timers = new Map();
  let timerId = 0;
  const list = {innerHTML: ''}, summary = {textContent: ''};
  const context = vm.createContext({
    Date: class extends Date { static now() {return now;} },
    $: id => id === 'action-center-list' ? list : summary,
    document: {querySelectorAll: () => []},
    esc: value => String(value), time: value => String(value), openVehicle: () => {},
    setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;},
    clearTimeout: id => timers.delete(id)
  });
  vm.runInContext(`let actionCenterExpiryTimer = null;
    let actionCenter = [
      {tr_id: 1, status: 'pending', tone: 'pending'},
      {tr_id: 2, status: 'executing', tone: 'executing'},
      {tr_id: 3, status: 'completed_success', tone: 'success', status_label: 'Закрыт', visible_until: new Date(Date.now() + 15000).toISOString()}
    ];
    ${functionSource('renderActionCenter')}; globalThis.draw = renderActionCenter;`, context);
  context.draw();
  assert.match(list.innerHTML, /is-pending/);
  assert.match(list.innerHTML, /is-executing/);
  assert.match(list.innerHTML, /is-success/);
  assert.match(summary.textContent, /1 исполняются/);
  assert.equal(timers.size,1);
  now += 5000; context.draw();
  assert.equal(timers.size,1);
  assert.equal([...timers.values()][0].delay,10020);
  now = start + 15020;
  [...timers.values()][0].callback();
  assert.doesNotMatch(list.innerHTML, /is-success/);
  assert.match(list.innerHTML, /is-executing/);
  assert.equal(timers.size,0);
});

test('demo response updates the correct debug-speed control even if the selected card changed', async () => {
  const invalidated = [];
  const context = vm.createContext({
    selectedId: 22, $: () => ({disabled: false}),
    commandHistoryOpen: false, commandFeedback: null,
    debugSpeedOverrides: new Map(), debugSpeedDraft: {trId: 22, value: '10'}, debugSpeedFeedback: null,
    api: async () => ({id: 'cmd-custom', tr_id: 1000022,
      simulated_response: {response: 'Принято', debug_speed: {speed_kmh: 45}}}),
    invalidateDetailSupplement: id => invalidated.push(id), refresh: async () => {}, esc: String
  });
  vm.runInContext(`async ${functionSource('simulateCommand')}; globalThis.simulate = simulateCommand;`, context);
  await context.simulate('cmd-custom');
  assert.equal(context.debugSpeedOverrides.get(1000022).speed,45);
  assert.equal(context.debugSpeedFeedback.trId,1000022);
  assert.equal(context.debugSpeedDraft.trId,22);
  assert.equal(context.commandFeedback.tr_id,1000022);
  assert.deepEqual(invalidated,[1000022]);
});

test('debug reset remains available after reload even when this tab has no overlay cache', () => {
  const context = vm.createContext({
    isCustomEmulatorVehicle: () => true, debugSpeedOverrides: new Map(),
    debugSpeedDraft: null, debugSpeedFeedback: null, esc: String
  });
  vm.runInContext(`${functionSource('debugSpeedControlHtml')}; globalThis.controlHtml = debugSpeedControlHtml;`, context);
  const html = context.controlHtml({tr_id: 1000022, features: {speed_last: 30}});
  assert.match(html, /id="debug-speed-reset"/);
  assert.doesNotMatch(html, /id="debug-speed-reset"[^>]*disabled/);
  assert.match(html, /демо-ускорение/);
});

test('delivery timeout removes stale demo receipt affordance', () => {
  const html = commandHarness({tr_id: 22, kind: 'driver_command', status: 'not_delivered'},
    {tr_id: 22, simulatable: true, commandId: 'old-command'});
  assert.match(html, /Приём команды не подтверждён/);
  assert.doesNotMatch(html, /simulate-command/);
});

function sourceDialogHarness() {
  const listeners = new Map();
  let closes = 0;
  const dialog = {
    getBoundingClientRect: () => ({left: 100, right: 600, top: 80, bottom: 400}),
    addEventListener: (name, callback) => listeners.set(name, callback),
    close() { closes++; listeners.get('close')?.({}); }
  };
  const context = vm.createContext({dialog});
  vm.runInContext(`${functionSource('bindSourceDialogDismiss')}; bindSourceDialogDismiss(dialog);`, context);
  return {dialog, closes: () => closes,
    fire: (name, x, y, target = dialog) => listeners.get(name)?.({clientX: x, clientY: y, target})};
}

test('clicking any backdrop edge dismisses the source dialog', () => {
  for (const [x,y] of [[50,200],[650,200],[300,40],[300,450]]) {
    const h = sourceDialogHarness();
    h.fire('pointerdown',x,y); h.fire('pointerup',x,y);
    assert.equal(h.closes(),1);
  }
});

test('clicks inside the source dialog or its controls do not dismiss it', () => {
  const h = sourceDialogHarness();
  h.fire('pointerdown',120,100); h.fire('pointerup',120,100);
  h.fire('pointerdown',130,120,{}); h.fire('pointerup',130,120,{});
  assert.equal(h.closes(),0);
});

test('dragging between dialog and backdrop does not dismiss it', () => {
  const h = sourceDialogHarness();
  h.fire('pointerdown',120,100); h.fire('pointerup',50,100);
  h.fire('pointerdown',50,100); h.fire('pointerup',120,100);
  assert.equal(h.closes(),0);
});

test('cancelled gesture and closing then reopening reset backdrop state', () => {
  const h = sourceDialogHarness();
  h.fire('pointerdown',50,100); h.fire('pointercancel'); h.fire('pointerup',50,100);
  assert.equal(h.closes(),0);
  h.fire('pointerdown',50,100); h.dialog.close(); h.fire('pointerup',50,100);
  assert.equal(h.closes(),1);
});

test('unknown source statuses have a readable Russian label', async () => {
  const box = {innerHTML: '', querySelectorAll: () => []};
  const context = vm.createContext({
    $: () => box, currentProfile: () => null, esc: text => String(text),
    api: async path => path === '/api/state' ? {state: {mode: 'live'}}
      : {sources: [{id: 'custom-emulator', label: 'custom-emulator', status: 'unknown'}]}
  });
  vm.runInContext(`async ${functionSource('refreshSourceStatus')}; globalThis.renderSources = refreshSourceStatus;`, context);
  await context.renderSources();
  assert.match(box.innerHTML, />Неизвестно</);
  assert.doesNotMatch(box.innerHTML, />unknown</);
});
