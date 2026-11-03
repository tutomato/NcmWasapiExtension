const conv = createConverter();
send({ event: 'converter-test', ...conv.test() });
const records = new Map(), renders = new Map(), events = new Map();
const contexts = new Map(), choices = new Map(), queries = new Map(), installed = new Set();
let selected = { mode: 'shared', deviceId: '', deviceName: '' }, selectionGeneration = 0;
let clientEpoch = 0, activeInfo = null, muted = true, gain = 1;
const audioIID = guid('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2');
const endpointIID = guid('1be09788-6894-4089-8586-9a2a6c265ac5');
const taskFree = new NativeFunction(ole.getExportByName('CoTaskMemFree'), 'void', ['pointer']);
const taskAlloc = new NativeFunction(ole.getExportByName('CoTaskMemAlloc'), 'pointer', ['ulong']);
const alignment = createAlignmentProbe(en);
const formatKinds = [
  { kind: 's24in32', bits: 32, validBits: 24 },
  { kind: 's32', bits: 32, validBits: 32 },
  { kind: 's24', bits: 24, validBits: 24 },
  { kind: 's16', bits: 16, validBits: 16 },
  { kind: 'f32', bits: 32, validBits: 32 }
];

function address(p, n) { return p.readPointer().add(n * 8).readPointer(); }
function raw(p, n, ret, args) {
  return new NativeFunction(address(p, n), ret, args, { traps: 'none' });
}
function intercept(p, key, callbacks) {
  const id = p.toString() + ':' + key;
  if (!installed.has(id)) { installed.add(id); Interceptor.attach(p, callbacks); }
}
function equalGuid(a, b) {
  for (let i = 0; i < 16; i += 4) if (a.add(i).readU32() !== b.add(i).readU32()) return false;
  return true;
}
function isMusicCaller(p) { return nativeSources.isMusicCaller(p); }
function deviceInfo(d) {
  const o = Memory.alloc(8);
  if (raw(d, 5, 'int', ['pointer', 'pointer'])(d, o) !== 0) return null;
  const idPtr = o.readPointer();
  const id = idPtr.readUtf16String(); taskFree(idPtr);
  if (raw(d, 0, 'int', ['pointer', 'pointer', 'pointer'])(d, endpointIID, o) !== 0) return null;
  const ep = o.readPointer(), flow = Memory.alloc(4);
  let render = false;
  try { render = raw(ep, 3, 'int', ['pointer', 'pointer'])(ep, flow) === 0 && flow.readU32() === 0; }
  finally { raw(ep, 2, 'uint', ['pointer'])(ep); }
  return { endpointId: id, render };
}
function retire(key) {
  const old = records.get(key), ctx = contexts.get(key);
  records.delete(key); contexts.delete(key); choices.delete(key); queries.delete(key);
  if (old) {
    for (const [k, v] of renders) if (v === old) renders.delete(k);
    for (const [k, v] of events) if (v === old) events.delete(k);
  }
  if (activeInfo && activeInfo.client === key && activeInfo.generation === ctx?.generation) activeInfo = null;
}
function bindClient(c, info, mode, generation) {
  const key = c.toString(); retire(key);
  contexts.set(key, { ...info, mode, generation: ++clientEpoch, selectionGeneration: generation });
  setupRelease(c); setupNegotiation(c); setupInitialize(c);
  send({ event: 'client-mode-bound', client: key, ...contexts.get(key) });
}
function watchDevice(d) {
  intercept(address(d, 3), 'activate', {
    onEnter(a) {
      this.audio = equalGuid(a[1], audioIID); this.out = a[4];
      if (!this.audio) return;
      this.info = deviceInfo(a[0]); this.generation = selectionGeneration;
      if (this.info) this.info.endpointMatch = !selected.deviceId || selected.deviceId.toLowerCase() === this.info.endpointId.toLowerCase();
      this.mode = selected.mode;
    },
    onLeave(r) {
      if (this.audio && this.info?.render && r.toInt32() === 0)
        bindClient(this.out.readPointer(), this.info, this.mode, this.generation);
    }
  });
}
function watchCollection(c) {
  intercept(address(c, 4), 'collection-item', {
    onEnter(a) { this.out = a[2]; },
    onLeave(r) { if (r.toInt32() === 0) watchDevice(this.out.readPointer()); }
  });
}
// Cover explicit devices, system defaults, enumeration, and future hotplug objects.
for (const n of [4, 5]) intercept(address(en, n), 'enumerator-device-' + n, {
  onEnter(a) {
    this.out = a[n === 4 ? 3 : 2];
    if (n === 5) {
      const id = a[1].readUtf16String(), suffix = /::codex-(exclusive|shared)$/.exec(id);
      if (suffix) { this.id = Memory.allocUtf16String(id.slice(0, suffix.index)); a[1] = this.id; }
    }
  },
  onLeave(r) { if (r.toInt32() === 0) watchDevice(this.out.readPointer()); }
});
intercept(address(en, 3), 'enumerator-collection', {
  onEnter(a) { this.out = a[3]; },
  onLeave(r) { if (r.toInt32() === 0) watchCollection(this.out.readPointer()); }
});
const bootstrapOut = Memory.alloc(8), countOut = Memory.alloc(4);
if (raw(en, 3, 'int', ['pointer', 'int', 'uint', 'pointer'])(en, 0, 1, bootstrapOut) === 0) {
  const col = bootstrapOut.readPointer(); watchCollection(col);
  try {
    if (raw(col, 3, 'int', ['pointer', 'pointer'])(col, countOut) === 0)
      for (let i = 0; i < countOut.readU32(); i++)
        if (raw(col, 4, 'int', ['pointer', 'uint', 'pointer'])(col, i, bootstrapOut) === 0) {
          const d = bootstrapOut.readPointer(); watchDevice(d); raw(d, 2, 'uint', ['pointer'])(d);
        }
  } finally { raw(col, 2, 'uint', ['pointer'])(col); }
}

