import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
let bridgeUrl=new URL('../src/python/cdp_bridge.py',import.meta.url);
let frontendUrl=new URL('../src/frontend/',import.meta.url);
try {await fs.access(bridgeUrl);} catch (_) {
  bridgeUrl=new URL('cdp_bridge.py',import.meta.url);frontendUrl=new URL('./',import.meta.url);
}
const expression=execFileSync(process.env.NCM_TEST_PYTHON||'python',['-B','-c',`
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('adaptive_fixture',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=pathlib.Path(sys.argv[2])
print(m._install_expression((p/'output-option-inject.js').read_text(encoding='utf-8-sig'),
    (p/'install_output_options.js').read_text(encoding='utf-8-sig'),
    'fixture-token','fixture-owner',1,volume_enabled=True))
`,fileURLToPath(bridgeUrl),fileURLToPath(frontendUrl)],{encoding:'utf8',windowsHide:true});

function fixture({noStore=false,noSetter=false}={}) {
  const events=[], state={volume:.2};
  const device={id:1,deviceId:'physical-dac',name:'Fixture DAC'};
  const audio={
    getOutputDevices:async()=>({currentDevice:{type:'Wasapi',...device},
      totalDevices:[{type:'Wasapi',devices:[device]}]}),
    setOutputDevice:async selection=>{events.push(['native-set',selection]);},
    setVolume:value=>{state.volume=value;events.push(['native-volume',value]);}
  };
  if(noSetter) delete audio.setVolume;
  const originalGet=audio.getOutputDevices,originalSet=audio.setOutputDevice,originalVolume=audio.setVolume;
  const native={AudioPlayer:audio,Bridge:{triggerRegisterCall(){events.push(['refresh']);}}};
  const require=id=>{throw Error('Application factory must not run');};
  require.c={'native-renumbered':{exports:{a:native}}}; require.m={};
  if(!noStore) require.c['store-renumbered']={exports:{default:{getStore:()=>({playing:{playingVolume:state.volume}})}}};
  const window={webpackJsonp:{push(chunk){chunk[1][chunk[2][0][0]]({}, {}, require);}}};
  window.__codexNcmOutputMode=raw=>{
    const request=JSON.parse(raw);
    events.push(['request',request.kind,request.value]);
    queueMicrotask(()=>{
      const value=request.kind==='catalog'?{generation:1,defaultDeviceId:device.deviceId,
        devices:[{...device,exclusiveSupported:true}]}:'accepted';
      events.push(['ack',request.kind]);
      window.__codexOutputCdpBridge.settle(request.id,true,value);
    });
  };
  const context=vm.createContext({window,setTimeout,clearTimeout,
    localStorage:{getItem(){return null;},setItem(){}}});
  return {context,events,state,audio,originalGet,originalSet,originalVolume,window};
}

{
  const h=fixture();
  const result=await vm.runInContext(expression,h.context);
  assert.equal(result.installed,true); assert.equal(result.volume,.2);
  const nativeIndex=h.events.findIndex(e=>e[0]==='native-set');
  assert(nativeIndex>0);
  assert(h.events.slice(0,nativeIndex).some(e=>e[0]==='ack'&&e[1]==='volume'));
  assert(h.events.slice(0,nativeIndex).some(e=>e[0]==='ack'&&e[1]==='mode'));
  h.audio.setVolume(.4); await new Promise(resolve=>setTimeout(resolve,0));
  assert(h.events.some(e=>e[0]==='request'&&e[1]==='volume'&&e[2]===.4));
  assert.equal(h.window.__codexOutputCdpBridge.snapshot().volume,.4);
  h.window.__codexOutputCdpBridge.dispose('test stop');
  h.window.__codexOutputOptions.uninstall();
  assert.equal(h.audio.setVolume,h.originalVolume);
  assert.deepEqual(JSON.parse(JSON.stringify(await h.audio.getOutputDevices())),
    JSON.parse(JSON.stringify(await h.originalGet())));
  assert.equal(h.window.__codexOutputCdpBridge.installed,false);
}
for(const options of [{noStore:true},{noSetter:true}]) {
  const h=fixture(options);
  await assert.rejects(vm.runInContext(expression,h.context),/volume control contract/);
  assert.equal(h.audio.getOutputDevices,h.originalGet);
  assert.equal(h.audio.setOutputDevice,h.originalSet);
  assert.equal(h.audio.setVolume,h.originalVolume);
  assert.equal(h.events.length,0);
  assert.equal(h.window.__codexOutputCdpBridge,undefined);
}
console.log('PASS: renumbered native/store discovery, volume-before-mode acknowledgement, native volume forwarding, teardown, missing store/setter rejected before patch');
