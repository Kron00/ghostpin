const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
function element() {
 const classes = new Set();
 const children = [], listeners = {};
 return {disabled:false,textContent:'',value:'',classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)},children, listeners, setAttribute(){},addEventListener:(name, fn)=>{listeners[name]=fn},querySelectorAll:()=>[],appendChild:child=>children.push(child),append:(...items)=>children.push(...items)};
}
const context = vm.createContext({console, URLSearchParams, AbortController, setTimeout,clearTimeout,setInterval,clearInterval, localStorage:{getItem:()=>null}, document:{createElement:()=>element(),addEventListener(){},getElementById:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)},querySelectorAll:()=>[],querySelector:()=>null}, window:{},fetch:async()=>({ok:true,json:async()=>({})})});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../static/js/app.js'),'utf8'),context);
vm.runInContext('var notices=[]; toast=(...args)=>notices.push(args);',context);
(async()=>{
 context.fetch=async()=>({ok:false,json:async()=>({error:'Device unavailable'})});
 await assert.rejects(vm.runInContext('requestMutation("/api/test")',context), /Device unavailable/);
 context.button=element(); context.reloaded=false;context.reload=()=>{context.reloaded=true};
 await vm.runInContext('deleteSavedItem(button,"/api/test",reload,"Deleted")',context);
 assert.equal(context.button.disabled,false);assert.equal(context.reloaded,false);
 assert.equal(vm.runInContext('notices.at(-1)[0]',context),'Device unavailable');
 await vm.runInContext('pauseRoute()',context);
 assert.equal(elements.get('btn-route-pause').classList.contains('hidden'),false);
 assert.equal(elements.get('btn-route-pause').disabled,false);
 context.fetch=async()=>({ok:true,json:async()=>({})});
 await vm.runInContext('pauseRoute()',context);
 assert.equal(elements.get('btn-route-pause').classList.contains('hidden'),true);
 assert.equal(elements.get('btn-route-resume').classList.contains('hidden'),false);
 await vm.runInContext('resumeRoute()',context);
 assert.equal(elements.get('btn-route-resume').classList.contains('hidden'),true);
 vm.runInContext('var invalidations=0; invalidateCalculatedRoute=()=>invalidations++; renderStops=()=>{}; routeStops=[{text:"A"},{text:"B"}]; reverseStops();',context);
 assert.equal(vm.runInContext('routeStops[0].text',context),'B');assert.equal(vm.runInContext('invalidations',context),1);
 context.calls=0;context.fetch=async()=>{context.calls++;return{ok:true,json:async()=>({})}};
 vm.runInContext('deviceReady=()=>true; startMovementTracking=()=>{}; joystickMove("n"); joystickMove("n");',context);
 await vm.runInContext('joystickCommand',context);assert.equal(context.calls,1);
 vm.runInContext('_activeKeys.add("n"); pollPosition=async()=>{}; stopMovementTracking=()=>{};',context);
 await vm.runInContext('joystickStop()',context);
 assert.equal(vm.runInContext('_activeKeys.size',context),0);assert.equal(vm.runInContext('joystickDirection',context),null);
 // Loading a profile must restore its controls and actual simulated position.
 vm.runInContext('map={flyTo(){}}; placeMarker=()=>{}; adoptDotAsRoamCentre=()=>{}; updateStatusBar=()=>{};',context);
 const profile={name:'Museum',lat:48.86,lon:2.34,speed:42,route_mode:'loop'};
 context.fetch=async url=>({ok:true,json:async()=>url.endsWith('/load')?{profile}:[profile]});
 await vm.runInContext('loadProfiles()',context);
 const profileRow=elements.get('profile-list').children.at(-1);
 await profileRow.listeners.click({target:profileRow});
 assert.equal(vm.runInContext('selectedSpeed',context),42);
 assert.equal(elements.get('route-mode').value,'loop');
 assert.equal(vm.runInContext('activeSpoofLocation.lat',context),48.86);
 // Route history restores the saved speed and playback mode as well as stops.
 vm.runInContext('clearRoutePoints=()=>{}; renderStopsOnMap=()=>{};',context);
 context.fetch=async()=>({ok:true,json:async()=>[{id:'route-1',name:'Museums',speed:9,mode:'pingpong',waypoints:[{lat:48.86,lng:2.34},{lat:48.87,lng:2.35}]}]});
 await vm.runInContext('loadRouteHistory()',context);
 const routeRow=elements.get('route-history-list').children.at(-1);
 routeRow.listeners.click({target:routeRow});
 assert.equal(vm.runInContext('selectedSpeed',context),9);
 assert.equal(elements.get('route-mode').value,'pingpong');
 assert.equal(vm.runInContext('routeStops.length',context),2);
 // Pausing a schedule sends an explicit false, and failures leave it retryable.
 context.schedule={id:'schedule-1',enabled:true}; context.button=element();
 let mutation;
 context.fetch=async(url,options)=>{if(options){mutation=JSON.parse(options.body);return{ok:true,json:async()=>({})}} return{ok:true,json:async()=>[]}};
 await vm.runInContext('toggleSchedule(button,schedule)',context);
 assert.equal(mutation.enabled,false);assert.equal(context.button.disabled,false);
 context.fetch=async()=>({ok:false,json:async()=>({error:'Schedule unavailable'})});
 await vm.runInContext('toggleSchedule(button,schedule)',context);
 assert.equal(context.button.disabled,false);
 assert.equal(vm.runInContext('notices.at(-1)[0]',context),'Schedule unavailable');
 console.log('PASS: mutation failures, pause/resume, route invalidation, joystick release, profile/route restoration, schedule toggle.');
})().catch(e=>{console.error(e);process.exitCode=1});
