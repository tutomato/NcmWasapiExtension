/* The offline scanner supplies this contract. Runtime memory and ABI must agree
 * before any source tracker hook is attached. No version string or RVA fallback. */
function resolveNativeSourceCompatibility(profile) {
  if (!profile || profile.ok !== true || profile.scheme !== 'ncm-ffmpeg58-thread-v1' ||
      profile.architecture !== 'x64' || Process.arch !== 'x64' || Process.pointerSize !== 8)
    throw Error('Unsupported native audio compatibility contract');
  const normalizeWindowsPath = value => {
    if (typeof value !== 'string' || !value || value.includes('\x00')) return null;
    let path = value.replace(/\//g, '\\');
    if (/^\\\\\?\\UNC\\/i.test(path)) path = '\\\\' + path.slice(8);
    else if (/^\\\\\?\\/.test(path)) path = path.slice(4);
    else if (/^\\\?\?\\UNC\\/i.test(path)) path = '\\\\' + path.slice(8);
    else if (/^\\\?\?\\/.test(path)) path = path.slice(4);
    let root, parts;
    if (/^[a-z]:\\/i.test(path)) {
      root = path.slice(0, 3);
      parts = path.slice(3).split(/\\+/).filter(Boolean);
    } else if (path.startsWith('\\\\')) {
      const all = path.slice(2).split(/\\+/).filter(Boolean);
      if (all.length < 2) return null;
      root = '\\\\' + all.slice(0, 2).join('\\') + '\\';
      parts = all.slice(2);
    } else return null;
    if (parts.some(part => part === '.' || part === '..' || part.includes(':')) ||
        /[\x00-\x1f]/.test(root)) return null;
    return (root + parts.join('\\')).replace(/\\+$/, parts.length ? '' : '\\').toLowerCase();
  };
  const clientDirectory = normalizeWindowsPath(profile.clientDir);
  if (!clientDirectory) throw Error('Invalid native audio client directory');
  const names = profile.modules;
  const roles = ['player', 'avcodec', 'avutil', 'swresample'];
  if (!names || roles.some(role => typeof names[role] !== 'string' ||
      !/^[^\\/:\x00]+\.dll$/i.test(names[role])))
    throw Error('Invalid native audio module roles');
  if (names.player.toLowerCase() !== 'cloudmusic.dll' ||
      new Set(roles.map(role => names[role].toLowerCase())).size !== roles.length)
    throw Error('Ambiguous native audio module roles');
  const modules = {};
  for (const role of roles) {
    const module = Process.getModuleByName(names[role]);
    if (!module || module.name.toLowerCase() !== names[role].toLowerCase())
      throw Error('Native audio module is not loaded: ' + role);
    const loadedPath = normalizeWindowsPath(module.path);
    const separator = loadedPath ? loadedPath.lastIndexOf('\\') : -1;
    const loadedDirectory = loadedPath && separator >= 0
      ? normalizeWindowsPath(loadedPath.slice(0, separator + 1)) : null;
    if (!loadedDirectory || loadedDirectory !== clientDirectory)
      throw Error('Native audio module directory mismatch: ' + role);
    modules[role] = module;
  }
  const requireExport = (role, name) => {
    const module = modules[role];
    const address = module.getExportByName(name);
    const owner = Process.findModuleByAddress(address);
    const range = Process.findRangeByAddress(address);
    if (!owner || !owner.base.equals(module.base) || !range ||
        !range.protection.includes('x'))
      throw Error('Native audio export ownership mismatch: ' + role + '/' + name);
    return address;
  };
  const contracts = {
    avcodec: {major: 58, version: 'avcodec_version', required: ['avcodec_open2', 'avcodec_receive_frame']},
    avutil: {major: 56, version: 'avutil_version', required: ['av_opt_get_int', 'av_frame_get_sample_rate']},
    swresample: {major: 3, version: 'swresample_version', required: ['swr_alloc_set_opts']}
  };
  const exports = {}, versions = {};
  // Resolve every required export before calling even the version-query functions.
  for (const [role, contract] of Object.entries(contracts)) {
    exports[role] = {};
    for (const name of [contract.version, ...contract.required])
      exports[role][name] = requireExport(role, name);
  }
  for (const [role, contract] of Object.entries(contracts)) {
    const value = new NativeFunction(exports[role][contract.version], 'uint', [])();
    const major = value >>> 16;
    if (major !== contract.major)
      throw Error('Unsupported FFmpeg ABI: ' + role + ' ' + major);
    versions[role] = value >>> 0;
  }
  const rva = profile.workerRva;
  const module = modules.player;
  const pattern = '48 89 4c 24 08 55 57 48 81 ec 08 01 00 00 48 8d 6c 24 20 48 8d 0d ?? ?? ?? ?? e8 ?? ?? ?? ?? 48 8b 85 00 01 00 00 48 89 45 08 48 83 7d 08 00 74 10 48 8b 45 08 48 8b 00 48 8b 4d 08 ff 50 08 eb 02 33 c0 48 8d a5 e8 00 00 00 5f 5d c3'.split(' ');
  if (!Number.isSafeInteger(rva) || rva < 0 || rva + pattern.length > module.size)
    throw Error('Invalid native output worker location');
  const worker = module.base.add(rva);
  const range = Process.findRangeByAddress(worker);
  if (!range || !range.protection.includes('x') ||
      worker.add(pattern.length).compare(range.base.add(range.size)) > 0)
    throw Error('Native output worker is not executable');
  const actual = new Uint8Array(worker.readByteArray(pattern.length));
  if (actual.length !== pattern.length || pattern.some((token, index) =>
      token !== '??' && actual[index] !== parseInt(token, 16)))
    throw Error('Native output worker changed after compatibility check');
  return {modules, exports, worker, workerRva: rva, versions};
}

function createNativeSourceTracker(){
 const compatibility = resolveNativeSourceCompatibility(globalThis.__ncmCompatibility);
 const threads=new Map(),codecRates=new Map();
 const avu=compatibility.modules.avutil,avc=compatibility.modules.avcodec;
 const opt=new NativeFunction(compatibility.exports.avutil.av_opt_get_int,'int',['pointer','pointer','int','pointer']);
 const ar=Memory.allocUtf8String('ar');
 const kernel=Process.getModuleByName('kernel32.dll'),ntdll=Process.getModuleByName('ntdll.dll');
 const getThreadId=new NativeFunction(kernel.getExportByName('GetThreadId'),'uint',['pointer']);
 const getProcessId=new NativeFunction(kernel.getExportByName('GetProcessId'),'uint',['pointer']);
 const resume=new NativeFunction(kernel.getExportByName('ResumeThread'),'uint',['pointer']);
 Interceptor.attach(compatibility.exports.avcodec.avcodec_open2,{
  onEnter(a){this.ctx=a[0];threads.delete(Process.getCurrentThreadId());codecRates.delete(this.ctx.toString());},
  onLeave(r){if(r.toInt32()<0)return;const outRate=Memory.alloc(8);if(opt(this.ctx,ar,0,outRate)!==0)return;const rate=Number(outRate.readS64());if(rate<8000||rate>384000)return;const tid=Process.getCurrentThreadId();const source={rate,codec:this.ctx.toString(),decoderThread:tid};threads.set(tid,source);codecRates.set(source.codec,rate);send({event:'decoded-source-format',...source});}
 });
 Interceptor.attach(ntdll.getExportByName('NtCreateThreadEx'),{
  onEnter(a){this.target=false;if(!a[4].equals(compatibility.worker))return;if(!a[3].equals(ptr(-1))&&getProcessId(a[3])!==Process.id)return;const access=a[1].toUInt32();if(!(access&2)||!(access&0x840))return;this.target=true;this.out=a[0];this.source=threads.get(Process.getCurrentThreadId());this.originalFlags=a[6].toUInt32();this.suspendedByUs=!(this.originalFlags&1);if(this.suspendedByUs)a[6]=ptr(this.originalFlags|1);},
  onLeave(r){if(!this.target||r.toInt32()<0)return;const handle=this.out.readPointer();try{const tid=getThreadId(handle);if(!tid)throw Error('output-thread-id');threads.delete(tid);if(this.source){threads.set(tid,{...this.source});send({event:'source-thread-binding',...this.source,outputThread:tid});}}catch(e){send({event:'audio-error',what:String(e),result:'failed'});}finally{if(this.suspendedByUs&&resume(handle)===0xffffffff)send({event:'audio-error',what:'resume-output-worker',result:'failed'});}}
 });
 Interceptor.attach(ntdll.getExportByName('NtTerminateThread'),{onEnter(a){if(a[0].isNull()||a[0].equals(ptr(-2)))threads.delete(Process.getCurrentThreadId());}});
 const decoded=new Map(),frameRate=new NativeFunction(compatibility.exports.avutil.av_frame_get_sample_rate,'int',['pointer']);
 Interceptor.attach(compatibility.exports.avcodec.avcodec_receive_frame,{onEnter(a){this.c=a[0].toString();this.f=a[1];},onLeave(r){if(r.toInt32()!==0)return;const rate=frameRate(this.f);if(rate>0&&decoded.get(this.c)!==rate){decoded.set(this.c,rate);if(codecRates.has(this.c)&&codecRates.get(this.c)!==rate)send({event:'audio-error',what:'decoded-rate-changed',result:'unsupported',rate,expected:codecRates.get(this.c)});else send({event:'decoded-rate-confirmed',codec:this.c,rate});}}});
 Interceptor.attach(compatibility.exports.swresample.swr_alloc_set_opts,{onEnter(a){send({event:'resampler-format',inputRate:a[6].toInt32(),outputRate:a[3].toInt32(),thread:Process.getCurrentThreadId()});}});
 send({event:'native-compatibility-validated',scheme:'ncm-ffmpeg58-thread-v1',workerRva:compatibility.workerRva,versions:compatibility.versions});
 return {current(){return threads.get(Process.getCurrentThreadId());},isMusicCaller(address){const owner=Process.findModuleByAddress(address);return !!owner&&owner.base.equals(compatibility.modules.player.base);}};
}
const nativeSources=createNativeSourceTracker();
