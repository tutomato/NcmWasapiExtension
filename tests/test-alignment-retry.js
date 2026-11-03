// Isolated state/lifetime tests. No Frida, COM, devices, or player interaction.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const scope = vm.createContext({Date, Map, Number, Array, Uint8Array, JSON, Error});
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'src', 'audio', 'alignment-retry.js'), 'utf8'), scope);
const meta = {rate: 44100, channels: 2, kind: 's24in32', bits: 32, validBits: 24, legacy: false};
const flags = 0x40000;
const ALIGNED = 0x88890019 | 0;
const BUSY = 0x8889000a | 0;
function fixture(results = [0], frames = [441]) {
    let time = 0, count = 0;
    const calls = [];
    const transport = {
        formatBytes: x => String(x),
        getDevice: id => {calls.push(['device', id]); return {hr: 0, device: 'endpoint'};},
        activate: () => {const client = 'client' + ++count; calls.push(['activate', client]); return {hr: 0, client};},
        initialize: (client, format, flags, duration) => {
            calls.push(['initialize', client, duration]);
            return results.length > 1 ? results.shift() : results[0];
        },
        getBufferSize: client => {
            calls.push(['size', client]);
            return {hr: 0, frames: frames.length > 1 ? frames.shift() : frames[0]};
        },
        release: object => calls.push(['release', object])
    };
    return {calls, transport, advance: ms => time += ms,
        probe: scope.createAlignmentProbe(null, {transport, now: () => time})};
}

// Failed temporary client is fully released before replacement Activate;
// replacement and endpoint are released before application receives the plan.
{
    const f = fixture([ALIGNED, 0], [512, 512]);
    const answer = f.probe.prepare('DAC', 'format1', meta, flags);
    assert(answer.ok);
    assert.strictEqual(answer.duration, Math.round(512 * 10000000 / 44100));
    assert.strictEqual(answer.bufferFrames, 512);
    assert.strictEqual(answer.aligned, true);
    assert.deepStrictEqual(f.calls, [
        ['device', 'DAC'], ['activate', 'client1'], ['initialize', 'client1', 100000],
        ['size', 'client1'], ['release', 'client1'], ['activate', 'client2'],
        ['initialize', 'client2', answer.duration], ['size', 'client2'],
        ['release', 'client2'], ['release', 'endpoint']
    ]);
    const count = f.calls.length;
    assert.strictEqual(f.probe.prepare('dac', 'format1', meta, flags).cached, true);
    assert.strictEqual(f.calls.length, count);
    assert.strictEqual(f.probe.invalidate('DAC', meta, flags), 1);
    assert.strictEqual(f.probe.prepare('DAC', 'format1', meta, flags).cached, false);
}
// Busy and repeat alignment failures never enter the positive cache.
{
    const f = fixture([BUSY]);
    for (let i = 0; i < 2; i++) {
        const answer = f.probe.prepare('DAC', 'format1', meta, flags);
        assert.strictEqual(answer.ok, false);
        assert.strictEqual(answer.hr, '0x8889000a');
    }
    assert.strictEqual(f.calls.filter(x => x[0] === 'initialize').length, 2);
    assert.strictEqual(f.calls.filter(x => x[0] === 'release').length, 4);
}
{
    const f = fixture([ALIGNED]);
    const answer = f.probe.prepare('DAC', 'format1', meta, flags);
    assert.strictEqual(answer.ok, false);
    assert.strictEqual(f.calls.filter(x => x[0] === 'initialize').length, 2);
    assert.strictEqual(f.calls.filter(x => x[0] === 'release').length, 3);
}
// Exact format bytes and TTL affect caching; callers cannot corrupt cache data.
{
    const f = fixture();
    const answer = f.probe.prepare('DAC', 'format1', meta, flags);
    answer.duration = 999;
    assert.strictEqual(f.probe.prepare('DAC', 'format1', meta, flags).duration, 100000);
    f.probe.prepare('DAC', 'format2', meta, flags);
    f.advance(60000);
    assert.strictEqual(f.probe.prepare('DAC', 'format1', meta, flags).cached, false);
    assert.strictEqual(f.calls.filter(x => x[0] === 'initialize').length, 3);
}
// Native exceptions still run both client and endpoint cleanup.
{
    const f = fixture();
    f.transport.getBufferSize = () => {throw Error('native failure');};
    assert.throws(() => f.probe.prepare('DAC', 'format1', meta, flags), /native failure/);
    assert.deepStrictEqual(f.calls.slice(-2), [['release', 'client1'], ['release', 'endpoint']]);
}
// Invalid configuration has no COM effects.
{
    const f = fixture();
    assert.throws(() => f.probe.prepare('DAC', 'format1', meta, 0), /event flags/);
    assert.strictEqual(f.calls.length, 0);
}
// Respect a hardware minimum above 10ms before the first Initialize.
{
    const f = fixture([0], [882]);
    f.transport.getDevicePeriod = client => {
        f.calls.push(['period', client]);
        return {hr: 0, defaultDuration: 100000, minimumDuration: 200000};
    };
    const answer = f.probe.prepare('DAC', 'format1', meta, flags);
    assert(answer.ok);
    assert.strictEqual(answer.duration, 200000);
    assert.deepStrictEqual(f.calls.slice(1, 4), [
        ['activate', 'client1'], ['period', 'client1'], ['initialize', 'client1', 200000]
    ]);
    assert.strictEqual(f.probe.prepare('DAC', 'format1', meta, flags).cached, true);
    assert.strictEqual(f.calls.filter(x => x[0] === 'period').length, 1);
}
// A failed query does not use undefined output values; only actual successful
// initialization may be cached. A device's shorter minimum keeps the request.
{
    const f = fixture();
    f.transport.getDevicePeriod = () => ({hr: 0x88890004 | 0, minimumDuration: 900000000});
    const answer = f.probe.prepare('DAC', 'format1', meta, flags);
    assert(answer.ok);
    assert.strictEqual(answer.duration, 100000);
    assert.strictEqual(f.probe.prepare('DAC', 'format1', meta, flags).cached, true);
    f.probe.invalidate('DAC');
    f.transport.getDevicePeriod = () => ({hr: 0, minimumDuration: 50000});
    assert.strictEqual(f.probe.prepare('DAC', 'format1', meta, flags).duration, 100000);
}
// Invalid driver periods are bounded and clean up without opening a stream.
{
    const f = fixture();
    f.transport.getDevicePeriod = () => ({hr: 0, minimumDuration: 50000001});
    const answer = f.probe.prepare('DAC', 'format1', meta, flags);
    assert.strictEqual(answer.ok, false);
    assert.strictEqual(answer.stage, 'MinimumPeriodTooLarge');
    assert.strictEqual(f.calls.filter(x => x[0] === 'initialize').length, 0);
    assert.deepStrictEqual(f.calls.slice(-2), [['release', 'client1'], ['release', 'endpoint']]);
}
console.log('10 alignment/cache/lifetime/minimum-period scenarios passed; no device operations');
