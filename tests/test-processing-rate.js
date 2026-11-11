const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/audio/generic-adapter-hooks.js'), 'utf8');

function createFixture(decoderRate, supportedRates) {
  const hooks = new Map();
  const choices = new Map();
  const queries = new Map();
  const contexts = new Map();
  const probes = [];
  const formats = [];
  const client = {toString: () => 'client'};
  const nullPointer = {isNull: () => true};
  const proposal = rate => ({add: offset => ({
    readU32: () => { assert.equal(offset, 4); return rate; },
    readU16: () => { assert.equal(offset, 2); return 2; }
  })});
  const context = vm.createContext({
    choices, queries, contexts, formatKinds: [{kind: 's24in32', bits: 32, validBits: 24}],
    nativeSources: {current: () => ({rate: decoderRate})},
    isMusicCaller: () => true, address: (value, slot) => slot,
    intercept: (address, name, callbacks) => hooks.set(name, callbacks),
    makeFormat: format => { formats.push(format); return format; },
    Memory: {alloc: () => ({readPointer: () => proposal(48000)})},
    taskFree: () => {}, ptr: () => nullPointer, send: () => {},
    raw: (value, slot) => slot === 8 ? () => 0 : (value, mode, format) => {
      assert.equal(mode, 1);
      probes.push(format.rate);
      return supportedRates.includes(format.rate) ? 0 : -2004287480;
    }
  });
  vm.runInContext(source.slice(source.indexOf('function chooseFormat('), source.indexOf('function setupRelease(')), context);
  vm.runInContext(source.slice(source.indexOf('function setupNegotiation('), source.indexOf('function rejectInitialize(')), context);
  contexts.set('client', {mode: 'exclusive', endpointMatch: true});
  context.setupNegotiation(client);
  return {
    choices, queries, contexts, probes, formats,
    query(rate) {
      const args = [client, nullPointer, proposal(rate), nullPointer];
      hooks.get('native-format').onEnter.call({returnAddress: nullPointer}, args);
      return args[2];
    }
  };
}

for (const rate of [96000, 192000]) {
  const fixture = createFixture(44100, [44100, 48000, 96000, 192000]);
  fixture.choices.set('client', {rate: 44100, sourceRate: 44100});
  assert.equal(fixture.query(rate).rate, rate, 'DSP request must replace early GetMixFormat decoder rate');
  assert.equal(fixture.choices.get('client').sourceRate, 44100);
  assert.equal(fixture.choices.get('client').processingRate, rate);
  assert.equal(fixture.query(rate).rate, rate, 'repeated negotiation must keep the selected format');
  assert.deepEqual(fixture.probes, [rate]);
}
for (const rate of [44100, 48000, 96000, 192000]) {
  const fixture = createFixture(rate, [44100, 48000, 96000, 192000]);
  assert.equal(fixture.query(rate).rate, rate, 'ordinary native-rate tracks must retain their rate');
}
const limitedDevice = createFixture(44100, [44100, 48000]);
assert.equal(limitedDevice.query(192000).rate, 44100, 'unsupported DSP rate must use a supported format');
assert.equal(limitedDevice.choices.get('client').processingRate, 192000);
const shared = createFixture(44100, [44100, 48000, 192000]);
shared.contexts.set('client', {mode: 'shared', endpointMatch: true});
shared.query(192000);
assert.equal(shared.choices.size, 0, 'shared output must bypass exclusive negotiation');
console.log('PASS: DSP rates, early mix proposal, repeated query, native rates, limited device and shared output');
