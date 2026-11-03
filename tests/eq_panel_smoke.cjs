/* Optional browser integration: install Playwright, then node tests/eq_panel_smoke.cjs.
 * NCM_PLAYWRIGHT_PATH / NCM_TEST_BROWSER can select a local development runtime.
 */
const {chromium}=require(process.env.NCM_PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../src/frontend/equalizer-panel.js'),'utf8');
const template=fs.readFileSync(path.join(__dirname,'../src/frontend/equalizer-transport.js'),'utf8');
// Parse the composed transport as well as the standalone view.
let composed=template;
for(const [key,value] of Object.entries({__TOKEN__:JSON.stringify('test-token'),__OWNER__:JSON.stringify('test-owner'),__GLOBAL__:JSON.stringify('testBridge'),__BINDING__:JSON.stringify('testBinding'),__UI__:source}))composed=composed.split(key).join(value);
new Function(composed);
(async()=>{
 const browser=await chromium.launch(process.env.NCM_TEST_BROWSER?{executablePath:process.env.NCM_TEST_BROWSER,headless:true}:{channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:575,height:560}});
  await page.setContent(`<html><meta charset="utf-8"><body style="margin:0;background:#21699d;color:#fff;font:13px 'Microsoft YaHei',sans-serif"><div class="cmd-tabs"><div role="tablist" style="height:80px"><button role="tab" style="color:inherit" id="cmdTab/audioEffect/soundEffect" aria-selected="true">均衡器</button><div class="cmd-tabs-bar-extra">原生音效开关</div></div><div role="tabpanel" id="semiTabPanel/audioEffect/soundEffect" aria-hidden="false"><div id="original">原生均衡器<div class="cmd-slider-track" style="background:rgb(54,198,244)"></div></div></div></div></body></html>`);
  await page.evaluate(text=>{
   window.requests=[];window.c={enabled:false,preamp:0,bands:[31,63,125,250,500,1000,2000,4000,8000,16000].map(f=>({enabled:true,type:'bell',frequency:f,gain:0,q:1}))};
   const transport={request:async(kind,v)=>{window.requests.push(v);if(window.failNext){window.failNext=false;throw Error('模拟保存失败');}if(v){if(v.action==='enable')window.c.enabled=v.enabled;else if(v.action==='reset')window.c={enabled:window.c.enabled,preamp:0,bands:[31,63,125,250,500,1000,2000,4000,8000,16000].map(f=>({enabled:true,type:'bell',frequency:f,gain:0,q:1}))};else window.c={...v,enabled:window.c.enabled};}return {config:JSON.parse(JSON.stringify(window.c)),active:true,rate:96000,peak:.5};}};
   eval(text);window.disposeEq=transport.disposeEqualizer;
  },source);
  const host=page.locator('#ncm-parametric-equalizer');
  assert.equal(await host.getByRole('button',{name:'一键重置',exact:true,includeHidden:true}).count(),1);
  assert.equal(await host.getByRole('button',{name:'预留增益空间',exact:true}).count(),0);
  assert.equal(await host.count(),1);assert.equal(await page.locator('body > #ncm-parametric-equalizer').count(),0);
  await host.getByRole('button',{name:'参数 EQ',exact:true}).click();
  assert.equal(await host.getByRole('spinbutton',{name:'总增益',exact:true}).inputValue(),'0');
  const flat=await host.locator('.curve').getAttribute('points');assert(flat.split(' ').every(p=>Number(p.split(',')[1])===65),'Default response must be 0 dB');
  await host.getByRole('spinbutton',{name:'频率',exact:true}).fill('73');
  await host.getByRole('spinbutton',{name:'增益',exact:true}).fill('-4');
  await host.getByRole('spinbutton',{name:'Q 值',exact:true}).fill('3');
  await host.locator('#enabled').check();
  await page.waitForFunction(()=>window.c.enabled);assert.equal(await page.evaluate(()=>window.c.bands[0].gain),0,'Toggle must not apply draft');assert.equal(await host.getByRole('spinbutton',{name:'增益',exact:true}).inputValue(),'-4');await host.getByRole('button',{name:'应用调参'}).click();
  const c=await page.evaluate(()=>window.c);assert(c.enabled);assert.equal(c.bands[0].frequency,73);assert.equal(c.bands[0].gain,-4);assert.equal(c.bands[0].q,3);
  await host.getByRole('button',{name:'选择频段 2',exact:true}).click();
  assert.equal(await host.getByRole('spinbutton',{name:'频率',exact:true}).inputValue(),'63');
  await host.getByRole('spinbutton',{name:'频率',exact:true}).fill('125');
  await host.getByRole('spinbutton',{name:'增益',exact:true}).fill('-12');
  const qInput=host.getByRole('spinbutton',{name:'Q 值',exact:true});
  async function curveAt(f){return host.locator('.curve').evaluate((p,f)=>{const x=32+Math.log(f/20)/Math.log(1000)*468;return p.getAttribute('points').split(' ').map(s=>s.split(',').map(Number)).sort((a,b)=>Math.abs(a[0]-x)-Math.abs(b[0]-x))[0][1];},f);}
  await qInput.fill('2');const wide=await curveAt(180);
  await qInput.fill('20');const narrow=await curveAt(180);assert(Math.abs(narrow-65)<Math.abs(wide-65),'Q20 must be narrower than Q2');
  await host.getByRole('button',{name:'应用调参'}).click();assert.equal(await page.evaluate(()=>window.c.bands[1].q),20);
  await qInput.fill('200');assert.equal(await qInput.inputValue(),'20');assert.match(await host.locator('.message').textContent(),/已调整为 20/);
  await qInput.fill('0');await host.getByRole('spinbutton',{name:'增益',exact:true}).click();assert.equal(await qInput.inputValue(),'0.2');
  await qInput.fill('');await qInput.press('Enter');assert.equal(await qInput.inputValue(),'0.2');
  await qInput.fill('0.333');await qInput.press('Enter');assert.equal(await qInput.inputValue(),'0.33');
  await host.getByRole('button',{name:'应用调参'}).click();assert.equal(await page.evaluate(()=>window.c.bands[1].q),.33);
  const gainInput=host.getByRole('spinbutton',{name:'增益',exact:true}), preampInput=host.getByRole('spinbutton',{name:'总增益',exact:true});
  await gainInput.fill('20');assert.equal(await gainInput.inputValue(),'18');assert.match(await host.locator('.message').textContent(),/已调整为 18 dB/);
  await gainInput.fill('-20');assert.equal(await gainInput.inputValue(),'-18');
  await gainInput.fill('');await gainInput.press('-');await gainInput.press('Enter');assert.equal(await gainInput.inputValue(),'-18');
  await gainInput.fill('2.34');await gainInput.press('Enter');assert.equal(await gainInput.inputValue(),'2.3');
  await preampInput.fill('6');assert.equal(await preampInput.inputValue(),'0');
  await preampInput.fill('-40');assert.equal(await preampInput.inputValue(),'-30');
  await preampInput.fill('-0.5');await preampInput.press('Enter');assert.equal(await preampInput.inputValue(),'-0.5');
  await preampInput.fill('');await preampInput.press('Enter');assert.equal(await preampInput.inputValue(),'-0.5');
  await host.getByRole('button',{name:'应用调参'}).click();
  assert.equal(await page.evaluate(()=>window.c.bands[1].gain),2.3);assert.equal(await page.evaluate(()=>window.c.preamp),-.5);
  await host.locator('#enabled').uncheck();await page.waitForFunction(()=>!window.c.enabled);
  await page.evaluate(()=>window.failNext=true);await host.locator('#enabled').click();await page.waitForTimeout(100);assert.equal(await host.locator('#enabled').isChecked(),false,'Failed toggle rolls back');
  await host.locator('#enabled').check();await host.locator('#enabled').uncheck();await host.locator('#enabled').check();await page.waitForTimeout(100);assert(await page.evaluate(()=>window.c.enabled));
  await host.getByRole('button',{name:'一键重置',exact:true}).click();await page.waitForFunction(()=>window.c.preamp===0&&window.c.bands.every(b=>b.gain===0&&b.q===1));assert(await page.evaluate(()=>window.c.enabled));assert.equal(await host.getByRole('spinbutton',{name:'频率',exact:true}).inputValue(),'31');
  assert.equal(await page.locator('#original').isVisible(),false);assert.equal(await page.locator('.cmd-tabs-bar-extra').isVisible(),false);
  const bounds=await host.locator('.message').boundingBox();assert(bounds.y+bounds.height<=560,'Editor exceeds native window');
  const output=process.env.NCM_EQ_SCREENSHOT_DIR;
  if(output){fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'native-blue.png')});}
  await host.getByRole('button',{name:'标准均衡器',exact:true}).click();assert(await page.locator('#original').isVisible());assert(await page.locator('.cmd-tabs-bar-extra').isVisible());
  await page.evaluate(()=>document.querySelector('[role=tabpanel]').remove());await page.waitForTimeout(30);assert.equal(await host.count(),0);
  await page.evaluate(()=>{const p=document.createElement('div');p.id='semiTabPanel/audioEffect/soundEffect';p.setAttribute('role','tabpanel');p.innerHTML='<div id="replacement">原生重开</div>';document.querySelector('.cmd-tabs').appendChild(p);});
  await page.waitForTimeout(30);assert.equal(await host.count(),1);
  await host.getByRole('button',{name:'参数 EQ',exact:true}).click();
  for(const [name,bg,color] of [['dark','#202126','#eee'],['light','#fff','#333']]){
   await page.evaluate(({bg,color})=>{document.body.style.background=bg;document.body.style.color=color;},{bg,color});
   await page.waitForTimeout(2200);
   const colors=await host.evaluate(h=>[getComputedStyle(h).color,getComputedStyle(document.getElementById('cmdTab/audioEffect/soundEffect')).color]);assert.equal(colors[0],colors[1]);
   if(output)await page.screenshot({path:path.join(output,'native-'+name+'.png')});
  }
  await page.evaluate(()=>window.disposeEq());assert.equal(await host.count(),0);assert(await page.locator('#replacement').isVisible());
  console.log('PASS: native-only mounting, 10 bands, parameter save, range clamping, Q2/Q20 curve width, native content/switch restoration, close/reopen, window fit and disposal.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
