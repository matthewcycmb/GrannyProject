const {test} = require('node:test');
const assert = require('node:assert/strict');
const {summarizeSteps} = require('../granny/static/steps.js');
const status = (deliveries = [], state_key = 'ALERTED') => ({state_key, telegram: true, calls: true, deliveries});
const row = (channel, state, confirmation = 'pending') => ({channel, state, confirmation});

test('waiting, checking, and a safe reply do not claim alerts were sent', () => {
  assert.equal(summarizeSteps(status([], 'MONITORING')).voice.state, 'waiting');
  assert.equal(summarizeSteps(status([], 'CHECKING')).voice.state, 'active');
  const safe = summarizeSteps(status([], 'COOLDOWN'));
  assert.equal(safe.voice.state, 'done');
  assert.equal(safe.telegram.text, 'Not needed');
  assert.equal(safe.calls.state, 'off');
});
test('a completed check ticks even when the person did not reply', () => {
  assert.equal(summarizeSteps({...status(), reason:'No clear response before the deadline'}).voice.text, 'No reply');
});
test('Telegram acceptance ticks sending; queued or ringing calls never tick', () => {
  for (const phase of ['queued', 'sending', 'initiated', 'ringing']) {
    const summary = summarizeSteps(status([row('telegram','accepted'),row('calls',phase)]));
    assert.equal(summary.telegram.state,'done');
    assert.equal(summary.telegram.text,'Sent to Telegram');
    assert.equal(summary.calls.state,'active');
  }
});
test('connected call ticks separately from family confirmation', () => {
  const summary = summarizeSteps(status([row('calls','completed','no-response')]));
  assert.equal(summary.calls.state,'done');
  assert.equal(summary.calls.text,'Call finished');
  assert.match(summary.confirmation,/No one has confirmed/);
  assert.match(summarizeSteps(status([row('calls','in-progress','coming')])).confirmation,/Family confirmed/);
});
test('one successful recipient cannot hide another failed recipient', () => {
  for (const [channel, success] of [['telegram','accepted'],['calls','completed']]) {
    const summary = summarizeSteps(status([row(channel,success),row(channel,'failed')]));
    assert.equal(summary[channel].state,'warning');
    assert.match(summary[channel].text,/1\/2/);
  }
});
test('no answer and uncertain delivery get no tick', () => {
  assert.equal(summarizeSteps(status([row('calls','no-answer')])).calls.state,'error');
  assert.equal(summarizeSteps(status([row('telegram','unconfirmed')])).telegram.state,'warning');
});
test('partial delivery still in flight stays active with the right channel label', () => {
  const summary=summarizeSteps(status([row('telegram','accepted'),row('telegram','sending'),row('calls','completed'),row('calls','ringing')]));
  assert.deepEqual(summary.telegram,{state:'active',text:'1/2 sent'});
  assert.deepEqual(summary.calls,{state:'active',text:'1/2 connected'});
});
test('disabled channels and disconnected dashboard get no success tick', () => {
  assert.equal(summarizeSteps({...status(),telegram:false}).telegram.state,'off');
  const offline=summarizeSteps(status([row('calls','completed'),row('telegram','accepted')]),false);
  for (const channel of ['voice','telegram','calls']) assert.equal(offline[channel].state,'offline');
  assert.equal(offline.confirmation,'');
});

test('retrying calls stay active and a confirmed family member ends the retry display', () => {
  const rows=[row('calls','retry-wait','no-response'),row('calls','completed','no-response')];
  assert.deepEqual(summarizeSteps(status(rows)).calls,{state:'active',text:'Calling again soon'});
  rows[1].confirmation='coming';
  assert.deepEqual(summarizeSteps(status(rows)).calls,{state:'done',text:'Family coming'});
});
