const {chromium}=require(process.env.NCM_PLAYWRIGHT_PATH||'playwright');
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({executablePath:process.env.NCM_TEST_BROWSER,headless:true});try{
const page=await browser.newPage();await page.route('http://eq.test/**',r=>r.fulfill({body:'<div id="page_pc_mini_bar"><div class="right-side"><div class="right"><div data-testid="tid_playbar_soundquality"><div class="cmd-tag native">音质</div></div></div></div></div>',contentType:'text/html'}));await page.goto('http://eq.test/');
await page.evaluate(source=>{window.state={config:{enabled:false},active:true};window.nativeEnabled=false;window.opened=0;window.__codexOutputOptions={readNativeEqState:()=>({enabled:window.nativeEnabled}),subscribeNativeEq:fn=>(window.notify=fn,()=>{}),openNativeEqualizer:async()=>{window.opened++;}};const transport={request:async(k,v)=>{if(v)window.state.config.enabled=v.enabled;return structuredClone(window.state);}};window.transport=transport;eval(source);},fs.readFileSync(path.join(__dirname,'../src/frontend/equalizer-playbar.js'),'utf8'));
const tag=page.getByTestId('ncm-parametric-eq-playbar');assert.equal(await tag.count(),0);
await page.evaluate(()=>{window.state.config.enabled=true;transport.setEqState(structuredClone(window.state));});assert.equal(await tag.count(),1);assert(await tag.evaluate(e=>e.classList.contains('native')));
await tag.click();assert.equal(await page.evaluate(()=>window.opened),1);assert(await page.evaluate(()=>localStorage.getItem('ncm-parametric-eq-open')));
await page.evaluate(()=>document.querySelector('.right').innerHTML='<div data-testid="tid_playbar_soundquality"><div class="cmd-tag native">音质</div></div>');await page.waitForTimeout(40);assert.equal(await tag.count(),1);
await page.evaluate(()=>{window.nativeEnabled=true;notify();});await page.waitForTimeout(40);assert.equal(await tag.count(),0);assert.equal(await page.evaluate(()=>window.state.config.enabled),false);
await page.evaluate(()=>transport.disposeEqPlaybar());assert.equal(await page.getByTestId('tid_playbar_soundquality').count(),1);console.log('PASS: playbar visibility, native styling, open routing, remount, native EQ interlock and disposal');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
