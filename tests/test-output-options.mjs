import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const root = new URL('../src/frontend/', import.meta.url);
const injection = await fs.readFile(new URL('output-option-inject.js', root), 'utf8');
const installation = await fs.readFile(new URL('install_output_options.js', root), 'utf8');
const shared = 'WASAPI（共享）';
const exclusive = 'WASAPI（独占，自动采样率）';
const clone = value => JSON.parse(JSON.stringify(value));
const defaultDevice = { id: 0, deviceId: '', name: '默认输出设备' };
const a = { id: 1, deviceId: '{ENDPOINT-A}', name: 'USB DAC A' };
const b = { id: 2, deviceId: '{endpoint-b}', name: 'Speakers B' };
const c = { id: 3, deviceId: '{endpoint-c}', name: 'Unknown C' };

function catalog(devices = [a, b, c], defaultId = a.deviceId) {
  return { generation: 1, defaultDeviceId: defaultId, devices: devices.map(device => ({
    deviceId: device.deviceId, name: device.name,
    exclusiveSupported: device.deviceId === b.deviceId ? false :
      device.deviceId === c.deviceId ? null : true,
    formats: []
  })) };
}

async function harness({ devices = [defaultDevice, a, b, c], current = a,
  capabilities = catalog(), storage = {}, provideCapabilities = true } = {}) {
  const state = { devices: clone(devices), current: { type: 'Wasapi', ...clone(current) },
    catalog: clone(capabilities), native: [], modes: [], reads: 0, probes: 0, refreshes: 0,
    storage: { ...storage } };
  const audio = {
    async getOutputDevices() {
      state.reads += 1;
      return { currentDevice: clone(state.current), totalDevices: [
        { type: 'Wasapi', devices: clone(state.devices) },
        { type: 'DirectSound', devices: [{ id: 9, deviceId: 'ds', name: 'DirectSound' }] }
      ] };
    },
    async setOutputDevice(selection) {
      state.native.push(clone(selection));
      state.current = { type: selection.type, ...clone(selection.device) };
    }
  };
  const native = { AudioPlayer: audio, Bridge: {
    triggerRegisterCall() {
      state.refreshes += 1;
      if (state.onRefresh) state.onRefresh();
    }
  } };
  const require = id => { throw new Error('Unexpected factory execution: ' + id); };
  require.m = {}; require.c = {'native-random-2105': {exports: {default: native}}};
  const context = vm.createContext({
    window: { webpackJsonp: { push(chunk) { chunk[1][chunk[2][0][0]]({}, {}, require); } } },
    localStorage: {
      getItem: key => state.storage[key] ?? null,
      setItem: (key, value) => { state.storage[key] = value; }
    }
  });
  await vm.runInContext(injection, context);
  const api = context.window.__codexOutputOptions;
  const install = api.install;
  api.install = config => install({ ...config,
    selectMode: async (mode, device) => { state.modes.push({ mode, device: clone(device) }); },
    ...(provideCapabilities ? { getCapabilities: async devices => {
      state.probes += 1;
      assert(devices.every(device => device.type === 'Wasapi'));
      return clone(state.catalog);
    } } : {})
  });
  vm.runInContext(installation, context);
  api.install = install;
  return { state, audio, api };
}

function option(result, type, deviceId) {
  const group = result.totalDevices.find(item => item.type === type);
  const device = group && group.devices.find(item =>
    item.deviceId.replace(/::codex-exclusive$/, '').toLowerCase() === deviceId.toLowerCase());
  assert(device, `Missing ${type} option for ${deviceId}`);
  return { type, device };
}

