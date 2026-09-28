const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
function element() {
 const classes = new Set();
 return {disabled:false,textContent:'',value:'',checked:false,style:{},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)},setAttribute(){},addEventListener(){},querySelectorAll:()=>[]};
}
const context = vm.createContext({console,AbortController,setTimeout,clearTimeout,
 setInterval:()=>1,clearInterval(){},localStorage:{getItem:()=>null},
 document:{addEventListener(){},getElementById:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)},querySelectorAll:()=>[],querySelector:()=>null},window:{}});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../static/js/app.js'),'utf8'), context);
const run=code=>vm.runInContext(code,context);
run('toast=()=>{}; map={removeLayer(){},stop(){},setView(){}}; renderRoamArea=()=>{}; updateStatusBar=()=>{}; checkStealth=async()=>{}; renderReadout=()=>{}; drawRoamPath=()=>{};');
run('$("roam-radius").value="1"; roamCentre={lat:48.86,lon:2.30};');
const chunk={waypoints:[{lat:48.86,lng:2.30},{lat:48.861,lng:2.301}],coordinates:[[2.30,48.86],[2.301,48.861]],distance_km:0.15};
const ok=data=>({ok:true,json:async()=>data});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 // Start lookup immediately while the previous route stops, but do not move
 // until both finish. The old serialized flow fails the first assertion.
 let finishStop, finishLookup; let parallelStarts=0;
 context.fetch=async url=>{
  if(url==='/api/route/stop')return new Promise(resolve=>{finishStop=resolve});
  if(url==='/api/roam/route')return new Promise(resolve=>{finishLookup=resolve});
  if(url==='/api/route/start')parallelStarts++;
  return ok({});
 };
 let parallelStart=run('startRoaming()'); await tick();
 assert.equal(typeof finishLookup,'function','Road lookup must not wait for the previous route to stop');
 finishLookup(ok(chunk)); await tick();
 assert.equal(parallelStarts,0,'A prepared route must wait for stop confirmation');
 finishStop(ok({})); await parallelStart;
 assert.equal(parallelStarts,1);
 run('cancelMovementUI()');
 // The inverse completion order must also wait for the roads.
 parallelStart=run('startRoaming()'); await tick();
 finishStop(ok({})); await tick(); assert.equal(parallelStarts,1);
 finishLookup(ok(chunk)); await parallelStart; assert.equal(parallelStarts,2);
 run('cancelMovementUI()');
 // Reset while stop is pending and roads are ready cannot start movement.
 parallelStart=run('startRoaming()'); await tick();
 finishLookup(ok(chunk)); await tick();
 await run('clearLocation()');
 finishStop(ok({})); await parallelStart;
 assert.equal(parallelStarts,2); assert.equal(run('roamActive'),false);
 // A failed stop blocks movement, aborts lookup, and handles its later
 // rejection. Cover both an HTTP error and a broken connection.
 for(const networkFailure of [false,true]) {
  let rejectLookup, lookupSignal;
  context.fetch=async(url,options)=>{
   if(url==='/api/route/stop') {
    if(networkFailure)throw new Error('Stop connection failed');
    return {ok:false,json:async()=>({error:'Could not stop previous route'})};
   }
   if(url==='/api/roam/route') {
    lookupSignal=options.signal;
    return new Promise((resolve,reject)=>{rejectLookup=reject});
   }
   if(url==='/api/route/start')parallelStarts++;
   return ok({});
  };
  await run('startRoaming()');
  assert.equal(parallelStarts,2);
  assert.equal(lookupSignal.aborted,true);
  assert.equal(elements.get('btn-roam-start').disabled,false);
  run('updateRoamUI()');
  assert.equal(elements.get('btn-roam-stop').disabled,false,'Failed prerequisite Stop must remain retryable');
  rejectLookup(new Error('Lookup aborted')); await tick();
  context.fetch=async()=>ok({});
  await run('stopRoaming()');
  assert.equal(elements.get('btn-roam-stop').disabled,true,'Confirmed Stop clears retry state');
 }
 // A lookup failure can arrive first; the later stop rejection is handled.
 let rejectStop;
 context.fetch=async url=>{
  if(url==='/api/route/stop')return new Promise((resolve,reject)=>{rejectStop=reject});
  if(url==='/api/roam/route')throw new Error('Road lookup failed');
  if(url==='/api/route/start')parallelStarts++;
  return ok({});
 };
 await run('startRoaming()');
 assert.equal(elements.get('btn-roam-stop').disabled,true);
 rejectStop(new Error('Stop failed')); await tick();
 assert.equal(elements.get('btn-roam-stop').disabled,false,'Late Stop failure must also remain retryable');
 assert.equal(parallelStarts,2);
 // Reset invalidates a pending Stop failure from an older start attempt.
 await run('startRoaming()');
 await run('clearLocation()');
 rejectStop(new Error('Old Stop failed')); await tick();
 assert.equal(elements.get('btn-roam-stop').disabled,true);
 assert.equal(run('roamStopNeedsRetry'),false);
 // A lookup failure with a successful Stop does not leave a retry button.
 context.fetch=async url=>{
  if(url==='/api/roam/route')throw new Error('Road lookup failed');
  return ok({});
 };
 await run('startRoaming()');
 assert.equal(elements.get('btn-roam-stop').disabled,true);
 // Cancel while roads are loading. A late response must never start a route.
 let resolveRoads; let starts=0;
 context.fetch=async(url)=>{
  if(url==='/api/roam/route')return new Promise(resolve=>{resolveRoads=resolve});
  if(url==='/api/route/start')starts++;
  return ok({});
 };
 const pending=run('startRoaming()'); await tick();
 assert.equal(elements.get('btn-roam-stop').disabled,false);
 assert.equal(elements.get('btn-roam-start').textContent,'Finding roads…');
 await run('stopRoaming()'); resolveRoads(ok(chunk)); await pending;
 assert.equal(starts,0); assert.equal(run('roamActive'),false);
 assert.equal(elements.get('btn-roam-start').disabled,false);
 // Reset an active roam; no continuation or late position response can restore it.
 context.fetch=async url=>ok(url==='/api/roam/route'?chunk:{});
 await run('startRoaming()'); assert.equal(run('roamActive'),true);
 assert.equal(elements.get('btn-route-stop').disabled,false);
 let resolvePosition;
 context.fetch=async url=>url==='/api/location/current'?new Promise(resolve=>{resolvePosition=resolve}):ok({});
 run('currentDeviceInfo={connected:true};');
 const poll=run('pollPosition()'); await tick();
 await run('clearLocation()');
 resolvePosition(ok({lat:48.86,lon:2.30}));await poll;
 assert.equal(run('roamActive'),false);assert.equal(run('activeSpoofLocation'),null);
 assert.equal(run('routePolling'),null);
 // A prefetch from an old session cannot be attached to the new session.
 let resolvePrefetch;
 run('roamActive=true; roamChunkCoords=[[2.30,48.86],[2.301,48.861]];');
 context.fetch=async()=>new Promise(resolve=>{resolvePrefetch=resolve});
 run('prefetchNextRoamChunk()');await tick();
 run('cancelMovementUI(); roamActive=true;');
 resolvePrefetch(ok(chunk));await tick();
 assert.equal(run('roamPrefetch'),null);
 // Joystick handoff relinquishes roam ownership, so route completion cannot restart it.
 context.fetch=async()=>ok({});
 run('activeSpoofLocation={lat:48.86,lon:2.30};');
 await run('joystickMove("n")');
 assert.equal(run('roamActive'),false);assert.equal(run('joystickDirection'),'n');
 await run('joystickStop()');
 // A device name containing "connect" is still a ready device.
 run('$("device-label").textContent="Connected iPhone"; currentDeviceInfo={connected:true};');
 assert.equal(run('deviceReady()'),true);
 // Preview follows the backend's accepted path even where the route crosses itself.
 let drawn, style;
 context.L={polyline:(points,options)=>{drawn=points;style=options;return {addTo(){return this},setLatLngs(points){drawn=points}}}};
 run('roamPathLine=null; roamChunkIdx=0; roamChunkCoords=[[2.30,48.86],[2.31,48.86],[2.30,48.86]];');
 run('updateRoamLookahead(48.86,2.30,[[2.30,48.86],[2.3001,48.86],[2.3002,48.8601]],0)');
 assert.equal(run('roamChunkIdx'),0);
 assert.equal(JSON.stringify(drawn),JSON.stringify([[48.86,2.30],[48.86,2.3001],[48.8601,2.3002]]));
 assert.equal(style.dashArray,undefined);
 assert.equal(style.smoothFactor,0);
 run('cancelMovementUI()');
 console.log('PASS: parallel roam preparation, stop/lookup failures, pending roam cancellation, reset, stale position/prefetch, joystick handoff, device readiness, driven-path preview.');
})().catch(error=>{console.error(error);process.exitCode=1});
