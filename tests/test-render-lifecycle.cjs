const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../src/audio/generic-adapter-hooks.js'),'utf8');
const hooks=new Map(),records=new Map(),renders=new Map(),contexts=new Map();
const ctx=vm.createContext({records,renders,contexts,events:new Map(),address:(p,n)=>n,intercept:(a,k,c)=>hooks.set(k,c)});
vm.runInContext(source.slice(source.indexOf('function setupClient(c)'),source.indexOf('function grant(handle)')),ctx);
const ptr=s=>({toString:()=>s}),result=n=>({toInt32:()=>n,toUInt32:()=>n>>>0});
const old={client:'old',generation:1};records.set('old',old);contexts.set('old',{generation:1});renders.set('reused',old);
ctx.setupClient(ptr('client'));const service=hooks.get('service');
function getService(client,hr=0){const call={};service.onEnter.call(call,[ptr(client),{readU32:()=>0xf294acfc},{readPointer:()=>ptr('reused')}]);service.onLeave.call(call,result(hr));}
getService('shared');assert.equal(renders.has('reused'),false,'shared service kept stale exclusive owner');
renders.set('reused',old);getService('shared',-1);assert.equal(renders.get('reused'),old,'failed GetService changed ownership');
getService('old');assert.equal(renders.get('reused'),old);
assert.equal(ctx.managedRender(ptr('reused')),old);contexts.set('old',{generation:2});assert.equal(ctx.managedRender(ptr('reused')),undefined);assert(!renders.has('reused'));
contexts.set('old',{generation:1});renders.set('reused',old);records.delete('old');assert.equal(ctx.managedRender(ptr('reused')),undefined);
records.set('old',old);getService('old');const release=hooks.get('render-release'),call={};release.onEnter.call(call,[ptr('reused')]);release.onLeave.call(call,result(0));assert(!renders.has('reused'));
getService('old');const raced={};release.onEnter.call(raced,[ptr('reused')]);const newer={client:'new',generation:3};renders.set('reused',newer);release.onLeave.call(raced,result(0));assert.equal(renders.get('reused'),newer);
console.log('PASS: shared pointer reuse, failed service, generation guard, retired client, render release and replacement race');
