const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const ctx=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/audio/parametric-eq.js'),'utf8'),ctx);
function response(c,f,sr){const w=2*Math.PI*f/sr;let nr=c[0]+c[1]*Math.cos(w)+c[2]*Math.cos(2*w),ni=-c[1]*Math.sin(w)-c[2]*Math.sin(2*w),dr=1+c[3]*Math.cos(w)+c[4]*Math.cos(2*w),di=-c[3]*Math.sin(w)-c[4]*Math.sin(2*w);return 10*Math.log10((nr*nr+ni*ni)/(dr*dr+di*di));}
let count=0;
for(const sr of [32000,44100,48000,96000,192000])for(const type of ['bell','low_shelf','high_shelf'])for(const gain of [-18,-6,6,18])for(const q of [.2,.707,1,5,10,20]){
 const b={enabled:true,type,frequency:1000,gain,q},c=ctx.eqCoefficients(b,sr);
 assert(c.every(Number.isFinite));
 // Jury stability conditions for denominator 1+a1*z^-1+a2*z^-2.
 assert(Math.abs(c[4])<1 && 1+c[3]+c[4]>0 && 1-c[3]+c[4]>0);
 const at=type==='bell'?1000:type==='low_shelf'?0:sr/2;
 assert(Math.abs(response(c,at,sr)-gain)<1e-6);count++;
}
const narrow=ctx.eqCoefficients({enabled:true,type:'bell',frequency:1000,gain:6,q:5},48000);
const wide=ctx.eqCoefficients({enabled:true,type:'bell',frequency:1000,gain:6,q:.5},48000);
assert(response(narrow,1500,48000)<response(wide,1500,48000));
assert.deepEqual(Array.from(ctx.eqCoefficients({enabled:true,type:'bell',frequency:20000,gain:6,q:1},32000)),[1,0,0,0,0]);
assert.throws(()=>ctx.validateEq({enabled:true,preamp:NaN,bands:[]}));
assert.throws(()=>ctx.validateEq({enabled:true,preamp:0,bands:[{enabled:true,type:'bell',frequency:1000,gain:6,q:0}]}));
console.log('EQ coefficient checks passed:',count,'filter configurations; Q width, stability and validation.');

for(const sr of [32000,44100,48000,96000,192000])for(const f of [20,73,1000,Math.min(20000,sr*.49)])for(const type of ['bell','low_shelf','high_shelf'])for(const gain of [-18,18]){
 const c=ctx.eqCoefficients({enabled:true,type,frequency:f,gain,q:20},sr);
 assert(c.every(Number.isFinite));assert(Math.abs(c[4])<1 && 1+c[3]+c[4]>0 && 1-c[3]+c[4]>0);
}
assert.equal(ctx.validateEq({enabled:true,preamp:0,bands:[{enabled:true,type:'bell',frequency:125,gain:-12,q:20}]}).bands[0].q,20);
console.log('Q20 boundary: 120 extreme filter configurations stable.');
