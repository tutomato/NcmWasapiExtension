// Pure native-contract tests: no Frida attachment, DLL loading or audio device use.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/audio/native-source.js'), 'utf8');
const guardSource = source.slice(0, source.indexOf('function createNativeSourceTracker'));
const signature = '48 89 4c 24 08 55 57 48 81 ec 08 01 00 00 48 8d 6c 24 20 48 8d 0d ?? ?? ?? ?? e8 ?? ?? ?? ?? 48 8b 85 00 01 00 00 48 89 45 08 48 83 7d 08 00 74 10 48 8b 45 08 48 8b 00 48 8b 4d 08 ff 50 08 eb 02 33 c0 48 8d a5 e8 00 00 00 5f 5d c3'.split(' ');
function fixture(options = {}) {
  const calls = [];
  const modules = [];
  let worker;
  const pattern = signature.map(x => x === '??' ? 0x99 : parseInt(x, 16));
  if (options.corruptWorker) pattern[60] ^= 1;
  class Pointer {
    constructor(value) { this.value = value; }
    add(value) { return new Pointer(this.value + value); }
    equals(value) { return this.value === value.value; }
    compare(value) { return Math.sign(this.value - value.value); }
    readByteArray(length) {
      assert.equal(this.value, worker.value);
      return Uint8Array.from(pattern.slice(0, length)).buffer;
    }
  }
  const roles = {
    player: ['cloudmusic.dll', []],
    avcodec: ['new-codec-name.dll', ['avcodec_version','avcodec_open2','avcodec_receive_frame']],
    avutil: ['new-utility-name.dll', ['avutil_version','av_opt_get_int','av_frame_get_sample_rate']],
    swresample: ['new-resampler-name.dll', ['swresample_version','swr_alloc_set_opts']]
  };
  const versions = {avcodec_version: 58 << 16, avutil_version: 56 << 16, swresample_version: 3 << 16};
  Object.assign(versions, options.versions);
  const addressVersions = new Map();
  let base = 0x100000;
  for (const [role, [name, names]] of Object.entries(roles)) {
    const exports = new Map(names.map((name, i) => [name, new Pointer(base + 0x100 + i * 0x20)]));
    for (const name of names) if (name in versions) addressVersions.set(exports.get(name).value, versions[name]);
    const directory = options.moduleDirectory ?? 'C:\\Program Files\\NetEase\\CloudMusic';
    const modulePath = role === options.wrongDirectoryRole ? 'C:\\Other Client\\' + name : directory + '\\' + name;
    modules.push({role, name, path: modulePath, base: new Pointer(base), size: 0x10000, exports,
      getExportByName(name) {
        if (options.missingExport === name) throw new Error('Missing ' + name);
        if (options.foreignExport === name) return modules[0].base.add(0x20);
        const value = exports.get(name);
        if (!value) throw new Error('Missing ' + name);
        return value;
      }});
    base += 0x100000;
  }
  const rva = options.workerRva ?? 0x2100;
  worker = modules[0].base.add(rva);
  const profile = {ok:true, scheme:'ncm-ffmpeg58-thread-v1', architecture:'x64',
    clientDir:'C:\\Program Files\\NetEase\\CloudMusic',
    modules:Object.fromEntries(Object.entries(roles).map(([role,[name]]) => [role,name])), workerRva:rva};
  Object.assign(profile, options.profile);
  const Process = {
    arch: options.arch ?? 'x64', pointerSize: 8,
    getModuleByName(name) {
      if (options.unloaded === name) throw new Error('Not loaded');
      return modules.find(x => x.name === name);
    },
    findModuleByAddress(address) { return modules.find(x => address.value >= x.base.value && address.value < x.base.value + x.size); },
    findRangeByAddress(address) {
      const module = this.findModuleByAddress(address);
      return module && {base:module.base,size:module.size,
        protection:options.nonExecutable && module.role === 'player' ? 'r--' : 'r-x'};
    }
  };
  function NativeFunction(address, result, args) {
    assert.equal(result, 'uint'); assert.equal(args.length, 0);
    return () => {
      calls.push(address.value);
      assert(addressVersions.has(address.value), 'Only known version query functions may run during validation');
      return addressVersions.get(address.value);
    };
  }
  const context = vm.createContext({Process, NativeFunction, Uint8Array, globalThis:{__ncmCompatibility:profile}});
  vm.runInContext(guardSource, context);
  return {resolve:() => context.resolveNativeSourceCompatibility(profile), context, calls, profile, worker};
}
let count = 0;
function ok(name, options, predicate) {
  const value = fixture(options); const result = value.resolve();
  assert(predicate(result, value), name); count++;
}
function rejected(name, options, pattern) {
  const value = fixture(options);
  assert.throws(value.resolve, pattern, name);
  if (options.noVersionCalls) assert.equal(value.calls.length, 0, name + ' called native code before path validation');
  // Evaluate the production entry as well: invalid contracts fail before Memory
  // or Interceptor is available, so no hook or source-state allocation occurs.
  assert.throws(() => vm.runInContext(source, value.context), pattern, name + ' entry');
  count++;
}
ok('renamed DLLs with supported ABI', {}, (r,f) => r.worker.equals(f.worker) && f.calls.length === 3);
ok('relocated worker RVA', {workerRva:0xa100}, (r,f) => r.workerRva === 0xa100 && r.worker.equals(f.worker));
ok('module directory case and separator normalization', {moduleDirectory:'c:/program files/NETEASE/cloudmusic',profile:{clientDir:'C:\\Program Files\\NetEase\\CloudMusic\\'}}, (r,f) => r.worker.equals(f.worker));
ok('verbatim DOS module path', {moduleDirectory:'\\\\?\\C:\\Program Files\\NetEase\\CloudMusic'}, (r,f) => r.worker.equals(f.worker));
ok('NT DOS profile prefix', {profile:{clientDir:'\\??\\C:\\Program Files\\NetEase\\CloudMusic'}}, (r,f) => r.worker.equals(f.worker));
ok('UNC verbatim normalization', {moduleDirectory:'\\\\?\\UNC\\server\\share\\CloudMusic',profile:{clientDir:'//SERVER/share/CloudMusic/'}}, (r,f) => r.worker.equals(f.worker));
ok('NT UNC module normalization', {moduleDirectory:'\\??\\UNC\\server\\share\\CloudMusic',profile:{clientDir:'\\\\server\\share\\CloudMusic'}}, (r,f) => r.worker.equals(f.worker));
ok('DOS root directory', {moduleDirectory:'C:',profile:{clientDir:'C:/'}}, (r,f) => r.worker.equals(f.worker));
ok('UNC share root directory', {moduleDirectory:'\\\\server\\share',profile:{clientDir:'//SERVER/share/'}}, (r,f) => r.worker.equals(f.worker));
rejected('absent client directory', {profile:{clientDir:undefined},noVersionCalls:true}, /client directory/);
rejected('relative client directory', {profile:{clientDir:'CloudMusic'},noVersionCalls:true}, /client directory/);
rejected('noncanonical client directory', {profile:{clientDir:'C:\\Other\\..\\CloudMusic'},noVersionCalls:true}, /client directory/);
for (const role of ['player','avcodec','avutil','swresample'])
  rejected('wrong module directory: ' + role, {wrongDirectoryRole:role,noVersionCalls:true}, /module directory mismatch/);
