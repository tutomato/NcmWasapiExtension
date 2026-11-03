import json
from pathlib import Path
import queue
import sys
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'python'))
import cdp_bridge as m
import eq_settings


class EqBridgeTests(unittest.TestCase):
    def test_effect_subwindow_can_only_send_eq(self):
        bridge=m.OutputOptionsBridge(lambda *a:True,get_catalog=lambda:{'devices':[]},set_equalizer=lambda v:{})
        class Client:
            closed=threading.Event()
        client=Client();bridge._owner='fixture';bridge._eq_clients['native-effect']=client
        def event(kind,value):
            bridge._on_event(client,{'method':'Runtime.bindingCalled','params':{'name':m.BINDING_NAME,
                'payload':json.dumps({'token':bridge._token,'owner':'fixture','id':'1','kind':kind,'value':value})}})
        event('volume',.5);event('catalog',[])
        event('mode',{'mode':'exclusive','device':{'id':1,'name':'DAC','deviceId':'dac'}})
        self.assertTrue(bridge._requests.empty());self.assertEqual(bridge._volume_requests,[])
        event('eq',None)
        request=bridge._requests.get_nowait()
        self.assertTrue(bridge._current_request(request))
        bridge._eq_clients.clear()
        self.assertFalse(bridge._current_request(request))

    def test_validated_acknowledgement_and_stale_owner(self):
        applied=[]
        bridge=m.OutputOptionsBridge(lambda *a:True,get_catalog=lambda:{'devices':[]},
            set_equalizer=lambda v: applied.append(v) or {'config':v,'active':True})
        class Client:
            closed=threading.Event()
        client=Client(); bridge._client=client; bridge._owner='fixture'
        ack=[];bridge._acknowledge=lambda requests,ok,message:ack.append((ok,message))
        def send(value, owner='fixture'):
            bridge._on_event(client,{'method':'Runtime.bindingCalled','params':{'name':m.BINDING_NAME,
                'payload':json.dumps({'token':bridge._token,'owner':owner,'id':'1','kind':'eq','value':value})}})
        send(eq_settings.defaults(),'stale')
        self.assertTrue(bridge._requests.empty())
        bad=eq_settings.defaults();bad['bands'][0]['q']=0
        send(bad);self.assertTrue(bridge._requests.empty())
        send(eq_settings.defaults())
        request=bridge._requests.get_nowait();bridge._apply('eq',[request])
        self.assertEqual(applied,[eq_settings.defaults()]);self.assertTrue(ack[-1][0])
        self.assertTrue(ack[-1][1]['active'])
        send(None);request=bridge._requests.get_nowait();bridge._apply('eq',[request])
        self.assertIsNone(applied[-1])


if __name__=='__main__':unittest.main()
