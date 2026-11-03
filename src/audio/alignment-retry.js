/* Exclusive event buffer alignment preflight. No Start, player pointers, or hooks.
 * Call before the application's FIRST Initialize. Every temporary interface is
 * fully released before prepare() returns. The application remains responsible
 * for its own Initialize; positive capability/preflight results are not a lock.
 * https://learn.microsoft.com/windows/win32/api/audioclient/nf-audioclient-iaudioclient-initialize
 * NativeFunction traps:none: https://frida.re/docs/javascript-api/#nativefunction
 */
function createAlignmentProbe(en, options = {}) {
    const NOT_ALIGNED = 0x88890019;
    const E_INVALIDARG = 0x80070057;
    const EVENT_CALLBACK = 0x40000;
    const ttl = options.ttlMs === undefined ? 60000 : options.ttlMs;
    const now = options.now || Date.now;
    const cache = new Map();
    const hex = hr => '0x' + (hr >>> 0).toString(16).padStart(8, '0');
    const metadataKey = meta => JSON.stringify([
        meta.rate, meta.channels, meta.kind, meta.bits, meta.validBits, !!meta.legacy
    ]);
    const failure = (hr, stage, duration, extra = {}) => ({
        ok: false, hr: hex(hr), stage, duration, bufferFrames: null,
        cached: false, ...extra
    });

    function nativeTransport() {
        const functions = new Map();
        const iid = guid('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2');
        function call(object, index, result, args, values) {
            const entry = object.readPointer().add(index * Process.pointerSize).readPointer();
            const key = entry.toString() + ':' + result + ':' + args.join(',');
            let fn = functions.get(key);
            if (!fn) {
                fn = new NativeFunction(entry, result, ['pointer', ...args], {traps: 'none'});
                functions.set(key, fn);
            }
            return fn(object, ...values);
        }
        return {
            formatBytes(format) {
                const extra = format.add(16).readU16();
                if (extra > 256) throw Error('Unexpected audio format extension size');
                return Array.from(new Uint8Array(format.readByteArray(18 + extra)),
                    byte => byte.toString(16).padStart(2, '0')).join('');
            },
            getDevice(id) {
                const out = Memory.alloc(Process.pointerSize);
                out.writePointer(ptr(0));
                const hr = call(en, 5, 'int', ['pointer', 'pointer'],
                    [Memory.allocUtf16String(id), out]);
                return {hr, device: out.readPointer()};
            },
            activate(device) {
                const out = Memory.alloc(Process.pointerSize);
                out.writePointer(ptr(0));
                const hr = call(device, 3, 'int', ['pointer', 'uint', 'pointer', 'pointer'],
                    [iid, 23, ptr(0), out]);
                return {hr, client: out.readPointer()};
            },
            initialize(client, format, flags, duration) {
                return call(client, 3, 'int',
                    ['int', 'uint', 'int64', 'int64', 'pointer', 'pointer'],
                    [1, flags, duration, duration, format, ptr(0)]);
            },
            getBufferSize(client) {
                const out = Memory.alloc(4);
                out.writeU32(0);
                const hr = call(client, 4, 'int', ['pointer'], [out]);
                return {hr, frames: out.readU32()};
            },
            getDevicePeriod(client) {
                const defaults = Memory.alloc(8), minimum = Memory.alloc(8);
                defaults.writeS64(0); minimum.writeS64(0);
                const hr = call(client, 9, 'int', ['pointer', 'pointer'],
                    [defaults, minimum]);
                return {hr, defaultDuration: Number(defaults.readS64()),
                    minimumDuration: Number(minimum.readS64())};
            },
            release(object) {
                if (object && !object.isNull()) call(object, 2, 'uint', [], []);
            }
        };
    }
    // Optional transport/clock injection is used only by the isolated Node tests.
    const io = options.transport || nativeTransport();

    function perform(endpointId, format, meta, flags, initialDuration) {
        let device = null;
        let duration = initialDuration;
        try {
            const opened = io.getDevice(endpointId);
            device = opened.device;
            if ((opened.hr >>> 0) !== 0) return failure(opened.hr, 'GetDevice', duration);
            for (let attempt = 0; attempt < 2; attempt++) {
                let client = null;
                try {
                    const activated = io.activate(device);
                    client = activated.client;
                    if ((activated.hr >>> 0) !== 0)
                        return failure(activated.hr, 'Activate', duration);
                    if (attempt === 0 && typeof io.getDevicePeriod === 'function') {
                        const periods = io.getDevicePeriod(client);
                        if ((periods.hr >>> 0) === 0 &&
                                Number.isSafeInteger(periods.minimumDuration) &&
                                periods.minimumDuration > 0) {
                            if (periods.minimumDuration > 50000000)
                                return failure(E_INVALIDARG, 'MinimumPeriodTooLarge', duration);
                            duration = Math.max(duration, periods.minimumDuration);
                        }
                    }
                    const hr = io.initialize(client, format, flags, duration);
                    if ((hr >>> 0) === 0) {
                        const size = io.getBufferSize(client);
                        if ((size.hr >>> 0) !== 0)
                            return failure(size.hr, 'GetBufferSize', duration);
                        if (!Number.isInteger(size.frames) || size.frames <= 0 ||
                                size.frames > meta.rate * 5)
                            return failure(E_INVALIDARG, 'InvalidBufferFrames', duration);
                        return {ok: true, hr: hex(0), duration,
                            bufferFrames: size.frames, cached: false, aligned: attempt > 0};
                    }
                    if ((hr >>> 0) !== NOT_ALIGNED || attempt !== 0)
                        return failure(hr, 'Initialize', duration);
                    const size = io.getBufferSize(client);
                    if ((size.hr >>> 0) !== 0)
                        return failure(size.hr, 'AlignedGetBufferSize', duration);
                    if (!Number.isInteger(size.frames) || size.frames <= 0 ||
                            size.frames > meta.rate * 5)
                        return failure(E_INVALIDARG, 'InvalidAlignedFrames', duration);
                    duration = Math.round(size.frames * 10000000 / meta.rate);
                    if (!Number.isSafeInteger(duration) || duration <= 0 || duration > 50000000)
                        return failure(E_INVALIDARG, 'InvalidAlignedDuration', duration);
                } finally {
                    // Includes failed Initialize: the next attempt always uses a
                    // newly activated object after the previous one is released.
                    if (client) io.release(client);
                }
            }
            return failure(NOT_ALIGNED, 'AlignmentRetriesExhausted', duration);
        } finally {
            if (device) io.release(device);
        }
    }

    return {
        prepare(endpointId, formatPtr, formatMeta, flags, requestedDuration = 100000) {
            if (typeof endpointId !== 'string' || !endpointId)
                throw Error('Alignment preflight requires a physical endpoint ID');
            if (!formatMeta || !Number.isInteger(formatMeta.rate) ||
                    formatMeta.rate <= 0 || formatMeta.rate > 768000)
                throw Error('Invalid alignment preflight rate');
            if (!Number.isInteger(flags) || flags < 0 || flags > 0xffffffff ||
                    !(flags & EVENT_CALLBACK))
                throw Error('Alignment preflight requires exclusive event flags');
            if (!Number.isSafeInteger(requestedDuration) || requestedDuration <= 0 ||
                    requestedDuration > 50000000)
                throw Error('Invalid alignment preflight duration');
            const id = endpointId.toLowerCase();
            const metaKey = metadataKey(formatMeta);
            const key = JSON.stringify([id, metaKey, flags, requestedDuration,
                io.formatBytes(formatPtr)]);
            const hit = cache.get(key);
            if (hit && now() - hit.time < ttl) return {...hit.answer, cached: true};
            const answer = perform(endpointId, formatPtr, formatMeta, flags, requestedDuration);
            // perform() has released every temporary COM reference before this
            // point. Only verified successes enter the cache; busy errors retry.
            if (answer.ok) cache.set(key, {id, metaKey, flags, time: now(), answer: {...answer}});
            else cache.delete(key);
            return answer;
        },
        invalidate(endpointId, formatMeta, flags) {
            const id = endpointId.toLowerCase();
            const metaKey = formatMeta ? metadataKey(formatMeta) : null;
            let removed = 0;
            for (const [key, value] of cache) {
                if (value.id === id && (metaKey === null || value.metaKey === metaKey) &&
                        (flags === undefined || value.flags === flags)) {
                    cache.delete(key);
                    removed++;
                }
            }
            return removed;
        }
    };
}