rejected('no profile', {profile:{ok:false}}, /compatibility contract/);
rejected('unknown scheme', {profile:{scheme:'future-unverified'}}, /compatibility contract/);
rejected('wrong architecture', {arch:'arm64'}, /compatibility contract/);
rejected('unknown codec ABI', {versions:{avcodec_version:59<<16}}, /Unsupported FFmpeg ABI/);
rejected('unknown utility ABI', {versions:{avutil_version:57<<16}}, /Unsupported FFmpeg ABI/);
rejected('unknown resampler ABI', {versions:{swresample_version:4<<16}}, /Unsupported FFmpeg ABI/);
rejected('missing getter export', {missingExport:'av_frame_get_sample_rate'}, /Missing/);
rejected('foreign export ownership', {foreignExport:'avcodec_open2'}, /ownership mismatch/);
rejected('worker byte change', {corruptWorker:true}, /worker changed/);
rejected('worker outside module', {workerRva:0xffff}, /worker location/);
rejected('non-executable worker', {nonExecutable:true}, /not executable/);
rejected('unsafe module filename', {profile:{modules:{player:'cloudmusic.dll',avcodec:'../codec.dll',avutil:'util.dll',swresample:'swr.dll'}}}, /module roles/);
rejected('duplicate module roles', {profile:{modules:{player:'cloudmusic.dll',avcodec:'x.dll',avutil:'x.dll',swresample:'y.dll'}}}, /Ambiguous/);
console.log(count + ' native compatibility scenarios passed; no native attachment or audio operations');
