"""No real process is opened, assigned, killed, or attached by these tests."""
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if (HERE/'attach_process.py').is_file():
    SOURCE=HERE
else:
    SOURCE=HERE.parent/'src/python'
sys.path.insert(0,str(SOURCE))
import attach_process as ap
spec=importlib.util.spec_from_file_location('autoload_controller_tests',SOURCE/'controller.py')
controller=importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class FakeIdentityKernel:
    def __init__(self):
        self.image=r'C:\Client\cloudmusic.exe';self.created=123456789
        self.session=8;self.our_session=8;self.closed=[];self.terminated=[]
        self.running=True;self.opens=[];self.signaled=[]
    def OpenProcess(self,rights,inherit,pid):self.opens.append((rights,inherit,pid));return 77
    def CloseHandle(self,handle):self.closed.append(handle);return True
    def QueryFullProcessImageNameW(self,handle,flags,buffer,length):buffer.value=self.image;return True
    def GetProcessTimes(self,handle,created,exited,kernel,user):
        created._obj.dwLowDateTime=self.created&0xffffffff
        created._obj.dwHighDateTime=self.created>>32
        return True
    def GetCurrentProcessId(self):return 999
    def ProcessIdToSessionId(self,pid,value):value._obj.value=self.our_session if pid==999 else self.session;return True
    def WaitForSingleObject(self,handle,timeout):return ap.WAIT_TIMEOUT if self.running else ap.WAIT_OBJECT_0
    def GetExitCodeProcess(self,handle,value):value._obj.value=0;return True
    def TerminateProcess(self,handle,code):self.terminated.append(handle);self.running=False;return True
    def OpenEventW(self,rights,inherit,name):self.opens.append((rights,inherit,name));return 91 if '.Ready.' in name else 92
    def SetEvent(self,handle):self.signaled.append(handle);return True


class IdentityTests(unittest.TestCase):
    def test_valid_process_keeps_the_opened_handle_for_wait_and_close(self):
        kernel=FakeIdentityKernel()
        process=ap.AttachedProcess(123,r'c:/CLIENT/cloudmusic.exe',kernel.created,kernel=kernel)
        self.assertIsNone(process.poll())
        process.close()
        self.assertEqual(kernel.closed,[77])
        self.assertEqual(kernel.terminated,[])
        self.assertEqual(kernel.opens[0],(ap.PROCESS_RIGHTS,False,123))

    def rejected(self,**changes):
        kernel=FakeIdentityKernel()
        for name,value in changes.items():setattr(kernel,name,value)
        with self.assertRaises(ValueError):ap.AttachedProcess(123,r'C:\Client\cloudmusic.exe',123456789,kernel=kernel)
        self.assertEqual(kernel.closed,[77])
        self.assertEqual(kernel.terminated,[])

    def test_recycled_pid_is_rejected_without_terminating_it(self):self.rejected(created=123456790)
    def test_another_image_is_rejected_without_terminating_it(self):self.rejected(image=r'C:\Other\cloudmusic.exe')
    def test_another_windows_session_is_rejected_without_terminating_it(self):self.rejected(session=9)

    def test_bad_arguments_do_not_open_any_process(self):
        for pid,created in [(True,1),(0,1),(-1,1),(1<<32,1),(1,0),(1,True),(1,1<<64)]:
            kernel=FakeIdentityKernel()
            with self.assertRaises(ValueError):ap.AttachedProcess(pid,r'C:\Client\cloudmusic.exe',created,kernel=kernel)
            self.assertEqual(kernel.opens,[])

    def test_already_exited_target_is_not_adopted(self):self.rejected(running=False)

    def test_wait_and_termination_use_retained_handle(self):
        kernel=FakeIdentityKernel();process=ap.AttachedProcess(123,kernel.image,kernel.created,kernel=kernel)
        with self.assertRaises(ap.subprocess.TimeoutExpired):process.wait(.01)
        process.terminate();self.assertEqual(process.wait(1),0)
        self.assertEqual(kernel.terminated,[77]);process.close()

    def test_event_identity_mismatch_does_not_open_arbitrary_events(self):
        kernel=FakeIdentityKernel()
        with self.assertRaises(ValueError):ap.StartupSignals(12,34,'Local\\Wrong','Local\\Other',kernel=kernel)
        self.assertEqual(kernel.opens,[])

    def test_correct_events_are_noninherited_and_closed(self):
        kernel=FakeIdentityKernel();names=ap.event_names(12,34)
        signals=ap.StartupSignals(12,34,*names,kernel=kernel)
        self.assertFalse(signals.stop_requested());signals.signal_ready();signals.close()
        self.assertEqual(kernel.signaled,[91]);self.assertEqual(kernel.closed,[91,92])
        self.assertEqual(kernel.opens,[(2,False,names[0]),(0x100000,False,names[1])])


class Function:
    def __init__(self,callback):self.callback=callback
    def __call__(self,*args):return self.callback(*args)


