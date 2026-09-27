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
