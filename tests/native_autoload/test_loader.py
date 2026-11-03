"""Isolated loader smoke test with fake CEF/music DLLs, never the real client."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
SOURCE=ROOT/'src/autoload' if (ROOT/'src/autoload').is_dir() else HERE
ZIG=Path(sys.argv[1]).resolve()
CASE=HERE/('fixture-'+str(time.time_ns()))
CASE.mkdir()
def cc(*args):
    result=subprocess.run([str(ZIG),'cc','-target','x86_64-windows-gnu','-O2','-g0','-s',*map(str,args)],cwd=CASE,
                          capture_output=True,text=True,creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError(result.stdout+result.stderr)
cc('-shared',SOURCE/'loader.c',SOURCE/'forwarders.S',SOURCE/'msimg32.def','-lws2_32',
   '-Wl,--out-implib,msimg32.dll.a','-o','msimg32.dll')
cc('-shared',HERE/'fixture_cef.c','-Wl,--out-implib,libcef.dll.a','-o','libcef.dll')
cc('-shared',HERE/'fixture_music.c','msimg32.dll.a','libcef.dll.a','-lgdi32',
   '-Wl,--out-implib,cloudmusic.dll.a','-o','cloudmusic.dll')
cc(HERE/'fixture_app.c','cloudmusic.dll.a','-o','cloudmusic.exe')
payload=CASE/'payload with spaces 测试'
payload.mkdir()
payload_record=ROOT/'.build/last-payload.txt' if (ROOT/'.build/last-payload.txt').is_file() else ROOT/'outputs/NcmWasapiProject/.build/last-payload.txt'
source_runtime=Path(payload_record.read_text(encoding='utf-8-sig'))/'runtime'
shutil.copytree(source_runtime,payload/'runtime')
(payload/'controller.py').write_text('''import argparse,ctypes as C,json,os,pathlib
p=argparse.ArgumentParser()
for x in ('attach','debug-port','startup-ready-event','stop-event','creation-time','client'): p.add_argument('--'+x,required=True)
a=p.parse_args()
pathlib.Path(os.environ['NCM_AUTOLOAD_TEST_HELPER']).write_text(json.dumps(vars(a)),encoding='utf-8')
k=C.WinDLL('kernel32',use_last_error=True)
k.OpenEventW.argtypes=[C.c_ulong,C.c_int,C.c_wchar_p];k.OpenEventW.restype=C.c_void_p
k.SetEvent.argtypes=[C.c_void_p];k.CloseHandle.argtypes=[C.c_void_p]
h=k.OpenEventW(2,False,a.startup_ready_event)
assert h
assert k.SetEvent(h)
k.CloseHandle(h)
''',encoding='utf-8')
(CASE/'ncm-wasapi-autoload.ini').write_text('[NcmWasapiExtension]\nSchema=1\nPayloadRoot='+str(payload)+'\n',encoding='utf-16')
env=dict(os.environ,NCM_AUTOLOAD_TEST_RESULT=str(CASE/'cef-result.txt'),NCM_AUTOLOAD_TEST_HELPER=str(CASE/'helper.json'))
run=subprocess.run([str(CASE/'cloudmusic.exe')],cwd=CASE,env=env,creationflags=0x08000000,timeout=35)
assert run.returncode==0,run.returncode
command=(CASE/'cef-result.txt').read_bytes().decode('utf-16-le')
helper=json.loads((CASE/'helper.json').read_text(encoding='utf-8'))
match=re.search(r'--remote-debugging-port=(\d+)',command)
assert match and int(match[1])==int(helper['debug_port'])
assert '--remote-debugging-address=127.0.0.1' in command
assert int(helper['attach'])>0 and int(helper['creation_time'])>0
expected='Local\\NcmWasapiExtension.Ready.'+helper['attach']+'.'+helper['creation_time']
assert helper['startup_ready_event']==expected
assert Path(helper['client'])==CASE/'cloudmusic.exe'
print('PASS: natural exe startup, CEF-before-init command line, hidden helper handshake, Unicode/spaced path, System32 AlphaBlend forwarding')
# An explicitly managed launch must never start a second controller or add switches.
(CASE/'helper.json').unlink()
run=subprocess.run([str(CASE/'cloudmusic.exe'),'--ncm-wasapi-managed-launch'],cwd=CASE,env=env,creationflags=0x08000000,timeout=15)
assert run.returncode==0 and not (CASE/'helper.json').exists()
assert '--remote-debugging-port=' not in (CASE/'cef-result.txt').read_bytes().decode('utf-16-le')
print('PASS: managed-launch duplicate-controller bypass')
run=subprocess.run([str(CASE/'cloudmusic.exe'),'--remote-debugging-port=42310'],cwd=CASE,env=env,creationflags=0x08000000,timeout=15)
assert run.returncode==0 and not (CASE/'helper.json').exists()
existing=(CASE/'cef-result.txt').read_bytes().decode('utf-16-le')
assert existing.count('--remote-debugging-port=')==1 and '--remote-debugging-port=42310' in existing
print('PASS: existing debug-port launch is not attached twice')
run=subprocess.run([str(CASE/'cloudmusic.exe'),'--type=renderer'],cwd=CASE,env=env,creationflags=0x08000000,timeout=15)
assert run.returncode==0 and not (CASE/'helper.json').exists()
assert '--remote-debugging-port=' not in (CASE/'cef-result.txt').read_bytes().decode('utf-16-le')
print('PASS: subprocess does not start a helper')
(CASE/'ncm-wasapi-autoload.ini').unlink()
run=subprocess.run([str(CASE/'cloudmusic.exe')],cwd=CASE,env=env,creationflags=0x08000000,timeout=15)
assert run.returncode==0 and not (CASE/'helper.json').exists()
assert '--remote-debugging-port=' not in (CASE/'cef-result.txt').read_bytes().decode('utf-16-le')
print('PASS: missing installation config remains transparent forwarding only')
print('Fixture directory:',CASE)