export async function run() {
  const h = await harness();
  let list = await h.audio.getOutputDevices();
  assert.equal(h.state.native.length, 0, 'Install must not reopen until restoreMode');
  assert.equal(list.totalDevices.find(group => group.type === shared).devices.length, 4);
  assert.deepEqual(Array.from(list.totalDevices.find(group => group.type === exclusive).devices,
    device => device.deviceId), ['::codex-exclusive', '{ENDPOINT-A}::codex-exclusive']);
  assert(list.totalDevices.some(group => group.type === 'DirectSound'));
  const staleB = option(list, shared, b.deviceId);
  h.state.devices.find(device => device.deviceId === b.deviceId).id = 42;
  await h.audio.setOutputDevice(staleB);
  assert.equal(h.state.native.at(-1).device.id, 42, 'Re-enumerate mutable native index');
  assert.equal(h.state.modes.at(-1).device.deviceId, b.deviceId);
  assert.equal(h.state.modes.at(-1).device.type, 'Wasapi');
  list = await h.audio.getOutputDevices();
  await h.audio.setOutputDevice(option(list, exclusive, a.deviceId));
  await h.audio.setOutputDevice(option(await h.audio.getOutputDevices(), shared, b.deviceId));
  h.state.current = { ...a, type: 'Wasapi' };
  const restoredA = await h.api.restoreMode();
  assert.equal(restoredA.mode, 'exclusive');
  assert.equal(h.state.native.at(-1).device.deviceId, a.deviceId, 'Native ID has no suffix');
  assert.equal((await h.audio.getOutputDevices()).currentDevice.type, exclusive);
  list = await h.audio.getOutputDevices();
  await h.audio.setOutputDevice(option(list, exclusive, ''));
  assert.equal(h.state.modes.at(-1).device.deviceId, '', 'Bridge retains default intent');
  assert.equal(h.state.native.at(-1).device.deviceId, a.deviceId, 'Native receives actual Windows default');
  assert.equal((await h.audio.getOutputDevices()).currentDevice.deviceId, '::codex-exclusive');
  assert.equal(JSON.parse(h.state.storage['codex.ncm.outputModes.v4']).selectedKey, '@default');
  assert.equal(JSON.parse(h.state.storage['codex.ncm.outputModes.v4']).savedDefaultTarget, a.deviceId);
  let saved = JSON.parse(h.state.storage['codex.ncm.outputModes.v4']).modes;
  assert.equal(saved['@default'], 'exclusive');
  assert.equal(saved['endpoint:{endpoint-a}'], 'exclusive');
  assert.equal(saved['endpoint:{endpoint-b}'], 'shared');
  h.state.catalog.defaultDeviceId = b.deviceId;
  const defaultFallback = await h.api.restoreMode();
  assert.equal(defaultFallback.mode, 'shared');
  assert.equal(defaultFallback.desiredMode, 'exclusive');
  assert.equal(defaultFallback.preferenceKey, '@default');
  assert.equal(h.state.native.at(-1).device.deviceId, b.deviceId);
  assert.equal(JSON.parse(h.state.storage['codex.ncm.outputModes.v4']).modes['@default'], 'exclusive');
  const d = { id: 5, deviceId: '{endpoint-d}', name: 'New USB device' };
  h.state.devices.push(d);
  h.state.catalog.devices.push({ ...d, exclusiveSupported: true });
  h.api.refreshDevices();
  list = await h.audio.getOutputDevices();
  option(list, exclusive, d.deviceId);
  const disconnectedA = option(list, exclusive, a.deviceId);
  h.state.devices = h.state.devices.filter(device => device.deviceId !== a.deviceId);
  h.state.catalog.devices = h.state.catalog.devices.filter(device => device.deviceId !== a.deviceId);
  const calls = h.state.native.length;
  await assert.rejects(h.audio.setOutputDevice(disconnectedA), /断开/);
  assert.equal(h.state.native.length, calls);
  const following = await harness();
  await following.audio.setOutputDevice(option(await following.audio.getOutputDevices(), exclusive, ''));
  following.state.onRefresh = () => { following.audio.getOutputDevices().catch(() => {}); };
  following.state.catalog.defaultDeviceId = b.deviceId;
  const beforeFollow = following.state.native.length;
  following.api.refreshDevices();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(following.state.native.length, beforeFollow + 1, 'Refresh follows Windows default once');
  assert.equal(following.state.native.at(-1).device.deviceId, b.deviceId);
  assert.equal(following.state.modes.at(-1).device.deviceId, '');
  assert.equal((await following.audio.getOutputDevices()).currentDevice.deviceId, '');
  assert.equal(JSON.parse(following.state.storage['codex.ncm.outputModes.v4']).selectedKey, '@default');
  following.api.refreshDevices(); following.api.refreshDevices();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(following.state.native.length, beforeFollow + 1, 'Same target does not reopen recursively');
  await following.audio.setOutputDevice(option(await following.audio.getOutputDevices(), shared, a.deviceId));
  following.state.catalog.defaultDeviceId = c.deviceId;
  const explicitCalls = following.state.native.length;
  following.api.refreshDevices();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(following.state.native.length, explicitCalls, 'Explicit physical choice disables following');
  assert.equal((await following.audio.getOutputDevices()).currentDevice.deviceId, a.deviceId);
  const savedAlias = { version: 4, modes: { '@default': 'exclusive' },
    selectedKey: '@default', savedDefaultTarget: a.deviceId };
  const restarted = await harness({ current: a, capabilities: catalog([a, b, c], b.deviceId),
    storage: { 'codex.ncm.outputModes.v4': JSON.stringify(savedAlias) } });
  assert.equal((await restarted.api.restoreMode()).preferenceKey, '@default');
  assert.equal(restarted.state.native.at(-1).device.deviceId, b.deviceId);
  const otherLegacy = await harness({ current: { id: -1, deviceId: '', name: b.name },
    storage: { 'codex.ncm.outputModes.v4': JSON.stringify(savedAlias) } });
  assert.equal((await otherLegacy.audio.getOutputDevices()).currentDevice.deviceId, b.deviceId);
  assert.equal(JSON.parse(otherLegacy.state.storage['codex.ncm.outputModes.v4']).selectedKey,
    'endpoint:{endpoint-b}');
  const otherApi = await harness({ current: { type: 'DirectSound', id: -1, deviceId: '', name: 'Other' },
    storage: { 'codex.ncm.outputModes.v4': JSON.stringify(savedAlias) } });
  assert.equal((await otherApi.api.restoreMode()).restored, false);
  assert.equal(JSON.parse(otherApi.state.storage['codex.ncm.outputModes.v4']).selectedKey, null);
  const unavailable = await harness({ provideCapabilities: false });
  assert(!(await unavailable.audio.getOutputDevices()).totalDevices.some(group => group.type === exclusive));
  await assert.rejects(unavailable.audio.setOutputDevice({ type: exclusive, device: a }), /尚未确认/);
  const unrelated = await harness({ devices: [a], current: {
    id: -1, deviceId: '', name: 'Unrelated old device'
  } });
  assert.equal((await unrelated.api.restoreMode()).restored, false, 'No sole-device fallback');
  assert.equal(unrelated.state.native.length, 0);
  const partialName = await harness({ devices: [a], current: {
    id: -1, deviceId: '', name: 'USB DAC'
  } });
  assert.equal((await partialName.api.restoreMode()).restored, false, 'Partial vendor names never bind');
  const duplicate = await harness({ devices: [a, { ...b, name: a.name }], current: {
    id: -1, deviceId: '', name: a.name
  } });
  assert.equal((await duplicate.api.restoreMode()).restored, false, 'Ambiguous names stay unresolved');
  const legacy = await harness({ current: { id: -1, deviceId: '', name: a.name } });
  assert.equal((await legacy.audio.getOutputDevices()).currentDevice.deviceId, a.deviceId);
  const wrongId = await harness({ devices: [{ ...a, deviceId: '{new-id}' }], current: a });
  assert.equal((await wrongId.api.restoreMode()).restored, false, 'Stale IDs never remap by name');
  const legacyPreference = await harness({
    storage: { 'codex.ncm.outputMode.v3': 'exclusive' }
  });
  assert.equal((await legacyPreference.audio.getOutputDevices()).currentDevice.type, shared);
  await assert.rejects(legacyPreference.audio.setOutputDevice({
    type: 'WASAPI（独占，原采样率）', device: a
  }), /已更新/);
  return { passed: true, checks: [
    'capability filtering', 'per-endpoint preferences', 'default sentinel',
    'fresh indices and IDs', 'plug/unplug', 'unknown capabilities',
    'legacy canonicalization', 'ambiguous names', 'legacy private preferences ignored',
    'default physical routing', 'default UI alias', 'default change following',
    'explicit output stops following', 'reentrant refresh does not reopen'
  ] };
}

console.log(JSON.stringify(await run()));
