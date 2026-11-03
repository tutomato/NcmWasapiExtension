window.__codexOutputOptions.install({
  defaultMode: 'shared',
  decodeDeviceId(value) {
    return String(value || '').replace(/::codex-exclusive$/, '');
  },
  encodeDevice(device, mode) {
    device.deviceId = String(device.deviceId || '').replace(/::codex-exclusive$/, '');
    if (mode === 'exclusive') device.deviceId += '::codex-exclusive';
    return device;
  },
  modeOfDevice(device) {
    return String(device.deviceId || '').endsWith('::codex-exclusive')
      ? 'exclusive' : undefined;
  },
  toNativeSelection(selection, mode, type) {
    const device = selection.device;
    delete device.type;
    device.deviceId = String(device.deviceId || '').replace(/::codex-exclusive$/, '');
    return { type, device };
  }
});
