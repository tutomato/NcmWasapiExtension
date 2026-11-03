import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';

let sourceUrl = new URL('../src/frontend/output-option-inject.js', import.meta.url);
try { await fs.access(sourceUrl); } catch (_) { sourceUrl = new URL('output-option-inject.js', import.meta.url); }
const injection = await fs.readFile(sourceUrl, 'utf8');
const checks = [];

function fixture({runtime = 'webpack4', nativeId = 'ncm-native-1138', storeId = 'store-241',
  alias = 'default', omitNative = false, omitStore = false, badDto = false,
  duplicateNative = false, duplicateStore = false, noCache = false} = {}) {
  const state = { volume: .27, notifications: 0, sets: [], factoryCalls: [] };
  const audio = {
    async getOutputDevices() { return badDto ? {totalDevices:{}} : {
      currentDevice: {type:'Wasapi', id:1, deviceId:'A',name:'DAC'},
      totalDevices:[{type:'Wasapi',devices:[{id:1, deviceId:'A',name:'DAC'}]}]}; },
    async setOutputDevice(selection) { state.sets.push(selection); },
    setVolume(value) { state.volume = value; }
  };
  const native = {AudioPlayer:audio, Bridge:{triggerRegisterCall(){state.notifications++;}}};
  const store = {getStore() {return {playing:{playingVolume:state.volume}};}};
  const cache = {};
  const wrap = value => alias === 'root' ? value : {[alias]:value};
  if (!omitNative) cache[nativeId] = {exports:wrap(native)};
  if (!omitStore) cache[storeId] = {exports:wrap(store)};
  if (duplicateNative) cache.otherNative = {exports:{...native,AudioPlayer:{...audio}}};
  if (duplicateStore) cache.otherStore = {exports:{getStore(){return {playing:{playingVolume:.13}};}}};
  const require = id => {state.factoryCalls.push(id); throw Error('Application module execution forbidden');};
  require.m = {unloadedNative:()=> {throw Error('Must never run unloaded factories');}};
  if (!noCache) require.c = cache;
  const window = {};
  if (runtime === 'webpack4') window.webpackJsonp = {push(chunk){
    const id=chunk[2][0][0]; require.m[id]=chunk[1][id];
    if (require.c) require.c[id] = {exports:{}};
    chunk[1][id]({}, {}, require);
  }};
  else window.webpackChunkCloudMusic = {push(chunk){chunk[2](require);}};
  const context = vm.createContext({window, localStorage:{getItem(){return null;},setItem(){}}});
  return {context,state,audio,native,store,require,cache};
}

for (const runtime of ['webpack4','webpack5']) for (const alias of ['root','a','default','randomAlias']) {
  const h=fixture({runtime,alias,nativeId:'renumbered-'+runtime,storeId:'arbitrary-'+alias});
  const originalGet=h.audio.getOutputDevices,originalSet=h.audio.setOutputDevice;
  const probe=await vm.runInContext(injection,h.context);
  const api=h.context.window.__codexOutputOptions;
  assert.equal(probe.ready,true); assert.equal(probe.moduleId,'renumbered-'+runtime);
  assert.equal(probe.storeModuleId,'arbitrary-'+alias); assert.equal(probe.hasVolumeReader,true);
  assert.equal(api.readVolume(),.27); assert.equal(api.requireVolumeSupport(),true);
  h.state.volume=.13; assert.equal(api.readVolume(),.13);
  assert.equal(h.audio.getOutputDevices,originalGet); assert.equal(h.audio.setOutputDevice,originalSet);
  assert.equal(h.state.sets.length,0); assert.equal(h.state.notifications,0);
  assert.equal(h.state.factoryCalls.length,0); assert(!Object.keys(h.require.m).some(x=>x.startsWith('codex_')));
  assert(!Object.keys(h.cache).some(x=>x.startsWith('codex_')));
  checks.push(runtime+'/'+alias+'/renumbered-exports');
}

for (const [options, pattern, label] of [
  [{omitNative:true},/audio contract.*unavailable/,'missing native'],
  [{duplicateNative:true},/native API is ambiguous/,'ambiguous native'],
  [{duplicateStore:true},/volume store is ambiguous/,'ambiguous store'],
  [{badDto:true},/DTO contract/,'wrong DTO'],
  [{noCache:true},/runtime is not ready/,'cache not exposed'],
]) {
  const h=fixture(options), originalGet=h.audio.getOutputDevices, originalSet=h.audio.setOutputDevice;
  await assert.rejects(vm.runInContext(injection,h.context),pattern);
  assert.equal(h.context.window.__codexOutputOptions,undefined);
  assert.equal(h.audio.getOutputDevices,originalGet); assert.equal(h.audio.setOutputDevice,originalSet);
  assert.equal(h.state.notifications,0); assert.equal(h.state.factoryCalls.length,0);
  checks.push(label+'/no-install');
}

{
  const h=fixture({omitStore:true});
  await vm.runInContext(injection,h.context);
  const api=h.context.window.__codexOutputOptions;
  assert.equal(api.readVolume(),null);
  assert.throws(()=>api.requireVolumeSupport(),/volume control contract/);
  assert.equal(h.state.notifications,0);
  checks.push('missing volume/no-install');
}

{
  const h=fixture();
  h.cache.sameNative={exports:{default:h.native}};
  h.cache.sameStore={exports:{anyAlias:h.store}};
  await vm.runInContext(injection,h.context);
  const api=h.context.window.__codexOutputOptions;
  h.state.volume=Infinity; assert.equal(api.readVolume(),null);
  h.state.volume=-.2; assert.equal(api.readVolume(),null);
  h.state.volume=2; assert.equal(api.readVolume(),null);
  h.state.volume=.4; assert.equal(api.readVolume(),.4);
  assert.equal(api.requireVolumeSupport(),true);
  checks.push('duplicate aliases and changed volume');
}
console.log(JSON.stringify({passed:true,checks},null,2));
