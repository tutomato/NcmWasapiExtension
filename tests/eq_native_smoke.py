"""Execute the actual Frida CModule DSP in an isolated test-only process."""
import json
from pathlib import Path
import subprocess
import sys
import frida

ROOT = Path(__file__).resolve().parents[1]
TEST = r'''
rpc.exports={test(){
 const assert=(v,m)=>{if(!v)throw Error(m);};
 const eq=createParametricEq();
 function config(gain,preamp=0,enabled=true,q=2){return {enabled,preamp,bands:[{enabled:true,type:'bell',frequency:1000,gain,q}]};}
 function input(rate,channels){const n=Math.round(rate*.25),a=Memory.alloc(n*channels*4+16);for(let i=0;i<n;i++)for(let ch=0;ch<channels;ch++)a.add((i*channels+ch)*4).writeFloat(ch===0?.1*Math.sin(2*Math.PI*1000*i/rate):0);a.add(n*channels*4).writeU32(0x1234abcd);return {n,a};}
 let cases=0;
 for(const rate of [32000,44100,48000,96000,192000])for(const channels of [1,2])for(const gain of [-12,6])for(const q of [2,20]){
   const {a,n}=input(rate,channels),s={rate,channels};eq.set(config(gain,0,true,q));
   // Non-divisible blocks exercise persistent state and the crossfade boundary.
   for(let f=0;f<n;f+=137)eq.process(s,a.add(f*channels*4),Math.min(137,n-f));
   let energy=0,count=0;
   for(let f=Math.round(rate*.1);f<n;f++){
     const x=a.add(f*channels*4).readFloat();assert(Number.isFinite(x),'finite');energy+=x*x;count++;
     if(channels===2)assert(a.add((f*2+1)*4).readFloat()===0,'stereo leakage');
   }
   const db=20*Math.log10(Math.sqrt(energy/count)/(.1/Math.sqrt(2)));
   assert(Math.abs(db-gain)<.03,'frequency response '+rate+'/'+channels+' '+db);
   assert(a.add(n*channels*4).readU32()===0x1234abcd,'buffer overrun');cases++;
 }
 // Processing one block or many must produce identical output, including ramp.
 for(const rate of [44100,96000]){
   const x=input(rate,2),y=input(rate,2),a={rate,channels:2},b={rate,channels:2};eq.set(config(6,-6));
   eq.process(a,x.a,x.n);
   for(let f=0;f<y.n;f+=73)eq.process(b,y.a.add(f*8),Math.min(73,y.n-f));
   const xb=new Uint8Array(x.a.readByteArray(x.n*8)),yb=new Uint8Array(y.a.readByteArray(y.n*8));
   assert(xb.every((v,i)=>v===yb[i]),'block size changed result');
 }
 const bypass=input(48000,2),before=new Uint8Array(bypass.a.readByteArray(bypass.n*8));
 eq.set(config(6,0,false));eq.process({rate:48000,channels:2},bypass.a,bypass.n);
 const after=new Uint8Array(bypass.a.readByteArray(bypass.n*8));
 assert(before.every((v,i)=>v===after[i]),'disabled bypass');
 // Fresh-stream preamp applies on the very first sample, never via dry bypass.
 eq.set({enabled:true,preamp:-12,bands:[]});
 const fresh=Memory.alloc(4);fresh.writeFloat(.8);eq.process({rate:48000,channels:1},fresh,1);
 assert(Math.abs(fresh.readFloat()-.8*Math.pow(10,-12/20))<1e-6,'startup preamp bypass');
 return {startupGain:true,passed:true,responseCases:cases,canaries:true,channelIsolation:true,blockInvariant:true,bypassExact:true};
}};
'''


def main():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    session = None
    try:
        session = frida.attach(child.pid)
        script = session.create_script((ROOT / 'src/audio/parametric-eq.js').read_text(encoding='utf-8') + TEST)
        script.load()
        print(json.dumps(script.exports_sync.test()))
    finally:
        if session: session.detach()
        child.terminate(); child.wait(timeout=10)


if __name__ == '__main__': main()
