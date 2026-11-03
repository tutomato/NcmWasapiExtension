"""Generic CDP transport regression, with no NCM connection or live mutation."""
import importlib.util,json,pathlib,queue,re,threading,time,unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]/'src'/'python'
FRONTEND=ROOT.parent/'frontend'
spec=importlib.util.spec_from_file_location('generic_cdp_bridge',ROOT/'cdp_bridge.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
DEVICE={'id':7,'deviceId':'physical-a','name':'USB DAC A','type':'Wasapi'}
CATALOG={'generation':9,'defaultDeviceId':'physical-a','devices':[
    {'deviceId':'physical-a','name':'USB DAC A','exclusiveSupported':True,'formats':[]}]}

class FakeWebSocket:
    def __init__(self):
        self.messages=queue.Queue();self.snapshot=None;self.installations=0
        self.install_request=None;self.closed=False;self.stages=[];self.acks=[];self.refreshes=0
    def settimeout(self,value):pass
    def reply(self,request_id,value=None):
        self.messages.put(json.dumps({'id':request_id,'result':{
            'result':{'type':'object','value':value}}}))
    def binding(self,kind,value,request_id='1'):
        self.messages.put(json.dumps({'method':'Runtime.bindingCalled','params':{
            'name':m.BINDING_NAME,'executionContextId':self.installations,
            'payload':json.dumps({'token':self.token,'owner':self.owner,
                'id':request_id,'kind':kind,'value':value})}}))
    def send(self,raw):
        message=json.loads(raw);request_id=message['id']
        if message['method']!='Runtime.evaluate':
            self.messages.put(json.dumps({'id':request_id,'result':{}}));return
        expression=message['params']['expression']
        if 'const token = ' in expression:
            self.installations+=1
            self.owner=json.loads(re.search(r'const owner = ("[^"]+");',expression)[1])
            self.token=json.loads(re.search(r'const token = ("[^"]+");',expression)[1])
            self.snapshot={'owner':self.owner,'installed':True,'lastMode':'exclusive',
                'lastDevice':DEVICE,'volume':.37,'restore':{'restored':True,'mode':'exclusive'}}
            self.install_request=request_id
            self.stages=[('catalog',[DEVICE]),('volume',.37),
                ('mode',{'mode':'exclusive','device':DEVICE})]
            self.binding('volume',.37)
        elif 'b.settle(' in expression:
            ack=expression.split('.map(id=>b.settle(id,',1)[1]
            ok,result=ack.split(',',1)
            self.acks.append((json.loads(ok),json.loads(result[:-len('));})()')])))
            self.reply(request_id,True)
            if self.install_request:
                if self.stages:self.binding(*self.stages.pop(0),request_id=str(len(self.acks)+1))
                else:
                    pending=self.install_request;self.install_request=None
                    self.reply(pending,self.snapshot)
        elif 'a.refreshDevices()' in expression:
            self.refreshes+=1;self.reply(request_id,None)
        elif 'b.dispose(' in expression:self.reply(request_id,None)
        else:self.reply(request_id,self.snapshot)
    def recv(self):
        try:return self.messages.get(timeout=.05)
        except queue.Empty:raise m.websocket.WebSocketTimeoutException()
    def close(self,**kwargs):self.closed=True;self.messages.put('')

def until(predicate,timeout=2):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:time.sleep(.01)
    assert predicate(),'Timed out'

class CdpBridgeTests(unittest.TestCase):
    def test_generic_transport_and_lifecycle(self):
        fake=FakeWebSocket()
        original_connect=m.websocket.create_connection;original_target=m._target
        m.websocket.create_connection=lambda *args,**kwargs:fake
        m._target=lambda port:{'id':'test-app','url':'orpheus/pub/app.html','webSocketDebuggerUrl':'ws://127.0.0.1:48763/test'}
        accepted=[];volumes=[];holder=[]
        volume_started=threading.Event();volume_release=threading.Event()
        def select_mode(mode,device):
            if device['deviceId']=='failed':raise RuntimeError('Expected adapter refusal')
            accepted.append((mode,device))
            if holder:holder[0].refreshDevices()  # Must never call CDP synchronously here.
        def set_volume(value):
            volumes.append(value)
            if value==.1:volume_started.set();assert volume_release.wait(2)
        bridge=None
        try:
            assert m._source_path('output-option-inject.js')==ROOT/'output-option-inject.js'
            bridge=m.start(select_mode,get_catalog=lambda:CATALOG,set_volume=set_volume,poll_interval=.1,
                injection_path=FRONTEND/'output-option-inject.js',
                installation_path=FRONTEND/'install_output_options.js')
            holder.append(bridge)
            assert bridge.wait_ready(3),bridge.status()
            assert accepted==[('exclusive',DEVICE)],accepted
            assert any(ok and isinstance(value,dict) and value==CATALOG for ok,value in fake.acks)
            assert bridge.status()['catalog_generation']==9,bridge.status()
            until(lambda:fake.refreshes>=1)
            time.sleep(.25);assert fake.installations==1,'Intact page reinstalled'
            fake.binding('volume',.1,'10');assert volume_started.wait(1)
            for request_id,value in enumerate([.2,.3,.4],11):fake.binding('volume',value,str(request_id))
            until(lambda:len(bridge._volume_requests)>=3)
            volume_release.set();until(lambda:volumes[-1]==.4)
            assert volumes==[.37,.37,.1,.4],volumes
            prior=len(accepted)
            fake.binding('mode',{'mode':'shared','device':{**DEVICE,'id':True}},'invalid')
            fake.binding('mode',{'mode':'shared','device':{**DEVICE,'deviceId':'a\0b'}},'invalid2')
            time.sleep(.05);assert len(accepted)==prior,'Malformed DTO reached adapter'
            fake.binding('mode',{'mode':'shared','device':{**DEVICE,'id':701}},'new-index')
            until(lambda:len(accepted)==prior+1)
            assert accepted[-1][1]['id']==701,'Transport assumed a fixed device index'
            fake.binding('mode',{'mode':'exclusive','device':{**DEVICE,'deviceId':'failed'}},'failure')
            until(lambda:any(not ok and 'Expected adapter refusal' in str(value) for ok,value in fake.acks))
            fake.snapshot=None
            until(lambda:fake.installations==2)
            until(lambda:len(accepted)==prior+2)
            assert bridge.wait_ready(1),bridge.status()
            print('PASS: catalog JSON roundtrip, canonical device callback, queued refresh, mode ack, volume coalescing, DTO validation, reload, teardown')
        finally:
            volume_release.set()
            if bridge:
                bridge.stop();assert bridge.status()['state']=='stopped',bridge.status()
            m.websocket.create_connection=original_connect;m._target=original_target

if __name__ == '__main__':
    unittest.main()