class LifecycleTests(unittest.TestCase):
    def execute(self, failure=None):
        events=[]
        state={'alive':True,'hooked':False,'ready':False,'flags':None}
        class Process:
            pid=123;_handle=77
            def __init__(self,*args,**kwargs):events.append('identity-verified')
            def poll(self):return None if state['alive'] else 0
            def terminate(self):events.append('terminate');state['alive']=False
            kill=terminate
            def wait(self,timeout=None):
                events.append('wait')
                if state['alive']:raise AssertionError('Must terminate stream before waiting')
                return 0
            def close(self):events.append('process-close')
        class Signals:
            def __init__(self,*args):events.append('signals-open')
            def stop_requested(self):return state['ready'] if failure!='bridge' else False
            def signal_ready(self):state['ready']=True;events.append('startup-ready')
            def close(self):events.append('signals-close')
        class Catalog:
            def refresh(self):return {'generation':1,'devices':[],'defaultDeviceId':None,'ok':True}
        class Exports:
            def ready(self):return True
            def verify(self):
                events.append('guard-verify')
                if state['flags'] != 0:raise AssertionError('Runtime verification must precede kill-on-close arming')
                if failure=='guard':raise RuntimeError('Unsupported runtime ABI')
                if failure=='guard-false':return {'ok':False,'workerRva':987}
                if failure=='guard-wrong-worker':return {'ok':True,'workerRva':988}
                return {'ok':True,'workerRva':987,'versions':{'avcodec':58<<16}}
            def command(self,command):
                events.append('command-'+command['action'])
                return {'selectedMode':'shared','activeInfo':None,'streams':[]}
        class Script:
            exports_sync=Exports()
            def __init__(self,is_probe,is_guard=False):self.probe=is_probe;self.guard=is_guard;self.callback=None
            def on(self,name,callback):self.callback=callback
            def load(self):
                if self.probe:return
                if self.guard:
                    events.append('guard-load')
                    if failure=='guard-load':raise RuntimeError('Guard script failed to load')
                    return
                events.append('script-load');state['hooked']=True
                self.assert_guard()
                if failure=='partial-load':raise RuntimeError('Partial audio hook failure')
                self.callback({'type':'send','payload':{'event':'adapter-ready'}},None)
            def assert_guard(self):
                if state['flags']!=0x2000:raise AssertionError('Hooks loaded without kill-on-close guard')
            def unload(self):events.append('guard-unload' if self.guard else 'probe-unload')
        class Session:
            def on(self,*args):pass
            def create_script(self,source):return Script('rpc.exports={ready()' in source,'readonly-guard-fixture' in source)
            def detach(self):
                if state['hooked'] and state['alive']:raise AssertionError('Live stream detached')
                events.append('detach')
        class MissingProcess(Exception):pass
        class Frida(types.SimpleNamespace):
            def attach(self,pid):
                if failure=='probe':raise RuntimeError('Cannot attach probe')
                events.append('frida-attach');return Session()
            def get_local_device(self):raise AssertionError('Attach mode enumerated/killed another player')
        frida=Frida(ProcessNotFoundError=MissingProcess,InvalidOperationError=RuntimeError)
        kernel=types.SimpleNamespace()
        def bind(name,callback):setattr(kernel,name,Function(callback))
        for name in ('OpenProcess','QueryFullProcessImageNameW','WaitForSingleObject','TerminateProcess'):
            bind(name,lambda *args: (_ for _ in ()).throw(AssertionError('Unexpected direct process API')))
        bind('CreateJobObjectW',lambda *args: events.append('job-create') or 88)
        def limits(*args):
            flags=args[2]._obj.basic.flags;events.append('job-flags-'+str(flags))
            if failure=='arm' and flags==0x2000:return False
            state['flags']=flags;return True
        bind('SetInformationJobObject',limits)
        bind('AssignProcessToJobObject',lambda *args: events.append('job-assign') or failure!='assign')
        def terminate_job(*args):events.append('job-kill');state['alive']=False;return True
        bind('TerminateJobObject',terminate_job)
        def close_handle(handle):
            events.append('job-close')
            if state['flags']==0x2000:state['alive']=False
            return True
        bind('CloseHandle',close_handle)
        class Bridge:
            def refreshDevices(self):events.append('bridge-refresh')
            def stop(self):events.append('bridge-stop')
        def start_bridge(*args,**kwargs):
            if not state['ready']:raise AssertionError('CEF startup gate has not been released')
            if failure in ('bridge','bridge-stop'):raise RuntimeError('Unsupported renderer interface')
            events.append('bridge-start');return Bridge()
        def start_plain(args,**kwargs):
            if state['alive']:raise AssertionError('Fallback started before modified player ended')
            if events.index('wait')>events.index('detach'):raise AssertionError('Unsafe detach order')
            if 'job-close' not in events:raise AssertionError('Fallback inherited the protected job')
            events.append(('plain-launch',args))
            return types.SimpleNamespace(pid=321)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'adapter.js').write_text('// adapter fixture')
            (root/'compatibility-guard.js').write_text('// readonly-guard-fixture')
            report={'clientPath':str(root/'cloudmusic.exe'),'compatibility':{'ok':True,'workerRva':987,'modules':{'player':'cloudmusic.dll'}}}
            paths={'logs':str(root/'logs')}
            fake_attach=types.SimpleNamespace(AttachedProcess=Process,StartupSignals=Signals)
            fake_jobs=types.SimpleNamespace(SuspendedProcess=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('Attach mode launched a player')))
            with mock.patch.dict(sys.modules,{'frida':frida,'attach_process':fake_attach,'job_process':fake_jobs,
                    'cdp_bridge':types.SimpleNamespace(start=start_bridge),
                    'device_capabilities':types.SimpleNamespace(DeviceCatalog=Catalog)}),\
                    mock.patch.object(C,'WinDLL',return_value=kernel),\
                    mock.patch.object(controller,'validate_client',return_value={'ok':True,'compatibility':report['compatibility']}),\
                    mock.patch.object(controller,'__file__',str(root/'controller.py')),\
                    mock.patch.object(sys,'stdin',None),\
                    mock.patch.object(sys,'stdout',io.StringIO()),\
                    mock.patch('subprocess.Popen',side_effect=start_plain):
                code=controller._run_enabled(report,paths,{},40001,attach_options={
                    'pid':123,'creation_time':123456789,'ready_event':'fixture-ready','stop_event':'fixture-stop'})
        return code,events,state

    def test_startup_ready_precedes_cef_bridge_and_teardown_preserves_hook_order(self):
        code,events,state=self.execute()
        self.assertEqual(code,0);self.assertFalse(state['alive'])
        self.assertLess(events.index('identity-verified'),events.index('job-assign'))
        self.assertLess(events.index('job-assign'),events.index('script-load'))
        self.assertLess(events.index('guard-verify'),events.index('job-flags-8192'))
        self.assertLess(events.index('guard-unload'),events.index('script-load'))
        self.assertLess(events.index('command-mute'),events.index('startup-ready'))
        self.assertLess(events.index('startup-ready'),events.index('bridge-start'))
        self.assertLess(events.index('job-kill'),events.index('wait'))
        self.assertLess(events.index('wait'),events.index('detach'))
        self.assertFalse(any(isinstance(event,tuple) for event in events))

    def test_pre_hook_probe_failure_keeps_the_ordinary_player_alive(self):
        code,events,state=self.execute('probe')
        self.assertEqual(code,1);self.assertTrue(state['alive'])
        self.assertNotIn('job-kill',events);self.assertNotIn('script-load',events)
        self.assertIn('job-close',events)
        self.assertFalse(any(isinstance(event,tuple) for event in events))

    def test_assignment_failure_does_not_kill_the_ordinary_player(self):
        code,events,state=self.execute('assign')
        self.assertEqual(code,1);self.assertTrue(state['alive'])
        self.assertNotIn('job-kill',events);self.assertNotIn('frida-attach',events)

    def test_failed_guard_arming_never_loads_or_kills(self):
        code,events,state=self.execute('arm')
        self.assertEqual(code,1);self.assertTrue(state['alive'])
        self.assertNotIn('script-load',events);self.assertNotIn('job-kill',events)

    def test_partial_hook_load_destroys_stream_before_detach(self):
        code,events,state=self.execute('partial-load')
        self.assertEqual(code,1);self.assertFalse(state['alive'])
        self.assertNotIn('startup-ready',events)
        self.assertLess(events.index('job-kill'),events.index('wait'))
        self.assertLess(events.index('wait'),events.index('detach'))
        launches=[event[1] for event in events if isinstance(event,tuple)]
        self.assertEqual(len(launches),1)
        self.assertEqual(launches[0][1:],['--ncm-wasapi-managed-launch'])

    def test_renderer_failure_starts_one_plain_bypass_after_safe_teardown(self):
        code,events,state=self.execute('bridge')
        self.assertEqual(code,1);self.assertFalse(state['alive'])
        launches=[event[1] for event in events if isinstance(event,tuple)]
        self.assertEqual(len(launches),1)
        self.assertEqual(launches[0][1:],['--ncm-wasapi-managed-launch'])
        self.assertLess(events.index('wait'),events.index('detach'))
        self.assertLess(events.index('detach'),events.index(('plain-launch',launches[0])))

    def test_stop_during_renderer_failure_never_restarts_the_player(self):
        code,events,state=self.execute('bridge-stop')
        self.assertFalse(state['alive'])
        self.assertFalse(any(isinstance(event,tuple) for event in events))
        self.assertLess(events.index('wait'),events.index('detach'))

    def test_runtime_guard_rejection_keeps_the_ordinary_player_alive(self):
        for failure in ('guard','guard-false','guard-wrong-worker','guard-load'):
            with self.subTest(failure=failure):
                code,events,state=self.execute(failure)
                self.assertEqual(code,1);self.assertTrue(state['alive'])
                self.assertNotIn('job-flags-8192',events)
                self.assertNotIn('script-load',events);self.assertNotIn('job-kill',events)
                self.assertNotIn('startup-ready',events)
                self.assertIn('guard-unload',events);self.assertIn('detach',events)
                self.assertFalse(any(isinstance(event,tuple) for event in events))


if __name__=='__main__':unittest.main()
