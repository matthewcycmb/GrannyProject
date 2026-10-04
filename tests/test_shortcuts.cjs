const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {summarizeSteps, createReadiness} = require('../granny/static/steps.js');

// Run the real dashboard handlers with a minimal DOM and an isolated mock API.
// No live browser, microphone, camera, or notification providers are contacted.
class Element {
  constructor(action) {
    this.dataset = {action};
    this.listeners = {};
    this.children = [];
    this.style = {};
    this.classList = {toggle() {}};
    this.textContent = '';
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  querySelector() { return new Element(); }
  replaceChildren(...items) { this.children = items; }
  append(...items) { this.children.push(...items); }
  prepend(item) { this.children.unshift(item); }
  remove() {}
  closest() { return null; }
}
const settle = () => new Promise(setImmediate);

async function dashboard(overrides = {}, holdPost = false) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const buttons = ['calibrate', 'simulate', 'okay', 'help', 'stop_voice', 'stop_call_audio', 'reset']
    .map((action) => new Element(action));
  const keys = {};
  const timers = [];
  const requests = [];
  let release;
  const status = {running: true, state_key: 'UNCALIBRATED', state: 'Waiting', incident_id: '',
    control_token: 'test-token', remaining_seconds: null, fps: 0, frame_available: false,
    telegram: true, calls: true, deliveries: [], alerts: 'Ready', ...overrides};
  const context = vm.createContext({
    document: {getElementById: get, querySelectorAll: () => buttons,
      createElement: () => new Element(), createTextNode: (text) => text,
      addEventListener: (type, fn) => { keys[type] = fn; }},
    summarizeSteps, createReadiness, AbortSignal, performance,
    setTimeout: (fn, ms) => { timers.push({fn, ms}); },
    fetch: async (url, options = {}) => {
      if (url === '/api/status') return {ok: true, json: async () => ({...status})};
      assert.equal(url, '/api/action');
      assert.equal(options.headers['X-Granny-Token'], 'test-token');
      const body = JSON.parse(options.body);
      requests.push(body);
      if (holdPost) await new Promise((resolve) => { release = resolve; });
      if (body.action === 'help') Object.assign(status, {state_key: 'ALERTED', incident_id: 'test-incident'});
      return {ok: true, json: async () => ({message: 'Action applied'})};
    },
  });
  vm.runInContext(fs.readFileSync(require.resolve('../granny/static/app.js'), 'utf8'), context);
  await settle();
  return {requests, timers, get, release: () => release(),
    click: (action) => buttons.find((button) => button.dataset.action === action).listeners.click(),
    press: (extra = {}) => keys.keydown({key: 'l', target: new Element(), preventDefault() {}, ...extra})};
}

test('L triggers Help immediately without a camera, calibration, or voice check', async () => {
  for (const key of ['l', 'L']) {
    const page = await dashboard();
    page.press({key});
    await settle();
    assert.deepEqual(page.requests, [{action: 'help', incident_id: ''}]);
    assert.match(page.get('action-message').textContent, /Alert triggered/);
  }
});

test('L interrupts a check without waiting for its 45-second deadline', async () => {
  const page = await dashboard({state_key: 'CHECKING', incident_id: 'fall', remaining_seconds: 45});
  page.press();
  await settle();
  assert.deepEqual(page.requests, [{action: 'help', incident_id: 'fall'}]);
});

test('held or repeated L sends one request, including while the request is pending', async () => {
  const page = await dashboard({}, true);
  page.press();
  page.press({repeat: true});
  page.press();
  assert.equal(page.requests.length, 1);
  page.release();
  await settle();
  page.press();
  assert.equal(page.requests.length, 1);
});

test('typing, composition, browser shortcuts and other keys do not trigger alerts', async () => {
  const page = await dashboard();
  for (const extra of [{key: 'k'}, {repeat: true}, {isComposing: true}, {defaultPrevented: true},
      {ctrlKey: true}, {metaKey: true}, {altKey: true},
      {target: {isContentEditable: true}}, {target: {closest: () => ({tagName: 'INPUT'})}}]) {
    page.press(extra);
  }
  await settle();
  assert.equal(page.requests.length, 0);
});

test('L preempts calibration and its old countdown cannot send a later action', async () => {
  const page = await dashboard();
  const calibration = page.click('calibrate');
  assert.match(page.get('action-message').textContent, /Calibrating in 5/);
  page.press();
  await settle();
  assert.deepEqual(page.requests, [{action: 'help', incident_id: ''}]);
  page.timers.find(({ms}) => ms === 1000).fn();
  await calibration;
  assert.equal(page.requests.length, 1);
  assert.match(page.get('action-message').textContent, /Alert triggered/);
});

test('L does not submit when the app is disconnected or already alerted', async () => {
  for (const status of [{running: false}, {state_key: 'ALERTED'}]) {
    const page = await dashboard(status);
    page.press();
    await settle();
    assert.equal(page.requests.length, 0);
  }
});
