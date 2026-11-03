/* Native output choices for every currently enumerated WASAPI endpoint.
 * Evaluation is read-only; install() receives the acknowledged native bridge.
 */
(async function () {
  if (window.__codexOutputOptions) return window.__codexOutputOptions.probe();

  function captureRuntimes() {
    const runtimes = [];
    const seen = new Set();
    const names = Object.getOwnPropertyNames(window).filter(name =>
      name === 'webpackJsonp' || /^webpackChunk/.test(name));
    for (const name of names) {
      let chunks;
      try { chunks = window[name]; } catch (_) { continue; }
      if (!chunks || typeof chunks.push !== 'function') continue;
      let require;
      const id = 'codex_output_probe_' + Date.now() + '_' + Math.random().toString(36).slice(2);
      try {
        if (/^webpackChunk/.test(name)) {
          // Webpack 5 runtime callback; no application module is executed.
          chunks.push([[id], {}, webpackRequire => { require = webpackRequire; }]);
        } else {
          // Webpack 4 entry bootstrap. Its one temporary module only captures
          // the runtime and is removed immediately after evaluation.
          const modules = {};
          modules[id] = (module, exports, webpackRequire) => { require = webpackRequire; };
          chunks.push([[id], modules, [[id]]]);
        }
      } catch (_) { continue; }
      if (!require || typeof require !== 'function' || !require.c) continue;
      if (require.m) delete require.m[id];
      delete require.c[id];
      if (!seen.has(require)) { runtimes.push({ require, name }); seen.add(require); }
    }
    if (!runtimes.length) throw new Error('NCM compatible webpack runtime is not ready');
    return runtimes;
  }

  function findContracts(runtimes) {
    const nativeCandidates = new Map();
    const stores = new Map();
    let inspected = 0;
    for (const runtime of runtimes) {
      const cache = runtime.require.c;
      const modules = Object.keys(cache);
      if (modules.length > 50000) throw new Error('NCM runtime module cache is too large');
      for (const moduleId of modules) {
        const module = cache[moduleId];
        if (!module) continue;
        // Search only exports of already loaded modules. Never require every
        // factory in require.m: doing that can initialize unrelated app code.
        const queue = [{ value: module.exports, depth: 0 }];
        const seen = new Set();
        while (queue.length) {
          const { value, depth } = queue.shift();
          if ((!value || (typeof value !== 'object' && typeof value !== 'function')) || seen.has(value)) continue;
          seen.add(value);
          if (++inspected > 100000) throw new Error('NCM loaded export graph is too large');
          try {
            const audio = value.AudioPlayer;
            const bridge = value.Bridge;
            if (audio && bridge && typeof audio.getOutputDevices === 'function' &&
                typeof audio.setOutputDevice === 'function' &&
                typeof bridge.triggerRegisterCall === 'function') {
              const existing = nativeCandidates.get(audio);
              if (existing && existing.native.Bridge !== bridge)
                throw new Error('NCM native bridge is ambiguous');
              nativeCandidates.set(audio, { native: value, runtime, moduleId });
            }
            if (typeof value.getStore === 'function') {
              let state;
              try { state = value.getStore(); } catch (_) { state = null; }
              const volume = state && state.playing && state.playing.playingVolume;
              if (typeof volume === 'number' && Number.isFinite(volume) && volume >= 0 && volume <= 1)
                stores.set(value, { store: value, runtime, moduleId });
            }
          } catch (error) {
            if (String(error.message || error).includes('ambiguous')) throw error;
          }
          if (depth >= 2) continue;
          let keys;
          try { keys = Object.keys(value); } catch (_) { continue; }
          if (keys.length > 128) continue;
          for (const key of keys) {
            if (key === '__proto__' || key === 'prototype' || key === 'constructor') continue;
            try { queue.push({ value: value[key], depth: depth + 1 }); } catch (_) {}
          }
        }
      }
    }
    if (nativeCandidates.size !== 1)
      throw new Error(nativeCandidates.size ? 'NCM native API is ambiguous' : 'NCM native audio contract is unavailable');
    if (stores.size > 1) throw new Error('NCM playing volume store is ambiguous');
    return { native: [...nativeCandidates.values()][0], store: [...stores.values()][0] || null };
  }

  const contracts = findContracts(captureRuntimes());
  const require = contracts.native.runtime.require;
  const native = contracts.native.native;
  const audio = native.AudioPlayer;
  const originalGet = audio.getOutputDevices.bind(audio);
  const originalSet = audio.setOutputDevice.bind(audio);
  const copy = value => JSON.parse(JSON.stringify(value));
  const labels = {
    shared: 'WASAPI（共享）',
    exclusive: 'WASAPI（独占，自动采样率）'
  };
  let installed = false;
  let undo = null;
  let restoreMode = null;
  let inspectDevices = null;
  let reconcileDefault = null;

  function notifyDevices() {
    native.Bridge.triggerRegisterCall('AudioDeviceChanged', 'audioplayer');
  }

  function refreshDevices() {
    // The controller can request a refresh with the settings page closed.
    // Reconciliation is asynchronous; its own notifications never recurse.
    if (reconcileDefault) reconcileDefault().then(notifyDevices, notifyDevices);
    else notifyDevices();
  }

  let nativeEqType;
  function uiStore() {
    const store=contracts.store && contracts.store.store;
    const redux=store && store.app && store.app._store;
    return redux && typeof redux.dispatch==='function' && typeof redux.subscribe==='function' ? redux : null;
  }
  function standardEqType() {
    if(nativeEqType!==undefined)return nativeEqType;
    const matches=new Set();
    for(const module of Object.values(require.c)) {
      try {
        const values=[module.exports,...Object.values(module.exports||{})];
        for(const value of values)if(value && typeof value==='object' &&
          Number.isInteger(value.EQUALIZER_AE) && Number.isInteger(value.AUDIO_AE) && Number.isInteger(value.DEVICE_AE))matches.add(value.EQUALIZER_AE);
      } catch(_) {}
    }
    if(matches.size===1)nativeEqType=[...matches][0];
    return nativeEqType;
  }
  function readNativeEqState() {
    try {
      const setting=contracts.store.store.getStore().audioEffect.setting,type=standardEqType();
      return {available:!!uiStore() && type!==undefined, enabled:!!setting.isOn && setting.currentAudioEffect.type===type,
        standardSelected:setting.currentAudioEffect.type===type};
    } catch(_){return {available:false,enabled:false,standardSelected:false};}
  }

  const api = {
    readNativeEqState,
    subscribeNativeEq(callback) { const store=uiStore();return store?store.subscribe(callback):()=>{}; },
    async setNativeStandardEqEnabled(enabled) {
      if(typeof enabled!=='boolean')throw Error('Invalid native EQ switch');
      const state=readNativeEqState(),store=uiStore();
      if(!state.available)throw Error('Native equalizer switch contract unavailable');
      if(!state.standardSelected || state.enabled===enabled)return {changed:false};
      await store.dispatch({type:'audioEffect/switchNew',payload:{state:enabled}});
      if(readNativeEqState().enabled!==enabled)throw Error('Native equalizer switch did not finish');
      return {changed:true};
    },
    async openNativeEqualizer() {
      const store=uiStore();if(!store)throw Error('Native equalizer window contract unavailable');
      const state=contracts.store.store.getStore(),windows=state.app && state.app.subWindows && state.app.subWindows.audioEffect;
      if(windows && Object.keys(windows.windowMap||{}).length) {
        await store.dispatch({type:'app/modalRoute',payload:{route:'audioEffect',push:['/audioEffect/soundEffect']}});
      } else await store.dispatch({type:'app/modalOpen',payload:{route:'audioEffect',width:574,height:560,path:'/audioEffect/soundEffect'}});
      return true;
    },
    version: '5-adaptive-device-options',
    require,
    native,
    readVolume() {
      if (!contracts.store) return null;
      try {
        const state = contracts.store.store.getStore();
        const volume = state && state.playing && state.playing.playingVolume;
        return typeof volume === 'number' && Number.isFinite(volume) && volume >= 0 && volume <= 1
          ? volume : null;
      } catch (_) { return null; }
    },
    requireVolumeSupport() {
      if (typeof audio.setVolume !== 'function' || api.readVolume() === null)
        throw new Error('NCM volume control contract is unavailable; output extension was not installed');
      return true;
    },
    async probe() {
      const result = await originalGet();
      return {
        ready: true,
        installed,
        version: api.version,
        moduleId: contracts.native.moduleId,
        runtime: contracts.native.runtime.name,
        storeModuleId: contracts.store && contracts.store.moduleId,
        hasVolumeReader: api.readVolume() !== null,
        currentDevice: result.currentDevice,
        totalDevices: result.totalDevices,
        hasDeviceSetter: typeof audio.setOutputDevice === 'function',
        hasDeviceRefresh: typeof native.Bridge.triggerRegisterCall === 'function'
      };
    },
    install(config) {
      if (installed) return { installed: true };
      if (!config || typeof config.encodeDevice !== 'function')
        throw new Error('A native device-ID codec is required');
      if (typeof config.selectMode !== 'function')
        throw new Error('An acknowledged endpoint mode bridge is required');
      const storageKey = 'codex.ncm.outputModes.v4';
      const modes = Object.create(null);
      let selectedKey = null;
      let savedDefaultTarget = '';
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
        if (saved.version === 4 && saved.modes && typeof saved.modes === 'object') {
          for (const key of Object.keys(saved.modes)) {
            if ((key === '@default' || key.startsWith('endpoint:')) && labels[saved.modes[key]])
              modes[key] = saved.modes[key];
          }
          if (saved.selectedKey === '@default' ||
              (typeof saved.selectedKey === 'string' && saved.selectedKey.startsWith('endpoint:')))
            selectedKey = saved.selectedKey;
          if (typeof saved.savedDefaultTarget === 'string')
            savedDefaultTarget = saved.savedDefaultTarget;
        }
      } catch (_) {}

      const decodeId = value => config.decodeDeviceId
        ? String(config.decodeDeviceId(String(value || '')))
        : String(value || '').replace(/::codex-exclusive$/, '');
      const endpointId = device => decodeId(device.deviceId);
      const endpointKey = device => endpointId(device)
        ? 'endpoint:' + endpointId(device).toLowerCase() : '@default';
      const isWasapi = type => String(type || '').toLowerCase() === 'wasapi';
      const nameKey = name => String(name || '').normalize('NFKC')
        .toLowerCase().replace(/[\s()（）\[\]【】{}:：_-]+/g, '');
      const saveModes = () => localStorage.setItem(storageKey,
        JSON.stringify({ version: 4, modes, selectedKey, savedDefaultTarget }));
      let actionTail = Promise.resolve();
      function serialize(action) {
        const result = actionTail.then(action, action);
        actionTail = result.catch(() => {});
        return result;
      }

      function preference(device, currentHint) {
        const key = endpointKey(device);
        if (labels[modes[key]]) return modes[key];
        const detected = config.modeOfDevice && config.modeOfDevice(currentHint || device);
        return labels[detected] ? detected :
          (labels[config.defaultMode] ? config.defaultMode : 'shared');
      }

      function canonicalDevice(device, entries) {
        const physicalId = endpointId(device);
        if (physicalId) {
          return entries.find(entry =>
            endpointId(entry).toLowerCase() === physicalId.toLowerCase()) || null;
        }
        const defaults = entries.filter(entry => !endpointId(entry));
        // NCM's explicitly enumerated Windows default entry is id 0. A legacy
        // physical output with id -1/empty endpoint is resolved by name instead.
        if (device.id === 0) return defaults.length === 1 ? defaults[0] : null;
        const friendly = nameKey(device.name);
        if (!friendly) return null;
        const exact = entries.filter(entry => nameKey(entry.name) === friendly);
        return exact.length === 1 ? exact[0] : null;
      }

      async function snapshot() {
        const result = copy(await originalGet());
        const entries = [];
        for (const group of result.totalDevices || []) {
          if (!isWasapi(group.type)) continue;
          for (const device of group.devices || [])
            entries.push({ ...device, deviceId: endpointId(device), type: group.type });
        }
        let catalog = null;
        let capabilityError = null;
        if (typeof config.getCapabilities === 'function') {
          try {
            catalog = await config.getCapabilities(copy(entries));
          } catch (error) {
            capabilityError = String(error.message || error);
          }
        }
        // Unknown and failed probes stay unknown; they never enable a choice.
        function capability(device) {
          const physicalId = endpointId(device) || String(catalog && catalog.defaultDeviceId || '');
          if (!physicalId || !catalog || !Array.isArray(catalog.devices)) return null;
          return catalog.devices.find(record => String(record.deviceId || '').toLowerCase() ===
            physicalId.toLowerCase()) || null;
        }
        return { result, entries, catalog, capability, capabilityError };
      }

      function supportedMode(device, desired, view) {
        const capability = view.capability(device);
        return desired === 'exclusive' && capability && capability.exclusiveSupported === true
          ? 'exclusive' : 'shared';
      }

      function nativeSelection(device, mode) {
        const selection = {
          type: labels[mode], device: config.encodeDevice(copy(device), mode)
        };
        if (config.toNativeSelection)
          return config.toNativeSelection(selection, mode, device.type);
        const canonical = copy(device);
        delete canonical.type;
        canonical.deviceId = endpointId(canonical);
        return { type: device.type, device: canonical };
      }

      function defaultEntry(view) {
        const entries = view.entries.filter(device => !endpointId(device));
        return entries.length === 1 ? entries[0] : null;
      }

      function defaultTarget(view) {
        const physicalId = String(view.catalog && view.catalog.defaultDeviceId || '');
        return physicalId ? view.entries.find(device => endpointId(device) &&
          endpointId(device).toLowerCase() === physicalId.toLowerCase()) || null : null;
      }

      function sameEndpoint(left, right) {
        return !!left && !!right && String(left).toLowerCase() === String(right).toLowerCase();
      }

      function rememberSelection(device, target) {
        selectedKey = endpointKey(device);
        savedDefaultTarget = selectedKey === '@default' ? endpointId(target) : '';
        saveModes();
      }

      function currentChoice(view) {
        const current = view.result.currentDevice;
        const canonical = current && isWasapi(current.type)
          ? canonicalDevice(current, view.entries) : null;
        if (selectedKey !== '@default') return canonical;
        const alias = defaultEntry(view);
        const target = defaultTarget(view);
        let keepFollowing = !!current && isWasapi(current.type) && !!alias;
        if (keepFollowing) {
          const physicalId = endpointId(current);
          const resolvedId = canonical && endpointId(canonical);
          const explicitDefault = canonical && !resolvedId;
          const expected = sameEndpoint(physicalId || resolvedId, savedDefaultTarget);
          const alreadyFollowed = target && sameEndpoint(physicalId || resolvedId, endpointId(target));
          // A stale legacy name for a different output is evidence that the
          // native selection changed. Do not relabel it as Windows default.
          const legacyOther = !physicalId && !explicitDefault && !expected && !alreadyFollowed;
          const targetChanged = target && savedDefaultTarget &&
            !sameEndpoint(endpointId(target), savedDefaultTarget);
          keepFollowing = !legacyOther &&
            (explicitDefault || expected || alreadyFollowed || (!!physicalId && !!targetChanged));
        }
        if (keepFollowing) return alias;
        selectedKey = canonical ? endpointKey(canonical) : null;
        savedDefaultTarget = '';
        saveModes();
        return canonical;
      }

      async function applySelection(device, mode, view) {
        // NCM treats its empty-ID default entry as the first enumerated device.
        // Resolve Windows default ourselves, while the mode bridge keeps the
        // empty-ID alias so the backend can retain default-device intent.
        const target = endpointId(device) ? device : defaultTarget(view);
        if (!target) throw new Error('Windows 默认输出设备尚未就绪，请刷新设备列表。');
        await config.selectMode(mode, copy(device));
        const result = await originalSet(nativeSelection(target, mode));
        rememberSelection(device, target);
        return result;
      }

      reconcileDefault = () => serialize(async function () {
        if (!installed || selectedKey !== '@default') return { followed: false };
        const view = await snapshot();
        const current = currentChoice(view);
        if (selectedKey !== '@default' || !current) return { followed: false };
        const target = defaultTarget(view);
        if (!target || sameEndpoint(endpointId(target), savedDefaultTarget))
          return { followed: false };
        const desired = preference(current, view.result.currentDevice);
        const mode = supportedMode(current, desired, view);
        await applySelection(current, mode, view);
        return { followed: true, mode, deviceId: endpointId(target) };
      });

      async function get() {
        let view = await snapshot();
        if (selectedKey === '@default') {
          currentChoice(view);
          const target = defaultTarget(view);
          if (selectedKey === '@default' && target &&
              !sameEndpoint(endpointId(target), savedDefaultTarget)) {
            await reconcileDefault();
            view = await snapshot();
          }
        }
        const groups = (view.result.totalDevices || []).filter(group => !isWasapi(group.type));
        if (view.entries.length) groups.push({
          type: labels.shared,
          devices: view.entries.map(device => config.encodeDevice(copy(device), 'shared'))
        });
        const exclusive = view.entries.filter(device => {
          const capability = view.capability(device);
          return capability && capability.exclusiveSupported === true;
        });
        if (exclusive.length) groups.push({
          type: labels.exclusive,
          devices: exclusive.map(device => config.encodeDevice(copy(device), 'exclusive'))
        });
        view.result.totalDevices = groups;
        const current = view.result.currentDevice;
        if (current && isWasapi(current.type)) {
          const canonical = currentChoice(view);
          if (canonical) {
            const mode = supportedMode(canonical, preference(canonical, current), view);
            view.result.currentDevice = {
              ...config.encodeDevice(copy(canonical), mode), type: labels[mode]
            };
          }
        }
        return view.result;
      }

      async function set(selection) {
        let mode = Object.keys(labels).find(choice => labels[choice] === selection.type);
        if (selection.type === 'WASAPI（独占，原采样率）') {
          throw new Error('输出选项已更新，请刷新设备列表。');
        }
        if (!mode) {
          // Other APIs keep their native behavior while clearing exclusive intent.
          await config.selectMode('shared', { ...copy(selection.device), type: selection.type });
          const result = await originalSet(selection);
          selectedKey = null;
          savedDefaultTarget = '';
          saveModes();
          notifyDevices();
          return result;
        }
        const view = await snapshot();
        const canonical = canonicalDevice(selection.device, view.entries);
        if (!canonical) throw new Error('输出设备已断开，请重新选择。');
        if (mode === 'exclusive') {
          const capability = view.capability(canonical);
          if (!capability || capability.exclusiveSupported !== true)
            throw new Error('尚未确认此设备支持独占输出，请刷新设备列表。');
        }
        const result = await applySelection(canonical, mode, view);
        modes[endpointKey(canonical)] = mode;
        saveModes();
        notifyDevices();
        return result;
      }

      restoreMode = () => serialize(async function () {
        const view = await snapshot();
        const current = view.result.currentDevice;
        if (!current || !isWasapi(current.type)) {
          currentChoice(view);
          return { restored: false, reason: 'other-output' };
        }
        const canonical = currentChoice(view);
        if (!canonical) return { restored: false, reason: 'endpoint-unavailable-or-ambiguous' };
        const desired = preference(canonical, current);
        const mode = supportedMode(canonical, desired, view);
        await applySelection(canonical, mode, view);
        notifyDevices();
        return {
          restored: true, mode, desiredMode: desired,
          deviceId: endpointId(canonical), preferenceKey: endpointKey(canonical)
        };
      });
      inspectDevices = async function () {
        const view = await snapshot();
        return { catalog: view.catalog, capabilityError: view.capabilityError,
          selectedKey, savedDefaultTarget,
          devices: view.entries.map(device => ({ ...device,
            preferenceKey: endpointKey(device), desiredMode: preference(device),
            capability: view.capability(device) })) };
      };
      audio.getOutputDevices = get;
      audio.setOutputDevice = selection => serialize(() => set(selection));
      installed = true;
      undo = function () {
        audio.getOutputDevices = originalGet;
        audio.setOutputDevice = originalSet;
        installed = false;
        restoreMode = null;
        inspectDevices = null;
        reconcileDefault = null;
      };
      notifyDevices();
      return { installed: true, labels: copy(labels), storageKey };
    },
    async restoreMode() {
      return restoreMode ? restoreMode() : { restored: false, reason: 'not-installed' };
    },
    async inspectDevices() {
      return inspectDevices ? inspectDevices() : { devices: [], reason: 'not-installed' };
    },
    refreshDevices,
    uninstall() {
      if (undo) undo();
      return { installed };
    }
  };
  // Validate the read-only shape before exposing an installable adapter.
  const probe = await api.probe();
  if (!Array.isArray(probe.totalDevices) ||
      !probe.totalDevices.every(group => group && typeof group.type === 'string' && Array.isArray(group.devices)))
    throw new Error('NCM output-device DTO contract is unavailable');
  window.__codexOutputOptions = api;
  return probe;
})();
