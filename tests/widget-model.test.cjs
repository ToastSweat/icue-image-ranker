const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const runtime=path.join(__dirname,'../widget/runtime.js');
function api(){assert.ok(fs.existsSync(runtime),'widget runtime exists'); return require(runtime);}
function at(day,hour,minute=0){return new Date(2026,9,4+day,hour,minute);}
test('schedule defaults are unrestricted but manual hiding wins',()=>{
 const {defaults,visibility}=api();
 assert.equal(defaults.scheduleEnabled,false);
 assert.equal(defaults.startTime,'08:00');assert.equal(defaults.endTime,'15:00');
 assert.equal(visibility(defaults,at(0,2)).visible,true);
 assert.equal(visibility({...defaults,manualHide:true},at(0,2)).visible,false);
});
test('schedule uses half-open day windows and anchors overnight to start day',()=>{
 const {defaults,visibility}=api();const s={...defaults,scheduleEnabled:true};
 for(let day=0;day<7;day++){
  assert.equal(visibility(s,at(day,7,59)).visible,false);
  assert.equal(visibility(s,at(day,8)).visible,day>0&&day<6);
  assert.equal(visibility(s,at(day,14,59)).visible,day>0&&day<6);
  assert.equal(visibility(s,at(day,15)).visible,false);
 }
 const n={...s,startTime:'22:00',endTime:'06:00'};
 assert.equal(visibility(n,at(5,22)).visible,true);
 assert.equal(visibility(n,at(6,5,59)).visible,true);
 assert.equal(visibility(n,at(6,6)).visible,false);
 assert.equal(visibility(n,at(1,5)).visible,false);
 assert.equal(visibility({...s,startTime:'08:00',endTime:'08:00'},at(1,0)).visible,true);
 for(const startTime of ['8:00','24:00','bad','08:99','']){
  const v=visibility({...s,startTime},at(1,10));assert.equal(v.visible,false);assert.match(v.reason,/invalid/i);
 }
});
