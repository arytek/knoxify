// Exercise job UI state without a browser or external map requests.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function page(respond, savedId = null) {
  const elements = new Map();
  const storage = new Map(savedId ? [['knoxify.activeJob', savedId]] : []);
  function newElement(id) {
    return {
      value: id === 'metersPerTile' ? '1' : '', textContent: '', innerHTML: '',
      hidden: true, disabled: false, listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; },
      appendChild(child) { (this.children ||= []).push(child); return child; },
      append(...children) { (this.children ||= []).push(...children); },
      scrollIntoView() {},
    };
  }
  function element(id) {
    if (!elements.has(id)) elements.set(id, newElement(id));
    return elements.get(id);
  }
  function layer() { return { addTo() { return this; }, clearLayers() {}, addLayer() {} }; }
  const timers = new Set();
  const context = vm.createContext({
    L: {
      map: () => ({ setView() { return this; }, addControl() {}, on() {}, fitBounds() {} }),
      tileLayer: layer, FeatureGroup: layer, Control: { Draw: function () {} },
      Draw: { Event: { CREATED: 'created', EDITED: 'edited', DELETED: 'deleted' } },
    },
    document: { getElementById: element, createElement: tag => newElement(tag) },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    fetch: async (url, options) => {
      const response = await respond(url, options);
      const status = response.status || 200;
      return { ok: status < 400, status, json: async () => response.data };
    },
    AbortController, Date, Math, Promise,
    setTimeout: (callback, delay) => {
      const timer = setTimeout(callback, delay === 15000 ? delay : 0);
      timers.add(timer);
      return timer;
    },
    clearTimeout, setInterval: () => 1, clearInterval: () => {},
    matchMedia: () => ({ matches: true }), URLSearchParams,
    confirm: () => true,
  });
  vm.runInContext(fs.readFileSync('static/js/app.js', 'utf8'), context);
  return { context, element, storage, close: () => timers.forEach(clearTimeout) };
}

async function settleUntil(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.fail('UI did not reach the expected state');
}

const result = { mapName: 'test', totalSeconds: 73, featureCount: 10,
  width: 300, height: 300, cellsX: 1, cellsY: 1,
  timings: { fetch: 60, render: 10, package: 3 },
  files: { preview: '/preview.png', zip: '/map.zip' } };

test('refresh reconnects, shows real progress, prevents duplicate generation, then shows total duration', async () => {
  let finish = false;
  const view = page(async () => ({ data: finish
    ? { state: 'complete', progress: 100, message: 'Map ready', elapsedSeconds: 73, result }
    : { state: 'running', progress: 27.5, message: '1 of 2 areas ready', elapsedSeconds: 20 }
  }), 'saved-job');
  try {
    await settleUntil(() => view.element('generation-bar').value === 27.5);
    assert.equal(view.element('progress-percent').textContent, '27%');
    assert.equal(view.element('progress-elapsed').textContent, '0:20 elapsed');
    assert.equal(view.element('generateBtn').disabled, true);
    // Selection changes must not re-enable generation while a job is running.
    vm.runInContext("currentRect = { getBounds: () => ({getSouth: () => 0, getWest: () => 0, getNorth: () => .01, getEast: () => .01}) }; updateBboxFields();", view.context);
    assert.equal(view.element('generateBtn').disabled, true);
    finish = true;
    await settleUntil(() => view.element('results').hidden === false);
    assert.match(view.element('status').textContent, /Done in 1:13/);
    assert.equal(view.element('generation-bar').value, 100);
    assert.equal(view.element('generateBtn').disabled, false);
    assert.equal(view.element('cancelBtn').hidden, true);
    assert.equal(view.storage.has('knoxify.activeJob'), false);
  } finally { view.close(); }
});

test('temporary polling failure reconnects without starting a second job', async () => {
  let calls = 0;
  const view = page(async url => {
    if (url === '/api/packs') return { data: { installed: [] } };
    if (url === '/api/packs/jobs/active') return { data: {} };
    assert.equal(url, '/api/jobs/saved-job');
    if (++calls === 1) throw new Error('Network interrupted');
    return { data: { state: 'failed', progress: 30, message: 'Generation failed', error: 'Map service busy', elapsedSeconds: 5 } };
  }, 'saved-job');
  try {
    await settleUntil(() => view.element('status').textContent === 'Map service busy');
    assert.equal(calls, 2);
    assert.equal(view.element('status').className, 'error');
    assert.equal(view.element('generation-bar').value, 30);
    assert.equal(view.element('cancelBtn').hidden, true);
  } finally { view.close(); }
});

test('cancel posts to the active job and restores controls when acknowledged', async () => {
  let cancelled = false;
  const view = page(async (url, options) => {
    if (url === '/api/packs') return { data: { installed: [] } };
    if (url === '/api/packs/jobs/active') return { data: {} };
    if (url.endsWith('/cancel')) {
      assert.equal(options.method, 'POST');
      cancelled = true;
      return { data: { state: 'cancelling', progress: 10, message: 'Cancelling', elapsedSeconds: 2 } };
    }
    return { data: { state: cancelled ? 'cancelled' : 'running', progress: 10,
      message: cancelled ? 'Generation cancelled' : 'Fetching', elapsedSeconds: 2 } };
  }, 'saved-job');
  try {
    await settleUntil(() => view.element('generation-bar').value === 10);
    await view.element('cancelBtn').listeners.click();
    await settleUntil(() => view.element('cancelBtn').hidden === true);
    assert.match(view.element('status').textContent, /Cancelled/);
    assert.equal(view.element('results').hidden, true);
  } finally { view.close(); }
});

test('a server restart clears the missing saved job and restores the generate button', async () => {
  const view = page(async () => ({ status: 404, data: { error: 'Server restarted' } }), 'gone');
  try {
    await settleUntil(() => view.element('status').textContent === 'Server restarted');
    assert.equal(view.storage.has('knoxify.activeJob'), false);
    assert.equal(view.element('generateBtn').textContent, 'Generate map');
  } finally { view.close(); }
});

test('regional data can be discovered, downloaded, prepared, and shown as ready', async () => {
  let packPolls = 0;
  const view = page(async (url, options) => {
    if (url === '/api/jobs/active' || url === '/api/packs/jobs/active') return { data: {} };
    if (url === '/api/packs') return { data: { installed: [] } };
    if (url.startsWith('/api/packs/status?')) return { data: {
      source: 'online', installedCovering: null, installed: [],
      recommended: { id: 'us/kentucky', name: 'Kentucky', installed: false },
    } };
    if (url === '/api/packs/install') {
      assert.equal(options.method, 'POST');
      return { status: 202, data: { id: 'pack-job' } };
    }
    if (url === '/api/packs/jobs/pack-job') {
      packPolls += 1;
      return { data: packPolls === 1
        ? { state: 'running', progress: 65, message: 'Downloading regional data', result: null }
        : { state: 'complete', progress: 100, message: 'Ready', result: { id: 'us/kentucky', name: 'Kentucky' } } };
    }
    assert.fail(`Unexpected request: ${url}`);
  });
  try {
    await view.context.checkPackCoverage({ south: 38, west: -85, north: 39, east: -84 });
    assert.equal(view.element('packActionBtn').hidden, false);
    assert.equal(view.element('packActionBtn').textContent, 'Download regional data');
    await view.element('packActionBtn').listeners.click();
    await settleUntil(() => view.element('pack-status').textContent === 'Kentucky is ready for offline generation.');
    assert.equal(packPolls, 2);
    assert.equal(view.element('pack-progress').hidden, true);
    assert.equal(view.element('generateBtn').disabled, true);
  } finally { view.close(); }
});
