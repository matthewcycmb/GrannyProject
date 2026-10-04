const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createReadiness} = require('../granny/static/steps.js');
const ready = {state_key:'MONITORING',state:'Monitoring',calibrated:true,frame_available:true,tracking_available:true};

test('loss is immediate, and a brief good frame cannot clear interrupted tracking', () => {
  const view = createReadiness();
  assert.equal(view(ready,0).title,'Tracking stabilizing');
  assert.equal(view(ready,1100).title,'Monitoring');
  for (const [at,camera] of [[1200,'No person detected'],[1600,'Multiple people: use one person']]) {
    const result=view({...ready,tracking_available:false,camera},at);
    assert.equal(result.title,'Body tracking paused');
    assert.equal(result.video,'Video connected');
    assert.equal(result.attention,true);
  }
  assert.equal(view(ready,1800).title,'Body tracking paused');
  assert.equal(view({...ready,tracking_available:false},2000).title,'Body tracking paused');
  assert.equal(view(ready,2100).title,'Body tracking paused');
  assert.equal(view(ready,3200).title,'Monitoring');
});
test('camera loss is distinct from body loss and cannot appear healthy', () => {
  const view=createReadiness();
  view(ready,0);
  const result=view({...ready,frame_available:false,tracking_available:false},2000);
  assert.equal(result.title,'Camera interrupted');
  assert.equal(result.video,'Video interrupted · reconnecting');
  assert.equal(result.attention,true);
});
test('checking and alert states remain visible through a camera outage', () => {
  const view=createReadiness();
  for (const state_key of ['CHECKING','ALERTED','COOLDOWN']) {
    const result=view({...ready,state_key,state:'Incident status',reason:'Incident reason',frame_available:false,tracking_available:false},1000);
    assert.equal(result.title,'Incident status');
    assert.equal(result.description,'Incident reason');
  }
});
test('a possible fall keeps its evidence visible once tracking stabilizes', () => {
  const view=createReadiness();
  const fall={...ready,state_key:'POSSIBLE_FALL',state:'Possible fall',reason:'Near-floor posture: 2.0 of 4 seconds observed'};
  view(fall,0);
  assert.equal(view(fall,1200).title,'Possible fall');
  assert.equal(view(fall,1300).description,fall.reason);
  assert.equal(view({...fall,tracking_available:false},1400).title,'Body tracking paused');
});
