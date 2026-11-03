/* Standalone Frida helper. No hooks or audio-device calls.
 * Retain the returned object while any audio callback can use it.
 *
 * convert(srcFloat32, dst, samples, kindOrCode, gain = 1) -> post-gain,
 * pre-clipping absolute peak. samples = frames * channels for interleaved
 * mono/stereo. Input is unchanged unless src === dst (supported in-place).
 * silence(dst, samples, kindOrCode) zeroes the exact output byte count.
 * toFloat(src, dstFloat32, samples, inputKindOrCode) decodes PCM/float input
 * without gain. It returns the pre-clipping absolute input peak. Non-float
 * input requires disjoint buffers; only f32 accepts exact in-place operation.
 *
 * Formats / numeric codes:
 *   s24in32 = 1: signed PCM24, left aligned in 32 bits (low 8 bits zero).
 *   s32     = 2: signed PCM32, full 32 valid bits.
 *   s24     = 3: packed signed PCM24, little endian, 3 bytes/sample.
 *   s16     = 4: signed PCM16, little endian.
 *   f32     = 5: IEEE float32, finite and clipped to [-1, +1].
 * NaN/infinite source samples become zero. PCM ties round away from zero.
 */
function createConverter() {
  const codes = Object.freeze({ s24in32: 1, s32: 2, s24: 3, s16: 4, f32: 5 });
  const byteCounts = [0, 4, 4, 3, 2, 4];
  const module = new CModule(`
    typedef unsigned char u8;
    typedef unsigned int u32;
    typedef signed int i32;

    static u32 load_u32_le(const u8 *p) {
      return (u32)p[0] | ((u32)p[1] << 8) |
        ((u32)p[2] << 16) | ((u32)p[3] << 24);
    }

    static void store_le(u8 *p, u32 value, u32 bytes) {
      u32 j;
      for (j = 0; j < bytes; j++) p[j] = (u8)(value >> (j * 8));
    }

    static i32 quantize(double value, u32 bits) {
      double scale, highest, lowest, rounded;
      i32 top, bottom;
      if (bits == 16) {
        scale = 32768.0; top = 32767; bottom = -32768;
      } else if (bits == 24) {
        scale = 8388608.0; top = 8388607; bottom = -8388608;
      } else {
        scale = 2147483648.0;
        top = 2147483647; bottom = (-2147483647 - 1);
      }
      if (value >= 1.0) return top;
      if (value <= -1.0) return bottom;
      rounded = value * scale;
      rounded += rounded >= 0.0 ? 0.5 : -0.5;
      highest = (double)top; lowest = (double)bottom;
      /* Check BEFORE conversion so no double-to-int cast is out of range. */
      if (rounded >= highest) return top;
      if (rounded <= lowest) return bottom;
      return (i32)rounded;
    }

    double convert_samples(const void *source, void *destination,
                           u32 samples, u32 format, double gain) {
      const u8 *src = (const u8 *)source;
      u8 *dst = (u8 *)destination;
      union Sample { float f; u32 bits; } sample;
      u32 i, bytes;
      double peak = 0.0;
      bytes = format == 3 ? 3 : (format == 4 ? 2 : 4);
      for (i = 0; i < samples; i++) {
        double value, magnitude;
        i32 quantized;
        u32 word;
        sample.bits = load_u32_le(src + i * 4);
        value = ((sample.bits & 0x7f800000U) == 0x7f800000U)
          ? 0.0 : (double)sample.f * gain;
        magnitude = value < 0.0 ? -value : value;
        if (magnitude > peak) peak = magnitude;
        if (format == 5) {
          if (value > 1.0) value = 1.0;
          if (value < -1.0) value = -1.0;
          if (value == 0.0) word = 0; /* Canonical silence, including -0. */
          else { sample.f = (float)value; word = sample.bits; }
        } else if (format == 1) {
          quantized = quantize(value, 24);
          /* Product stays in int32 range, unlike a signed negative shift. */
          word = (u32)(quantized * 256);
        } else if (format == 2) {
          word = (u32)quantize(value, 32);
        } else if (format == 3) {
          word = (u32)quantize(value, 24);
        } else {
          word = (u32)quantize(value, 16);
        }
        store_le(dst + i * bytes, word, bytes);
      }
      return peak;
    }

    void silence_samples(void *destination, u32 samples, u32 format) {
      u8 *dst = (u8 *)destination;
      u32 bytes = format == 3 ? 3 : (format == 4 ? 2 : 4);
      u32 i, length = samples * bytes;
      for (i = 0; i < length; i++) dst[i] = 0;
    }

    double decode_samples(const void *source, void *destination,
                          u32 samples, u32 format) {
      const u8 *src = (const u8 *)source;
      u8 *dst = (u8 *)destination;
      union Sample { float f; u32 bits; } sample;
      u32 i, bytes = format == 3 ? 3 : (format == 4 ? 2 : 4);
      double peak = 0.0;
      for (i = 0; i < samples; i++) {
        const u8 *p = src + i * bytes;
        double value, magnitude;
        u32 word;
        if (format == 5) {
          sample.bits = load_u32_le(p);
          value = ((sample.bits & 0x7f800000U) == 0x7f800000U)
            ? 0.0 : (double)sample.f;
        } else if (format == 4) {
          word = (u32)p[0] | ((u32)p[1] << 8);
          value = ((word & 0x8000U) ? (double)word - 65536.0 :
            (double)word) / 32768.0;
        } else if (format == 3) {
          word = (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16);
          value = ((word & 0x800000U) ? (double)word - 16777216.0 :
            (double)word) / 8388608.0;
        } else {
          /* Signed32 and left-aligned24 share the same 2^31 denominator.
             Subtract in double to avoid implementation-defined unsigned casts. */
          word = load_u32_le(p);
          value = ((word & 0x80000000U) ? (double)word - 4294967296.0 :
            (double)word) / 2147483648.0;
        }
        magnitude = value < 0.0 ? -value : value;
        if (magnitude > peak) peak = magnitude;
        if (value > 1.0) value = 1.0;
        if (value < -1.0) value = -1.0;
        if (value == 0.0) sample.bits = 0;
        else sample.f = (float)value;
        store_le(dst + i * 4, sample.bits, 4);
      }
      return peak;
    }
  `);
  const nativeConvert = new NativeFunction(module.convert_samples, 'double',
    ['pointer', 'pointer', 'uint', 'uint', 'double']);
  const nativeSilence = new NativeFunction(module.silence_samples, 'void',
    ['pointer', 'uint', 'uint']);
  const nativeToFloat = new NativeFunction(module.decode_samples, 'double',
    ['pointer', 'pointer', 'uint', 'uint']);
  let disposed = false;

  function formatCode(format) {
    const code = typeof format === 'string' ? codes[format] : format;
    if (!Number.isInteger(code) || code < 1 || code > 5)
      throw new RangeError('Unsupported audio format: ' + String(format));
    return code;
  }
  function bytesPerSample(format) { return byteCounts[formatCode(format)]; }
  function validate(pointer, samples) {
    if (disposed) throw new Error('Converter has been disposed');
    if (!Number.isInteger(samples) || samples < 0 || samples > 0x1000000)
      throw new RangeError('Invalid sample count');
    if (pointer == null || typeof pointer.isNull !== 'function')
      throw new TypeError('Expected a NativePointer');
    if (samples !== 0 && pointer.isNull())
      throw new Error('Null audio buffer');
  }
  function convert(source, destination, samples, format, gain = 1) {
    const code = formatCode(format);
    validate(source, samples); validate(destination, samples);
    if (!Number.isFinite(gain) || gain < 0 || gain > 1)
      throw new RangeError('Gain must be finite and between zero and one');
    /* Equal pointers are safe because every output format uses <= 4 bytes.
       Reject other overlapping layouts rather than corrupt unread input. */
    if (samples && !source.equals(destination) &&
        source.compare(destination.add(samples * byteCounts[code])) < 0 &&
        destination.compare(source.add(samples * 4)) < 0)
      throw new RangeError('Partially overlapping audio buffers');
    return nativeConvert(source, destination, samples, code, gain);
  }
  function silence(destination, samples, format) {
    const code = formatCode(format);
    validate(destination, samples);
    nativeSilence(destination, samples, code);
  }
  function toFloat(source, destination, samples, format) {
    const code = formatCode(format);
    validate(source, samples); validate(destination, samples);
    const overlaps = samples &&
      source.compare(destination.add(samples * 4)) < 0 &&
      destination.compare(source.add(samples * byteCounts[code])) < 0;
    if (overlaps && !(code === codes.f32 && source.equals(destination)))
      throw new RangeError('PCM-to-float decoding requires separate audio buffers');
    return nativeToFloat(source, destination, samples, code);
  }

  function testToFloat() {
    const vectors = {
      s16: [-32768,32767,-1,0,1,-16384,16384,-32767,32766],
      s24: [-8388608,8388607,-1,0,1,-4194304,4194304,-8388607,8388606],
      s24in32: [-2147483648,2147483392,-256,0,256,-1073741824,
        1073741824,-2147483392,2147483136],
      s32: [-2147483648,2147483647,-1,0,1,-1073741824,1073741824,
        -2147483647,2147483646,123456789,-123456789],
      f32: [0,-0,1,-1,0.5,-0.5,2,-2,NaN,Infinity,-Infinity,
        2 ** -23,-(2 ** -23),3.4028234663852886e38]
    };
    function check(condition, text) {
      if (!condition) throw new Error('Input decoder test: ' + text);
    }
    function readPcm(p, kind, i) {
      if (kind === 's16') return p.add(i * 2).readS16();
      if (kind === 's24') {
        const at = p.add(i * 3);
        return ((at.readU8() | (at.add(1).readU8() << 8) |
          (at.add(2).readU8() << 16)) << 8) >> 8;
      }
      return p.add(i * 4).readS32();
    }
    const results = [];
    let total = 0;
    for (const kind of Object.keys(vectors)) {
      const values = vectors[kind], count = values.length;
      const bytes = bytesPerSample(kind), sourceLength = count * bytes;
      const inputAllocation = Memory.alloc(sourceLength + 34);
      inputAllocation.writeByteArray(new Array(sourceLength + 34).fill(0x6a));
      const input = inputAllocation.add(17);
      values.forEach((v, i) => {
        if (kind === 'f32') input.add(i * 4).writeFloat(v);
        else if (kind === 's16') input.add(i * 2).writeS16(v);
        else if (kind === 's24') input.add(i * 3)
          .writeByteArray([v & 255,(v >> 8) & 255,(v >> 16) & 255]);
        else input.add(i * 4).writeS32(v);
      });
      const original = Array.from(new Uint8Array(
        inputAllocation.readByteArray(sourceLength + 34))).join(',');
      const outputAllocation = Memory.alloc(count * 4 + 34);
      outputAllocation.writeByteArray(new Array(count * 4 + 34).fill(0x9c));
      const output = outputAllocation.add(17);
      const peak = toFloat(input, output, count, codes[kind]);
      const denominator = kind === 's16' ? 32768 : kind === 's24' ?
        8388608 : 2147483648;
      values.forEach((v, i) => {
        const value = kind === 'f32' ? (Number.isFinite(v) ?
          Math.max(-1,Math.min(1,v)) : 0) : v / denominator;
        check(output.add(i * 4).readFloat() === Math.fround(value),
          kind + ' decoded sample ' + i);
      });
      check(peak === (kind === 'f32' ? 3.4028234663852886e38 : 1),
        kind + ' input peak');
      for (let i = 0; i < 17; i++) {
        check(outputAllocation.add(i).readU8() === 0x9c, kind + ' prefix');
        check(output.add(count * 4 + i).readU8() === 0x9c, kind + ' suffix');
      }
      check(Array.from(new Uint8Array(inputAllocation.readByteArray(
        sourceLength + 34))).join(',') === original, kind + ' changed input');
      const back = Memory.alloc(sourceLength);
      convert(output, back, count, kind, 1);
      values.forEach((v, i) => {
        if (kind === 'f32') check(back.add(i * 4).readFloat() ===
          output.add(i * 4).readFloat(), kind + ' sanitized roundtrip');
        else {
          const delta = Math.abs(readPcm(back,kind,i) - v);
          // Float32 cannot preserve every low PCM32 bit. Other PCM kinds are exact.
          check(delta <= (kind === 's32' ? 128 : 0), kind + ' roundtrip ' + i);
        }
      });
      let overlapRejected = false;
      try { toFloat(input, input, count, kind); }
      catch (_) { overlapRejected = true; }
      check(kind === 'f32' ? !overlapRejected : overlapRejected,
        kind + ' inplace restriction');
      toFloat(ptr(0),ptr(0),0,kind);
      results.push({kind,cases:count,peak,guards:true,inputUnchanged:true,
        roundtrip:true,roundtripTolerance:kind === 's32' ? 128 : 0,
        overlapPolicy:true,emptyBuffer:true});
      total += count;
    }
    return {passed:true,formats:results,arithmeticCasesTotal:total};
  }

  function test() {
    // Golden integer values, not values calculated with the converter's loop.
    // Columns: input, s16, s24, s32, expected sanitized/clipped float.
    const cases = [
      [0, 0, 0, 0, 0], [-0, 0, 0, 0, 0],
      [1, 32767, 8388607, 2147483647, 1],
      [-1, -32768, -8388608, -2147483648, -1],
      [0.5, 16384, 4194304, 1073741824, 0.5],
      [-0.5, -16384, -4194304, -1073741824, -0.5],
      [2, 32767, 8388607, 2147483647, 1],
      [-2, -32768, -8388608, -2147483648, -1],
      [2 ** -23, 0, 1, 256, 2 ** -23],
      [-(2 ** -23), 0, -1, -256, -(2 ** -23)],
      [2 ** -24, 0, 1, 128, 2 ** -24],
      [-(2 ** -24), 0, -1, -128, -(2 ** -24)],
      [2 ** -25, 0, 0, 64, 2 ** -25],
      [-(2 ** -25), 0, 0, -64, -(2 ** -25)],
      [NaN, 0, 0, 0, 0], [Infinity, 0, 0, 0, 0],
      [-Infinity, 0, 0, 0, 0],
      [2 ** -15, 1, 256, 65536, 2 ** -15],
      [-(2 ** -15), -1, -256, -65536, -(2 ** -15)],
      [2 ** -16, 1, 128, 32768, 2 ** -16],
      [-(2 ** -16), -1, -128, -32768, -(2 ** -16)],
      [2 ** -17, 0, 64, 16384, 2 ** -17],
      [-(2 ** -17), 0, -64, -16384, -(2 ** -17)],
      [2 ** -31, 0, 0, 1, 2 ** -31],
      [-(2 ** -31), 0, 0, -1, -(2 ** -31)],
      [2 ** -32, 0, 0, 1, 2 ** -32],
      [-(2 ** -32), 0, 0, -1, -(2 ** -32)],
      [2 ** -33, 0, 0, 0, 2 ** -33],
      [-(2 ** -33), 0, 0, 0, -(2 ** -33)],
      [1 - 2 ** -24, 32767, 8388607, 2147483520, 1 - 2 ** -24],
      [-1 + 2 ** -24, -32768, -8388608, -2147483520, -1 + 2 ** -24]
    ];
    const sampleCount = cases.length;
    const sourceAllocation = Memory.alloc(sampleCount * 4 + 32);
    sourceAllocation.writeByteArray(new Array(sampleCount * 4 + 32).fill(0xa7));
    const source = sourceAllocation.add(16);
    cases.forEach((item, i) => source.add(i * 4).writeFloat(item[0]));
    const sourceBefore = Array.from(new Uint8Array(
      sourceAllocation.readByteArray(sampleCount * 4 + 32)));
    function assert(condition, text) {
      if (!condition) throw new Error('Converter test: ' + text);
    }
    function decoded(destination, kind, i) {
      if (kind === 's16') return destination.add(i * 2).readS16();
      if (kind === 's24') {
        const p = destination.add(i * 3);
        const v = p.readU8() | (p.add(1).readU8() << 8) |
          (p.add(2).readU8() << 16);
        return (v << 8) >> 8;
      }
      if (kind === 'f32') return destination.add(i * 4).readFloat();
      return destination.add(i * 4).readS32();
    }
    function expected(item, kind) {
      if (kind === 's16') return item[1];
      if (kind === 's24') return item[2];
      if (kind === 's24in32') return item[2] * 256;
      if (kind === 's32') return item[3];
      return item[4];
    }
    const results = [];
    for (const kind of Object.keys(codes)) {
      const bytes = bytesPerSample(kind), length = sampleCount * bytes;
      const allocation = Memory.alloc(length + 34);
      allocation.writeByteArray(new Array(length + 34).fill(0x5b));
      // Intentionally unaligned destination exercises exact little-endian writes.
      const destination = allocation.add(17);
      const peak = convert(source, destination, sampleCount, kind, 1);
      assert(peak === 2, kind + ' peak');
      cases.forEach((item, i) => {
        const actual = decoded(destination, kind, i);
        assert(actual === expected(item, kind),
          kind + ' case ' + i + ' expected ' + expected(item, kind) + ', got ' + actual);
        if (kind === 's24in32')
          assert(destination.add(i * 4).readU8() === 0, 'low eight bits');
      });
      for (let i = 0; i < 17; i++) {
        assert(allocation.add(i).readU8() === 0x5b, kind + ' prefix canary');
        assert(destination.add(length + i).readU8() === 0x5b, kind + ' suffix canary');
      }
      if (kind === 's24') {
        assert(Array.from(new Uint8Array(destination.add(2 * 3).readByteArray(3)))
          .join(',') === '255,255,127', 'packed +1 bytes');
        assert(Array.from(new Uint8Array(destination.add(3 * 3).readByteArray(3)))
          .join(',') === '0,0,128', 'packed -1 bytes');
        assert(Array.from(new Uint8Array(destination.add(9 * 3).readByteArray(3)))
          .join(',') === '255,255,255', 'packed negative one LSB bytes');
      }
      const gainSource = Memory.alloc(8);
      gainSource.writeFloat(0.5); gainSource.add(4).writeFloat(-0.5);
      const gainPeak = convert(gainSource, destination, 2, codes[kind], 0.5);
      assert(gainPeak === 0.25, kind + ' gain peak');
      const gainExpected = kind === 's16' ? 8192 : kind === 's24' ? 2097152 :
        kind === 'f32' ? 0.25 : 536870912;
      assert(decoded(destination, kind, 0) === gainExpected &&
        decoded(destination, kind, 1) === -gainExpected, kind + ' gain values');
      // Two stereo frames: L/R ordering must be preserved at each sample width.
      const stereo = Memory.alloc(16);
      [1, -1, 0.5, -0.5].forEach((v, i) => stereo.add(i * 4).writeFloat(v));
      convert(stereo, destination, 2 * 2, kind, 1);
      [cases[2], cases[3], cases[4], cases[5]].forEach((item, i) =>
        assert(decoded(destination, kind, i) === expected(item, kind),
          kind + ' stereo order ' + i));
      // Largest finite IEEE float must clamp rather than be mistaken for Inf.
      stereo.writeU32(0x7f7fffff);
      const finitePeak = convert(stereo, destination, 1, kind, 1);
      assert(finitePeak === 3.4028234663852886e38 &&
        decoded(destination, kind, 0) === expected(cases[2], kind),
        kind + ' largest finite input');
      convert(source, destination, sampleCount, kind, 0);
      for (let i = 0; i < length; i++)
        assert(destination.add(i).readU8() === 0, kind + ' gain zero');
      destination.writeByteArray(new Array(length).fill(0xff));
      silence(destination, sampleCount, kind);
      for (let i = 0; i < length; i++)
        assert(destination.add(i).readU8() === 0, kind + ' silence');
      assert(destination.add(length).readU8() === 0x5b, kind + ' silence boundary');
      // Exact in-place conversion is supported for all formats, including packed24.
      const inplace = Memory.alloc(sampleCount * 4);
      Memory.copy(inplace, source, sampleCount * 4);
      convert(inplace, inplace, sampleCount, kind, 1);
      cases.forEach((item, i) => assert(decoded(inplace, kind, i) ===
        expected(item, kind), kind + ' inplace ' + i));
      convert(ptr(0), ptr(0), 0, kind);
      silence(ptr(0), 0, kind);
      results.push({kind, code: codes[kind], bytesPerSample: bytes,
        cases: sampleCount, peak, gainPeak, canaries: true, inplace: true,
        silence: true, emptyBuffer: true, stereoOrder: true, largestFinite: true});
    }
    const sourceAfter = Array.from(new Uint8Array(
      sourceAllocation.readByteArray(sampleCount * 4 + 32)));
    assert(sourceBefore.join(',') === sourceAfter.join(','), 'input was modified');
    const rejectionCases = [
      () => convert(source, source, 1, 'unknown'),
      () => convert(source, source, 1, 0),
      () => convert(source, source, 1, 's32', NaN),
      () => convert(source, source, 1, 's32', Infinity),
      () => convert(source, source, 1, 's32', -0.1),
      () => convert(source, source, 1, 's32', 1.1),
      () => convert(source, source, -1, 's32'),
      () => convert(source, source, 1.5, 's32'),
      () => convert(source, source, 0x1000001, 's32'),
      () => convert(ptr(0), source, 1, 's32'),
      () => convert(source, ptr(0), 1, 's32'),
      () => convert(source, source.add(1), 2, 's32')
    ];
    for (let i = 0; i < rejectionCases.length; i++) {
      let rejected = false;
      try { rejectionCases[i](); } catch (_) { rejected = true; }
      assert(rejected, 'invalid input ' + i + ' accepted');
    }
    return {passed: true, formats: results, arithmeticCasesPerFormat: sampleCount,
      arithmeticCasesTotal: sampleCount * results.length,
      inputUnchanged: true, invalidInputsRejected: rejectionCases.length,
      inputDecoder: testToFloat()};
  }

  return { convert, toFloat, silence, bytesPerSample, formatCode, codes, test,
    testToFloat, nativeConvert, nativeToFloat, nativeSilence,
    dispose() { if (!disposed) { disposed = true; module.dispose(); } }
  };
}
