"""Release controller: read-only compatibility check, explicit audio enable.

The check/failed-validation path imports no Frida, websocket, COM, or process
control modules, creates no files, and never stops an existing music client.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from installation import (find_manifest, locate_supported_client, user_data_paths,
                          validate_client)


def check_message(report):
    reasons = {
        'supported': '已识别兼容的音频引擎，启动时将继续检测输出界面。',
        'incompatible-engine': '未识别到兼容的音频引擎结构，当前保持普通输出。请更新扩展后重试。',
        'client-not-found': '未找到网易云音乐，请选择 cloudmusic.exe 的位置。',
        'no-supported-client': '未找到已适配的网易云版本，请选择受支持的客户端。',
        'unsupported-version': '未识别到兼容的音频结构，请更新扩展后重试。',
        'client-files-mismatch': '网易云文件与已验证版本不一致，请更新扩展或选择受支持的客户端。',
        'unsupported-architecture': '需要 64 位 x64 网易云客户端，当前客户端架构不受支持。',
        'unsupported-runtime-architecture': '需要使用扩展附带的 64 位运行环境。',
        'invalid-pe': '选择的文件不是有效的 Windows 网易云客户端。',
        'invalid-executable': '请选择网易云音乐的 cloudmusic.exe。',
        'invalid-manifest': '兼容性清单缺失或损坏，请重新安装扩展。',
        'discovery-error': '客户端路径设置无法读取，请修正设置或重新选择客户端。',
        'verification-error': '无法完成客户端文件校验，请检查文件是否完整、可读。',
        'runtime-resources-missing': '运行文件不完整，请使用构建后的安装包启动扩展。',
        'startup-error': '输出扩展无法启动，请检查运行环境和安装文件。',
    }
    return reasons.get(report.get('reason'), '无法启用当前客户端，请检查安装或更新扩展。')


def print_check(report):
    answer = dict(report, event='status', status='ready' if report.get('ok') else 'error',
                  state='ready' if report.get('ok') else 'error', active=False,
                  message=check_message(report))
    print(json.dumps(answer, ensure_ascii=False), flush=True)


def save_client_path(path, settings_path):
    """Persist only a verified path, preserving other user settings atomically."""
    target = Path(settings_path)
    try:
        settings = json.loads(target.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError('Settings must be a JSON object')
    settings['clientPath'] = str(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='settings-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def choose_debug_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='网易云 WASAPI 输出扩展')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='只读检查客户端兼容性')
    mode.add_argument('--enable', action='store_true', help='启用扩展并管理网易云播放进程')
    mode.add_argument('--attach', type=int, metavar='PID', help='接入本次正常启动的网易云进程')
    parser.add_argument('--client', help='cloudmusic.exe 或其安装目录')
    parser.add_argument('--debug-port', type=int)
    parser.add_argument('--startup-ready-event')
    parser.add_argument('--stop-event')
    parser.add_argument('--creation-time', type=int)
    args = parser.parse_args(argv)
    attach_options = None
    if args.attach is not None:
        if (not 0 < args.attach <= 0xffffffff or not args.client or
                args.creation_time is None or not 0 < args.creation_time < (1 << 64) or
                not args.debug_port or not 1024 <= args.debug_port <= 65535 or
                not args.startup_ready_event or not args.stop_event):
            parser.error('--attach needs client, creation-time, debug-port and startup/stop events')
        attach_options = {'pid': args.attach, 'creation_time': args.creation_time,
                          'ready_event': args.startup_ready_event, 'stop_event': args.stop_event}
    elif any(value is not None for value in (args.debug_port, args.startup_ready_event,
                                             args.stop_event, args.creation_time)):
        parser.error('Attach arguments require --attach')
    manifest = find_manifest()
    report = locate_supported_client(explicit=args.client, manifest=manifest)
    if not report.get('ok'):
        print_check(report)
        return 2
    # No args is also a read-only check; starting audio always requires --enable.
    if not args.enable and attach_options is None:
        print_check(report)
        return 0
    required = ('adapter.js', 'compatibility-guard.js', 'output-option-inject.js', 'install_output_options.js', 'equalizer-panel.js', 'equalizer-transport.js', 'equalizer-playbar.js', 'eq_settings.py')
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        print_check({'ok': False, 'reason': 'runtime-resources-missing', 'files': missing})
        return 2
    try:
        paths = user_data_paths(create=True)
        save_client_path(report['clientPath'], paths['settings'])
        return _run_enabled(report, paths, manifest,
                            args.debug_port if attach_options else choose_debug_port(),
                            attach_options=attach_options)
    except (Exception, KeyboardInterrupt) as error:
        print_check({'ok': False, 'reason': 'startup-error', 'error': str(error)})
        return 2


def _run_enabled(report, paths, manifest, debug_port, *, attach_options=None):
    import ctypes as C
    from ctypes import wintypes as W
    import hashlib,json,os,pathlib,queue,subprocess,sys,threading,time
    import frida

    ROOT=pathlib.Path(__file__).resolve().parent
    sys.path.insert(0,str(ROOT))
    from job_process import SuspendedProcess
    from cdp_bridge import start as start_output_bridge
    from device_capabilities import DeviceCatalog
    import eq_settings
    if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stdin, 'reconfigure'):sys.stdin.reconfigure(encoding='utf-8')
    attached = attach_options is not None
    K=C.WinDLL('kernel32',use_last_error=True)
    K.CreateJobObjectW.argtypes=[C.c_void_p,W.LPCWSTR];K.CreateJobObjectW.restype=W.HANDLE
    K.SetInformationJobObject.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD];K.SetInformationJobObject.restype=W.BOOL
    K.AssignProcessToJobObject.argtypes=[W.HANDLE,W.HANDLE];K.AssignProcessToJobObject.restype=W.BOOL
    K.TerminateJobObject.argtypes=[W.HANDLE,W.UINT];K.TerminateJobObject.restype=W.BOOL
    K.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];K.OpenProcess.restype=W.HANDLE
    K.CloseHandle.argtypes=[W.HANDLE];K.CloseHandle.restype=W.BOOL
    K.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)];K.QueryFullProcessImageNameW.restype=W.BOOL
    K.TerminateProcess.argtypes=[W.HANDLE,W.UINT];K.TerminateProcess.restype=W.BOOL
    K.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD];K.WaitForSingleObject.restype=W.DWORD
    class Basic(C.Structure):
        _fields_=[('process_time',C.c_longlong),('job_time',C.c_longlong),('flags',W.DWORD),('min_ws',C.c_size_t),('max_ws',C.c_size_t),('active',W.DWORD),('affinity',C.c_size_t),('priority',W.DWORD),('scheduling',W.DWORD)]
    class IO(C.Structure):
        _fields_=[('v',C.c_ulonglong*6)]
    class Limits(C.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',C.c_size_t),('job_memory',C.c_size_t),('peak_process',C.c_size_t),('peak_job',C.c_size_t)]

    LOG=pathlib.Path(paths['logs']);LOG.mkdir(parents=True,exist_ok=True)
    log=(LOG/'audio.jsonl').open('a',encoding='utf-8',buffering=1)
    lock=threading.Lock()
    def emit(message,state='starting',active=False,**extra):
        obj=dict(event='status',state=state,active=active,message=message,**extra)
        with lock:
            if sys.stdout is not None:print(json.dumps(obj,ensure_ascii=False),flush=True)
    def audit(obj):
        with lock:log.write(json.dumps(dict(time=time.time(),**obj),ensure_ascii=False)+'\n')
    def image(pid):
        h=K.OpenProcess(0x1000,False,pid)
        if not h:return ''
        try:
            buf=C.create_unicode_buffer(32768);n=W.DWORD(len(buf))
            return buf.value if K.QueryFullProcessImageNameW(h,0,buf,C.byref(n)) else ''
        finally:K.CloseHandle(h)
    def stop_existing(exe):
        for p in frida.get_local_device().enumerate_processes():
            if p.name.lower()=='cloudmusic.exe' and image(p.pid).lower()==str(exe).lower():
                h=K.OpenProcess(0x100001,False,p.pid)
                if h:
                    K.TerminateProcess(h,0);K.WaitForSingleObject(h,5000);K.CloseHandle(h)

    class CatalogPoller:
        """Keep COM polling off both the CDP callback and the Frida RPC thread.

        The cache lock protects an immutable JSON string only. snapshot() never
        waits for endpoint enumeration or format probes in DeviceCatalog.refresh().
        """
        def __init__(self, on_change=None, on_error=None, catalog=None, interval=2.0):
            self._catalog=catalog if catalog is not None else DeviceCatalog()
            self._on_change,self._on_error=on_change,on_error
            self._interval=max(0.05,float(interval))
            self._stop=threading.Event();self._ready=threading.Event()
            self._lock=threading.Lock()
            self._json=json.dumps({'devices':[],'defaultDeviceId':None,
                'generation':0,'ok':False,'errors':[],'initializing':True})
            self._thread=threading.Thread(target=self._run,name='ncm-device-catalog',daemon=True)

        def start(self):
            self._thread.start();return self

        def snapshot(self):
            with self._lock:encoded=self._json
            return json.loads(encoded)

        def wait_initial(self,timeout=3.0):
            return self._ready.wait(timeout)

        def _run(self):
            previous_generation=None;previous_error=None
            while not self._stop.is_set():
                try:
                    snapshot=self._catalog.refresh()
                    if not isinstance(snapshot,dict) or not isinstance(snapshot.get('devices'),list):
                        raise ValueError('Device catalog returned an invalid snapshot')
                    encoded=json.dumps(snapshot,ensure_ascii=False,allow_nan=False)
                    if self._stop.is_set():return
                    with self._lock:self._json=encoded
                    self._ready.set();previous_error=None
                    generation=snapshot.get('generation')
                    if generation!=previous_generation:
                        previous_generation=generation
                        if not self._stop.is_set() and self._on_change:
                            self._on_change(snapshot)
                except Exception as error:
                    # Keep the previous cache. A transient COM/service failure must
                    # not stop audio control or repeatedly flood the audit log.
                    message=str(error)
                    if (message!=previous_error and not self._stop.is_set()
                            and self._on_error):
                        self._on_error(message)
                    previous_error=message
                if self._stop.wait(self._interval):return

        def stop(self,timeout=3.0):
            self._stop.set()
            if self._thread.is_alive() and threading.current_thread() is not self._thread:
                self._thread.join(timeout)

    def hresult_ok(value):
        try:
            number=int(value,0) if isinstance(value,str) else int(value)
            return 0<=number<0x80000000
        except (TypeError,ValueError):return False

    def output_warning(payload):
        code=str(payload.get('result',payload.get('hr',''))).lower()
        reason=str(payload.get('reason',payload.get('what',''))).lower()
        if code=='0x8889000a' or 'busy' in reason:
            message='输出设备正被其他程序占用，请关闭占用后重新选择输出。'
        elif code in ('0x88890004','0x88890026','0x80070490') or any(
                word in reason for word in ('unplug','disconnect','missing','unavailable')):
            message='输出设备当前不可用，请连接设备或选择其他输出。'
        elif code=='0x8889000e':
            message='该设备当前不允许独占输出，请选择共享输出或其他设备。'
        elif code=='0x88890008' or any(word in reason for word in ('format','support','sample-rate','rate-not')):
            message='该设备不支持当前输出格式，请选择共享输出或其他设备。'
        else:message='当前音频输出未能打开，请在网易云中重新选择输出设备。'
        return message+' 若重新播放仍无声，请关闭并重开扩展。'

    def output_status(info,mode,catalog):
        """Format generic per-stream metadata without assuming a DAC or bit depth."""
        device_id=str(info.get('endpointId') or info.get('deviceId') or '')
        name=next((v.get('name') for v in catalog.get('devices',[])
            if str(v.get('deviceId','')).lower()==device_id.lower()),None) if device_id else None
        name=name or info.get('deviceName')
        prefix='WASAPI 独占' if mode=='exclusive' else 'WASAPI 共享'
        if name:prefix+=' · '+str(name)
        rate=info.get('rate');source=info.get('sourceRate')
        pieces=[]
        if isinstance(rate,(int,float)) and rate>0:
            if isinstance(source,(int,float)) and source>0 and source!=rate:
                pieces.append('%g → %g kHz'%(source/1000,rate/1000))
            else:pieces.append('%g kHz'%(rate/1000))
        kind=info.get('kind');bits=info.get('validBits')
        if not isinstance(bits,(int,float)) or bits<=0:
            bits={'s24in32':24,'s24':24,'s32':32,'s16':16,'f32':32}.get(kind)
        if bits:
            pieces.append('%g 位%s'%(bits,'浮点' if kind=='f32' else ' PCM' if kind else ''))
        channels=info.get('channels')
        if channels==1:pieces.append('单声道')
        elif channels==2:pieces.append('双声道')
        return prefix+(' · '+' / '.join(pieces) if pieces else '')

    def compact_stats(stats):
        answer={k:stats[k] for k in ('selectedMode','selectionGeneration','activeMode',
            'activeInfo','muted','gain') if k in stats}
        keep=('id','client','generation','mode','endpointId','sourceRate','rate',
            'channels','kind','validBits','inputKind','inputBlockAlign','bufferFrames','gets','releases','frames',
            'errors','peak','eqGeneration','eqProcessedFrames')
        answer['streams']=[{k:s[k] for k in keep if k in s}
            for s in stats.get('streams',[]) if isinstance(s,dict)]
        return answer

    commands=queue.Queue()
    stop_requested=threading.Event()
    managed_job=None
    hooks_may_be_loaded=False
    def request_stop():
        stop_requested.set()
        if managed_job and (not attached or hooks_may_be_loaded):K.TerminateJobObject(managed_job,0)
    def read_commands():
        try:
            for line in sys.stdin:
                try:
                    cmd=json.loads(line)
                    if cmd.get('action') in ('disable','exit'):request_stop()
                    commands.put(cmd)
                except (ValueError,TypeError):pass
        finally:
            request_stop();commands.put({'action':'exit'})

    def main():
        nonlocal managed_job,hooks_may_be_loaded
        exe=pathlib.Path(report['clientPath'])
        verified=validate_client(exe,manifest)
        if not verified.get('ok'):
            emit(check_message(verified),'error',False,reason=verified.get('reason'));return 2
        compatibility=verified.get('compatibility')
        if not compatibility or not compatibility.get('ok'):
            emit('没有经过验证的音频结构信息，正在保持普通输出。','error',False);return 2
        cfg={'arguments':['--remote-debugging-port='+str(debug_port),
            '--remote-debugging-address=127.0.0.1','--ncm-wasapi-managed-launch']}
        if not attached:threading.Thread(target=read_commands,daemon=True).start()
        session=script=job=process=bridge=catalog=signals=None
        gain=1.0;muted=False;active=False;ready=threading.Event();fatal=queue.Queue();ending=False;failure=None;selection_error=None
        rpc_lock=threading.RLock();player_exited=threading.Event()
        try:
            emit('正在自动接入网易云输出扩展……' if attached else '正在启动网易云输出扩展……')
            if attached:
                from attach_process import AttachedProcess,StartupSignals
                signals=StartupSignals(attach_options['pid'],attach_options['creation_time'],
                    attach_options['ready_event'],attach_options['stop_event'])
                process=AttachedProcess(attach_options['pid'],str(exe),attach_options['creation_time'])
            else:stop_existing(exe)
            def callbacks_allowed():
                return not ending and not player_exited.is_set() and not stop_requested.is_set()
            def catalog_changed(snapshot):
                if not callbacks_allowed():return
                audit({'event':'device-catalog-changed','generation':snapshot.get('generation'),
                    'defaultDeviceId':snapshot.get('defaultDeviceId'),'ok':snapshot.get('ok'),
                    'deviceCount':len(snapshot.get('devices',[])),
                    'devices':[{'deviceId':d.get('deviceId'),'name':d.get('name'),
                        'exclusiveSupported':d.get('exclusiveSupported'),
                        'formatCount':len(d.get('formats',[])),'probeState':d.get('probeState')}
                        for d in snapshot.get('devices',[])],
                    'errorCount':len(snapshot.get('errors',[]))})
                if callbacks_allowed() and bridge is not None:bridge.refreshDevices()
            def catalog_error(message):
                if callbacks_allowed():audit({'event':'device-catalog-error','message':message})
            catalog=CatalogPoller(catalog_changed,catalog_error).start()
            job=K.CreateJobObjectW(None,None)
            managed_job=job
            if not job:raise C.WinError(C.get_last_error())
            # For an existing ordinary player, job membership starts harmless.
            # Arm KILL_ON_JOB_CLOSE immediately before any hook can execute.
            limits=Limits();limits.basic.flags=0 if attached else 0x2000
            if not K.SetInformationJobObject(job,9,C.byref(limits),C.sizeof(limits)):raise C.WinError(C.get_last_error())
            if not attached:process=SuspendedProcess([str(exe),*cfg.get('arguments',[])],cwd=str(exe.parent))
            if not K.AssignProcessToJobObject(job,W.HANDLE(int(process._handle))):raise C.WinError(C.get_last_error())
            if not attached:process.resume()
            audit({'event':'job-assigned','pid':process.pid,'debugPort':debug_port,'attached':attached})
            for _ in range(100):
                if signals is not None and signals.stop_requested():raise RuntimeError('扩展接入已取消。')
                if process.poll() is not None:raise RuntimeError('网易云未能启动。')
                try:
                    session=frida.attach(process.pid)
                    # Wait for both modules to finish loading before creating COM objects.
                    required_modules=sorted(set(compatibility['modules'].values())|{'combase.dll'})
                    probe=session.create_script('const modules='+json.dumps(required_modules)+';rpc.exports={ready(){return modules.every(n=>!!Process.findModuleByName(n));}};')
                    probe.load();loaded=probe.exports_sync.ready();probe.unload()
                    if loaded:break
                    session.detach();session=None
                except frida.ProcessNotFoundError:pass
                time.sleep(0.1)
            if session is None:raise RuntimeError('等待网易云初始化超时。')
            def detached(reason,*args):
                if not ending:
                    if job and (not attached or hooks_may_be_loaded):K.TerminateJobObject(job,0)
                    if reason=='process-terminated':
                        if not stop_requested.is_set() and fatal.empty():player_exited.set()
                    else:fatal.put('音频接管连接中断。')
            session.on('detached',detached)
            def receive(msg,data):
                nonlocal active,selection_error
                if not callbacks_allowed():return
                audit(msg)
                if msg['type']=='error':
                    fatal.put('音频适配出现错误，正在恢复普通输出。')
                    if job:K.TerminateJobObject(job,0)
                payload=msg.get('payload',{})
                if payload.get('event')=='adapter-ready':ready.set()
                if payload.get('event')=='exclusive-result':
                    if hresult_ok(payload.get('result')):active=True;selection_error=None
                    else:active=False;selection_error=output_warning(payload)
                if payload.get('event')=='shared-result':
                    if hresult_ok(payload.get('result')):active=True;selection_error=None
                    else:active=False;selection_error=output_warning(payload)
                if payload.get('event') in ('output-error','unsupported-input'):
                    active=False;selection_error=output_warning(payload)
                if payload.get('event')=='audio-error':
                    fatal.put('当前音频格式或缓冲不兼容，正在恢复普通输出。')
                    if job:K.TerminateJobObject(job,0)
            runtime_contract=dict(compatibility,clientDir=str(exe.parent))
            contract_source='globalThis.__ncmCompatibility='+json.dumps(runtime_contract)+';\n'
            # This script defines the compatibility resolver and a read-only
            # RPC entry point only. No Interceptor, audio converter, COM
            # enumerator or source tracker may be part of this resource.
            guard=session.create_script(contract_source+(ROOT/'compatibility-guard.js').read_text(encoding='utf-8-sig'))
            try:
                guard.load()
                native_check=guard.exports_sync.verify()
                if (not isinstance(native_check,dict) or native_check.get('ok') is not True or
                        native_check.get('workerRva') != compatibility.get('workerRva')):
                    raise RuntimeError('当前运行时音频接口不兼容，保持普通输出。')
            finally:
                guard.unload()
            audit({'event':'native-preflight-passed','workerRva':native_check['workerRva'],
                   'versions':native_check.get('versions')})
            script=session.create_script(contract_source+(ROOT/'adapter.js').read_text(encoding='utf-8-sig'))
            script.on('message',receive)
            if attached:
                limits.basic.flags=0x2000
                if not K.SetInformationJobObject(job,9,C.byref(limits),C.sizeof(limits)):raise C.WinError(C.get_last_error())
            # load() can partially execute and then throw. From here, cleanup
            # must kill and wait for the stream before any detach is allowed.
            hooks_may_be_loaded=True
            script.load()
            if not ready.wait(10):raise RuntimeError('音频接管未能准备完成。')
            with rpc_lock:
                script.exports_sync.command({'action':'gain','value':gain})
                script.exports_sync.command({'action':'mute','value':muted})
                script.exports_sync.command({'action':'eq','value':eq_settings.load()})
            if signals is not None:
                # The native cef_initialize gate can now resume. CEF and its
                # renderer/CDP page do not exist yet; do not wait for them here.
                signals.signal_ready()
                audit({'event':'autoload-audio-ready','pid':process.pid})
            def select_mode(mode,device):
                nonlocal selection_error
                if mode not in ('shared','exclusive') or not isinstance(device,dict):
                    raise ValueError('无效的输出设备选择')
                device_id=device.get('deviceId') or ''
                device_name=device.get('name') or ''
                if not isinstance(device_id,str) or not isinstance(device_name,str):
                    raise ValueError('无效的输出设备信息')
                is_default=not device_id and str(device.get('type','')).lower()=='wasapi'
                if is_default:
                    device_id=catalog.snapshot().get('defaultDeviceId') or ''
                    if not device_id:raise RuntimeError('当前没有可用的 Windows 默认输出设备')
                if stop_requested.is_set():raise RuntimeError('输出扩展已关闭')
                with rpc_lock:
                    if ending or player_exited.is_set() or stop_requested.is_set():raise RuntimeError('输出扩展已关闭')
                    selection_error=None
                    result=script.exports_sync.command({'action':'select-mode','value':mode,
                        'deviceId':device_id,'deviceName':device_name,
                        'defaultDevice':is_default})
                if result is False or (isinstance(result,dict) and result.get('ok') is False):
                    raise RuntimeError(result.get('error','当前输出选择未被接受')
                        if isinstance(result,dict) else '当前输出选择未被接受')
                if callbacks_allowed():audit({'event':'output-selection','mode':mode,
                    'deviceId':device_id,'deviceName':device_name,
                    'defaultDevice':is_default})
                return result
            def set_volume(value):
                nonlocal gain
                if stop_requested.is_set():raise RuntimeError('输出扩展已关闭')
                gain=max(0,min(1,float(value)))
                with rpc_lock:
                    if ending or player_exited.is_set() or stop_requested.is_set():raise RuntimeError('输出扩展已关闭')
                    return script.exports_sync.command({'action':'gain','value':gain})
            def set_equalizer(value):
                if stop_requested.is_set():raise RuntimeError('输出扩展已关闭')
                with rpc_lock:
                    if ending or player_exited.is_set():raise RuntimeError('输出扩展已关闭')
                    if value is not None:
                        request=eq_settings.validate_request(value)
                        previous=script.exports_sync.command({'action':'status'})['eq']
                        value=eq_settings.resolve_update(previous,request)
                        native_changed=False
                        if request.get('action')=='enable' and value['enabled'] and bridge is not None:
                            outcome=bridge.set_native_standard_eq(False)
                            native_changed=bool(outcome and outcome.get('changed'))
                        try:
                            script.exports_sync.command({'action':'eq','value':value})
                            eq_settings.save(value)
                        except Exception:
                            script.exports_sync.command({'action':'eq','value':previous})
                            if native_changed:
                                try:bridge.set_native_standard_eq(True)
                                except Exception:pass
                            raise
                    state=script.exports_sync.command({'action':'status'})
                    return {'config':state.get('eq',eq_settings.defaults()),
                        'active':state.get('activeMode')=='exclusive',
                        'peak':max((s.get('peak',0) for s in state.get('streams',[])),default=0),
                        'rate':(state.get('activeInfo') or {}).get('rate')}
            def bridge_status(state):
                if callbacks_allowed():audit({'event':'output-list-status',
                    **{k:state[k] for k in ('state','connected','installed','last_error',
                        'mode','catalog_generation') if k in state}})
            # The first enumeration normally finishes while NCM starts. Bound the
            # remaining wait; an empty cache can later refresh when a device appears.
            catalog.wait_initial(3.0)
            if not callbacks_allowed():raise RuntimeError('输出扩展已关闭')
            bridge=start_output_bridge(select_mode,get_catalog=catalog.snapshot,port=debug_port,
                set_volume=set_volume,set_equalizer=set_equalizer,on_status=bridge_status)
            bridge.refreshDevices()
            emit('正在准备网易云输出设备列表……','ready')
            last=0;ui_missing_since=time.monotonic()
            while True:
                if signals is not None and signals.stop_requested():request_stop()
                if stop_requested.is_set():break
                if not fatal.empty():raise RuntimeError(fatal.get())
                if player_exited.is_set():break
                try:cmd=commands.get(timeout=0.25)
                except queue.Empty:cmd={}
                action=cmd.get('action')
                if action in ('disable','exit'):break
                if action in ('gain','mute','select-mode','reset-peak'):
                    if action=='select-mode':
                        select_mode(cmd.get('value',cmd.get('mode')),
                            {'deviceId':cmd.get('deviceId',''),'name':cmd.get('deviceName','')})
                    else:
                        with rpc_lock:script.exports_sync.command(cmd)
                    if action=='gain':gain=max(0,min(1,float(cmd.get('value',gain))))
                    elif action=='mute':muted=bool(cmd.get('value'))
                if action=='status' or time.monotonic()-last>=2:
                    with rpc_lock:stats=script.exports_sync.command({'action':'status'})
                    last=time.monotonic()
                    audit({'event':'stats',**compact_stats(stats)})
                    info=stats.get('activeInfo') or {}
                    device_snapshot=catalog.snapshot()
                    if (info.get('endpointId') and device_snapshot.get('ok') and
                            not any(str(d.get('deviceId','')).lower()==str(info['endpointId']).lower()
                                for d in device_snapshot.get('devices',[]))):
                        selection_error=output_warning({'reason':'device-unavailable'})
                    ui=bridge.status()
                    if not ui.get('installed'):
                        if ui_missing_since is None:ui_missing_since=time.monotonic()
                        if device_snapshot.get('devices'):
                            if time.monotonic()-ui_missing_since>45:raise RuntimeError('未能准备网易云输出列表，请重新启动扩展。')
                            emit('正在准备网易云输出设备列表……','ready',False)
                        else:
                            ui_missing_since=time.monotonic()
                            emit('等待可用的音频输出设备，连接后会自动更新列表。','ready',False)
                        continue
                    ui_missing_since=None
                    if selection_error:
                        emit(selection_error,'warning',False)
                        continue
                    mode=info.get('mode') or stats.get('activeMode')
                    if mode in ('exclusive','shared'):
                        emit(output_status(info,mode,device_snapshot),'enabled',True,
                            mode=mode,endpointId=info.get('endpointId'),
                            sourceRate=info.get('sourceRate'),outputRate=info.get('rate'),
                            channels=info.get('channels'),kind=info.get('kind'),
                            validBits=info.get('validBits'))
                    elif not device_snapshot.get('devices'):
                        emit('没有可用的音频输出设备，连接后会自动更新列表。','ready',False)
                    else:emit('输出列表已就绪。请在网易云设置中选择设备及共享或独占。','ready',False)
        except (Exception,KeyboardInterrupt) as e:
            if not stop_requested.is_set() and not player_exited.is_set():failure=str(e)
            audit({'event':'controller-error','error':str(e)})
            if failure:emit(str(e),'error',active)
        finally:
            ending=True
            # A native uninstall/stop signal can arrive during bridge startup,
            # before the ordinary loop next polls it. Never undo that stop by
            # launching the fallback player.
            if signals is not None:
                try:
                    if signals.stop_requested():stop_requested.set()
                except Exception:
                    stop_requested.set()
            if catalog is not None:catalog.stop()
            # A still-alive PCM stream must be destroyed before Frida removes conversion hooks.
            if process is not None and (not attached or hooks_may_be_loaded):
                killed=bool(job and K.TerminateJobObject(job,0))
                if not killed and process.poll() is None:process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=10)
            if bridge is not None:bridge.stop()
            if session is not None:
                try:session.detach()
                except frida.InvalidOperationError:pass
            if job:K.CloseHandle(job)
            managed_job=None
            restored_plain=False
            if process is not None:
                if not attached and not player_exited.is_set():
                    subprocess.Popen([str(exe),'--ncm-wasapi-managed-launch'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                    restored_plain=True
                process.close()
            if (attached and hooks_may_be_loaded and failure and
                    not player_exited.is_set() and not stop_requested.is_set()):
                # One recovery process only. Its explicit marker makes the
                # installed loader bypass itself, preventing a failure loop.
                try:
                    subprocess.Popen([str(exe),'--ncm-wasapi-managed-launch'],
                        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                    restored_plain=True
                    audit({'event':'ordinary-fallback-started','reason':failure})
                except Exception as error:
                    audit({'event':'ordinary-fallback-error','error':str(error)})
            if signals is not None:signals.close()
            if attached:
                message='网易云已退出，扩展已关闭。'if player_exited.is_set() else ('已恢复普通网易云输出。'if restored_plain else '扩展已安全退出，请重新打开网易云。'if hooks_may_be_loaded else '扩展未接入，网易云保持普通输出。')
                if failure:message=failure+' '+message
            else:message='网易云已退出，扩展已关闭。'if player_exited.is_set() else ((failure+' 已恢复普通输出。')if failure else '已恢复普通输出。网易云已重新启动。')
            emit(message,'error'if failure else 'disabled',False)
        return 1 if failure else 0

    try:
        return main()
    finally:
        log.close()


if __name__ == '__main__':
    raise SystemExit(main())