function makeFormat(f, floatInput = false) {
  const kind = floatInput ? 'f32' : f.kind, bits = floatInput ? 32 : f.bits;
  const legacy = !floatInput && !!f.legacy, isFloat = kind === 'f32';
  const p = Memory.alloc(40); p.writeByteArray(new Uint8Array(40));
  p.writeU16(legacy ? (isFloat ? 3 : 1) : 65534); p.add(2).writeU16(f.channels);
  p.add(4).writeU32(f.rate); p.add(8).writeU32(f.rate * f.channels * bits / 8);
  p.add(12).writeU16(f.channels * bits / 8); p.add(14).writeU16(bits);
  p.add(16).writeU16(legacy ? 0 : 22);
  if (!legacy) {
    p.add(18).writeU16(floatInput ? 32 : f.validBits);
    p.add(20).writeU32(f.channels === 1 ? 4 : 3);
    Memory.copy(p.add(24), guid(isFloat ? '00000003-0000-0010-8000-00aa00389b71' : '00000001-0000-0010-8000-00aa00389b71'), 16);
  }
  return p;
}
function chooseFormat(c, sourceRate, channels) {
  let mixRate = 0;
  const o = Memory.alloc(8);
  if (raw(c, 8, 'int', ['pointer', 'pointer'])(c, o) === 0) {
    const f = o.readPointer(); mixRate = f.add(4).readU32(); taskFree(f);
  }
  const rates = [...new Set([sourceRate, mixRate, 48000, 44100, 96000, 88200, 192000, 176400, 32000, 24000, 16000, 8000].filter(x => x >= 8000 && x <= 768000))];
  const channelOrder = [...new Set([channels === 1 ? 1 : 2, 2, 1])];
  let last = 0x88890008;
  const supported = raw(c, 7, 'int', ['pointer', 'int', 'pointer', 'pointer']);
  for (const rate of rates) for (const ch of channelOrder) for (const kind of formatKinds)
    for (const legacy of kind.kind === 's24in32' ? [false] : [false, true]) {
      const f = { ...kind, rate, sourceRate, channels: ch, legacy, blockAlign: ch * kind.bits / 8 };
      const p = makeFormat(f), result = supported(c, 1, p, ptr(0)); last = result >>> 0;
      if (result === 0) return f;
      if ([0x88890004, 0x8889000a, 0x8889000e, 0x80070005, 0x80070490].includes(last))
        return { error: 'device-unavailable', result: '0x' + last.toString(16) };
    }
  return { error: 'no-supported-output-format', result: '0x' + last.toString(16) };
}
function setupRelease(c) {
  intercept(address(c, 2), 'client-release', {
    onEnter(a) { this.key = a[0].toString(); this.epoch = contexts.get(this.key)?.generation; },
    onLeave(r) { if (this.epoch !== undefined && r.toUInt32() === 0 && contexts.get(this.key)?.generation === this.epoch) retire(this.key); }
  });
}
function setupNegotiation(c) {
  intercept(address(c, 7), 'native-format', {
    onEnter(a) {
      const key = a[0].toString(), ctx = contexts.get(key), source = nativeSources.current();
      this.target = ctx?.mode === 'exclusive' && isMusicCaller(this.returnAddress) && !!source;
      if (!this.target) return;
      this.key = key; this.closest = a[3];
      let choice = choices.get(key);
      if (!choice) {
        choice = ctx.endpointMatch ? chooseFormat(a[0], source.rate, a[2].add(2).readU16()) : { error: 'selected-device-unavailable', result: '0x88890004' };
        choices.set(key, choice);
        send({ event: 'source-format-bound', client: key, endpointId: ctx.endpointId, ...choice });
      }
      this.choice = choice;
      if (!this.closest.isNull()) this.closest.writePointer(ptr(0));
      if (choice.error) { a[1] = ptr(1); this.invalid = Memory.alloc(40); this.invalid.writeByteArray(new Uint8Array(40)); a[2] = this.invalid; a[3] = ptr(0); return; }
      this.query = (queries.get(key) || 0) + 1; queries.set(key, this.query);
      if (!this.closest.isNull()) this.closest.writePointer(ptr(0));
      this.format = makeFormat(choice); a[1] = ptr(1); a[2] = this.format; a[3] = ptr(0);
    },
    onLeave(r) {
      if (!this.target) return;
      if (this.choice.error) { r.replace(ptr(0x88890008)); send({ event: 'output-error', ...this.choice }); return; }
      if (r.toInt32() !== 0) { send({ event: 'output-error', reason: 'format-query-failed', result: r.toString() }); return; }
      if (this.query === 1 && !this.closest.isNull()) {
        const f = taskAlloc(40);
        if (f.isNull()) throw Error('Cannot allocate negotiated audio format');
        Memory.copy(f, makeFormat(this.choice, true), 40); this.closest.writePointer(f); r.replace(1);
      }
      send({ event: 'native-format-negotiated', client: this.key, sourceRate: this.choice.sourceRate, rate: this.choice.rate, channels: this.choice.channels, kind: this.choice.kind, result: r.toString() });
    }
  });
  intercept(address(c, 8), 'native-mix-format', {
    onEnter(a) {
      const key = a[0].toString(), ctx = contexts.get(key), source = nativeSources.current();
      this.out = a[1]; this.choice = choices.get(key);
      this.floatInput = ctx?.mode === 'exclusive' && ctx.endpointMatch && isMusicCaller(this.returnAddress);
      if (!this.choice && ctx?.mode === 'exclusive' && isMusicCaller(this.returnAddress) && source) {
        this.choice = ctx.endpointMatch ? chooseFormat(a[0], source.rate, 2) : { error: 'selected-device-unavailable', result: '0x88890004' };
        choices.set(key, this.choice);
        send({ event: 'source-format-bound', client: key, endpointId: ctx.endpointId, ...this.choice });
      }
    },
    onLeave(r) {
      if (r.toInt32() === 0 && ((this.choice && !this.choice.error) || this.floatInput)) {
        const previous = this.out.readPointer();
        const proposal = this.choice && !this.choice.error ? this.choice : {
          rate: previous.add(4).readU32(), channels: previous.add(2).readU16() === 1 ? 1 : 2
        };
        const f = taskAlloc(40);
        if (f.isNull()) throw Error('Cannot allocate application mix format');
        Memory.copy(f, makeFormat(proposal, true), 40);
        taskFree(previous); this.out.writePointer(f);
        send({ event: 'app-mix-proposal', rate: proposal.rate, channels: proposal.channels });
      }
    }
  });
}
function rejectInitialize(call, a, reason, detail) {
  call.rejected = reason; call.detail = detail; a[1] = ptr(0x7fffffff);
}
function setupInitialize(c) {
  intercept(address(c, 3), 'initialize', {
    onEnter(a) {
      this.key = a[0].toString(); this.ctx = contexts.get(this.key);
      if (!this.ctx) return;
      this.shared = this.ctx.mode === 'shared';
      this.sharedInfo = { client: this.key, ...this.ctx, rate: a[5].add(4).readU32() };
      this.target = this.ctx.mode === 'exclusive';
      if (!this.target) return;
      if (!this.ctx.endpointMatch) { rejectInitialize(this, a, 'selected-device-unavailable', this.ctx); return; }
      if (!isMusicCaller(this.returnAddress) || a[1].toInt32() !== 0) {
        rejectInitialize(this, a, 'unsupported-initialize-caller', { caller: Process.findModuleByAddress(this.returnAddress)?.name, requestedMode: a[1].toInt32() }); return;
      }
      const f = a[5], tag = f.readU16(), bits = f.add(14).readU16(), type = tag === 65534 ? f.add(24).readU32() : tag;
      const choice = choices.get(this.key);
      if (!choice || choice.error) { rejectInitialize(this, a, 'source-format-unavailable', choice); return; }
      const inputKind = type === 3 && bits === 32 ? 'f32' : type === 1 ? ({16:'s16',24:'s24',32:'s32'})[bits] : undefined;
      const inputBlockAlign = f.add(12).readU16();
      if (!inputKind || f.add(2).readU16() !== choice.channels || f.add(4).readU32() !== choice.rate || inputBlockAlign !== choice.channels * bits / 8) {
        rejectInitialize(this, a, 'app-format-mismatch', { expected: choice, rate: f.add(4).readU32(), channels: f.add(2).readU16(), bits, type }); return;
      }
      this.s = { client: this.key, ...this.ctx, ...choice, inputKind, inputBlockAlign, gets: 0, releases: 0, frames: 0, errors: 0, peak: 0, bufferFrames: 0, credit: true };
      this.format = makeFormat(choice);
      const flags = a[2].toUInt32(); this.flags = flags;
      if (!(flags & 0x40000)) { rejectInitialize(this, a, 'unsupported-timer-driven-output', { flags }); return; }
      const prepared = alignment.prepare(this.ctx.endpointId, this.format, choice, flags, 100000);
      if (!prepared.ok) { rejectInitialize(this, a, 'exclusive-preflight-failed', prepared); return; }
      a[5] = this.format; a[1] = ptr(1); a[3] = ptr(prepared.duration); a[4] = ptr(prepared.duration);
      setupClient(a[0]);
      send({ event: 'exclusive-attempt', endpointId: this.ctx.endpointId, ...choice, duration: prepared.duration });
    },
    onLeave(r) {
      if (this.rejected) { r.replace(ptr(0x88890008)); send({ event: 'output-error', reason: this.rejected, detail: this.detail, endpointId: this.ctx.endpointId }); return; }
      if (this.shared) {
        if (r.toInt32() === 0) activeInfo = this.sharedInfo;
        send({ event: 'shared-result', ...this.sharedInfo, result: r.toString() }); return;
      }
      if (!this.target) return;
      send({ event: 'exclusive-result', endpointId: this.ctx.endpointId, result: r.toString() });
      if (r.toInt32() === 0) {
        records.set(this.key, this.s);
        activeInfo = { client: this.key, ...this.ctx, sourceRate: this.s.sourceRate, rate: this.s.rate, channels: this.s.channels, kind: this.s.kind, validBits: this.s.validBits };
      } else if ((r.toUInt32() === 0x88890019) && alignment.invalidate) alignment.invalidate(this.ctx.endpointId, this.s, this.flags);
    }
  });
}
function reportError(s, what, r) {
  s.errors++;
  const disconnected = [0x88890004, 0x88890026, 0x88890010].includes(r.toUInt32());
  if (disconnected) s.unavailable = true;
  if (s.errors < 8) send({ event: disconnected ? 'output-error' : 'audio-error',
    reason: disconnected ? 'device-unavailable' : what, what, endpointId: s.endpointId, result: r.toString() });
}
function setupClient(c) {
  intercept(address(c, 4), 'buffer-size', {
    onEnter(a) { this.s = records.get(a[0].toString()); this.out = a[1]; },
    onLeave(r) { if (this.s && r.toInt32() === 0) { this.s.bufferFrames = this.out.readU32(); send({ event: 'buffer-size', frames: this.s.bufferFrames }); } }
  });
  intercept(address(c, 6), 'padding', {
    onEnter(a) { this.s = records.get(a[0].toString()); this.out = a[1]; },
    onLeave(r) { if (this.s && r.toInt32() === 0) { this.s.pads = (this.s.pads || 0) + 1; this.out.writeU32(this.s.credit ? 0 : this.s.bufferFrames); } }
  });
  for (const [slot, name] of [[10, 'start'], [11, 'stop'], [12, 'reset'], [13, 'event-handle']])
    intercept(address(c, slot), name, {
      onEnter(a) { this.s = records.get(a[0].toString()); this.handle = a[1]; },
      onLeave(r) {
        if (!this.s) return;
        if (name === 'event-handle' && r.toInt32() === 0) {
          for (const [key, s] of events) if (s === this.s) events.delete(key);
          events.set(this.handle.toString(), this.s);
        }
        if (name === 'reset' && r.toInt32() === 0) this.s.credit = true;
        send({ event: name, result: r.toString() });
      }
    });
  intercept(address(c, 14), 'service', {
    onEnter(a) { this.s = records.get(a[0].toString()); this.id = a[1].readU32(); this.out = a[2]; },
    onLeave(r) {
      if (!this.s || r.toInt32() !== 0 || this.id !== 0xf294acfc) return;
      const render = this.out.readPointer(); renders.set(render.toString(), this.s); setupRender(render);
    }
  });
}
function setupRender(c) {
  intercept(address(c, 3), 'get-buffer', {
    onEnter(a) {
      this.s = renders.get(a[0].toString()); this.out = a[2]; this.frames = a[1].toUInt32();
      if (this.s && this.frames <= this.s.bufferFrames) a[1] = ptr(this.s.bufferFrames);
    },
    onLeave(r) {
      if (!this.s) return;
      if (r.toInt32() !== 0) { reportError(this.s, 'GetBuffer', r); return; }
      const s = this.s; s.unavailable = false; s.buffer = this.out.readPointer(); s.request = this.frames;
      if (!s.scratch) s.scratch = Memory.alloc(s.bufferFrames * s.inputBlockAlign);
      if (s.inputKind !== 'f32' && !s.floatScratch) s.floatScratch = Memory.alloc(s.bufferFrames * s.channels * 4);
      conv.silence(s.scratch, s.bufferFrames * s.channels, s.inputKind);
      this.out.writePointer(s.scratch); s.gets++;
    }
  });
  intercept(address(c, 4), 'release-buffer', {
    onEnter(a) {
      this.s = renders.get(a[0].toString()); if (!this.s) return;
      const s = this.s, frames = a[1].toUInt32(); let flags = a[2].toUInt32();
      if (s.unavailable) flags |= 2;
      else if (frames > s.request || frames > s.bufferFrames) {
        flags |= 2; reportError(s, 'ReleaseBuffer frame count', ptr(0x80070057));
      } else if (!(flags & 2) && frames > 0) {
        let source = s.scratch;
        if (s.inputKind !== 'f32') { conv.toFloat(s.scratch, s.floatScratch, frames * s.channels, s.inputKind); source = s.floatScratch; }
        s.peak = Math.max(s.peak, conv.convert(source, s.buffer, frames * s.channels, s.kind, gain));
        if (frames < s.bufferFrames)
          conv.silence(s.buffer.add(frames * s.blockAlign), (s.bufferFrames - frames) * s.channels, s.kind);
      } else flags |= 2;
      a[1] = ptr(s.bufferFrames); a[2] = ptr(muted ? flags | 2 : flags);
    },
    onLeave(r) {
      if (!this.s) return;
      this.s.credit = false;
      if (r.toInt32() === 0) { this.s.releases++; this.s.frames += this.s.bufferFrames; }
      if (r.toInt32() !== 0) reportError(this.s, 'ReleaseBuffer', r);
    }
  });
}
function grant(handle) {
  const s = events.get(handle.toString()); if (s) { s.credit = true; s.events = (s.events || 0) + 1; }
}
const nt = Process.getModuleByName('ntdll.dll');
Interceptor.attach(nt.getExportByName('NtWaitForSingleObject'), {
  onEnter(a) { this.handle = a[0]; }, onLeave(r) { if (r.toInt32() === 0) grant(this.handle); }
});
Interceptor.attach(nt.getExportByName('NtWaitForMultipleObjects'), {
  onEnter(a) {
    this.type = a[2].toUInt32(); const count = a[0].toUInt32(); this.handles = [];
    if (count <= 64) for (let i = 0; i < count; i++) this.handles.push(a[1].add(i * 8).readPointer());
  },
  onLeave(r) {
    const n = r.toUInt32();
    if (this.type === 0 && n === 0) this.handles.forEach(grant);
    else if (this.type === 1 && n < this.handles.length) grant(this.handles[n]);
  }
});
rpc.exports = {
  command(c) {
    if (c.action === 'select-mode') {
      if (!['shared', 'exclusive'].includes(c.value)) throw Error('Invalid output mode');
      if (c.deviceId !== undefined && (typeof c.deviceId !== 'string' || c.deviceId.length > 4096)) throw Error('Invalid endpoint');
      selected = { mode: c.value, deviceId: c.deviceId || '', deviceName: String(c.deviceName || '') };
      selectionGeneration++; send({ event: 'mode-selected', ...selected, selectionGeneration });
    }
    if (c.action === 'gain' && Number.isFinite(Number(c.value))) gain = Math.max(0, Math.min(1, Number(c.value)));
    if (c.action === 'mute') muted = !!c.value;
    if (c.action === 'reset-peak') for (const s of records.values()) s.peak = 0;
    return { ok: true, selectedMode: selected.mode, selectedDeviceId: selected.deviceId, selectionGeneration,
      activeMode: activeInfo?.mode || null, activeInfo, muted, gain,
      streams: Array.from(records, ([id, s]) => ({ id, ...s, buffer: undefined, scratch: undefined, floatScratch: undefined })) };
  }
};
send({ event: 'adapter-ready', genericDevices: true, muted });
