/* Mounted only inside NCM's native audio-effect -> equalizer pane. */
{
  let mounted=null, disposed=false, mode='standard', config=null, selected=0, lastState=null, busy=false;
  const clone=x=>JSON.parse(JSON.stringify(x));
  function coefficients(b,rate){
    if(!b.enabled||!b.gain||b.frequency>=rate/2)return [1,0,0,0,0];
    const w=2*Math.PI*b.frequency/rate,c=Math.cos(w),A=Math.pow(10,b.gain/40),a=Math.sin(w)/(2*b.q),t=2*Math.sqrt(A)*a;
    let n,d;
    if(b.type==='bell'){n=[1+a*A,-2*c,1-a*A];d=[1+a/A,-2*c,1-a/A];}
    else if(b.type==='low_shelf'){n=[A*((A+1)-(A-1)*c+t),2*A*((A-1)-(A+1)*c),A*((A+1)-(A-1)*c-t)];d=[(A+1)+(A-1)*c+t,-2*((A-1)+(A+1)*c),(A+1)+(A-1)*c-t];}
    else{n=[A*((A+1)+(A-1)*c+t),-2*A*((A-1)+(A+1)*c),A*((A+1)+(A-1)*c-t)];d=[(A+1)-(A-1)*c+t,2*((A-1)-(A+1)*c),(A+1)-(A-1)*c-t];}
    return [n[0]/d[0],n[1]/d[0],n[2]/d[0],d[1]/d[0],d[2]/d[0]];
  }
  function response(c,f,rate){const w=2*Math.PI*f/rate,nr=c[0]+c[1]*Math.cos(w)+c[2]*Math.cos(2*w),ni=-c[1]*Math.sin(w)-c[2]*Math.sin(2*w),dr=1+c[3]*Math.cos(w)+c[4]*Math.cos(2*w),di=-c[3]*Math.sin(w)-c[4]*Math.sin(2*w);return 10*Math.log10(Math.max(1e-20,(nr*nr+ni*ni)/(dr*dr+di*di)));}
  const frequencyLabel=f=>f>=1000?(Math.round(f/100)/10)+'k':String(Math.round(f));
  function release(){
    if(!mounted)return;
    for(const item of mounted.hidden){if(item.node.style.display==='none')item.node.style.display=item.display;}
    if(mounted.extra&&mounted.extra.style.display==='none')mounted.extra.style.display=mounted.extraDisplay;
    if(mounted.pane.style.height==='100%')mounted.pane.style.height=mounted.paneHeight;
    if(mounted.pane.style.overflowY==='auto')mounted.pane.style.overflowY=mounted.paneOverflow;
    mounted.host.remove();mounted=null;
  }
  function report(message){if(mounted)mounted.root.querySelector('.message').textContent=message;}
  function status(){if(!mounted||!lastState)return;const e=mounted.root.querySelector('.state');e.textContent=lastState.active?(lastState.config.enabled?'独占 EQ 已启用':'EQ 已关闭'):'等待独占输出';e.title=lastState.rate?lastState.rate+' Hz':'';if(lastState.peak>1)report('输出峰值超限，请降低总增益。');}
  function applyMode(){
    if(!mounted)return;
    const m=mounted, param=mode==='parametric';
    m.root.querySelector('.editor').hidden=!param;
    m.root.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===mode)));
    for(const item of m.hidden)item.node.style.display=param?'none':item.display;
    if(m.extra)m.extra.style.display=param?'none':m.extraDisplay;
    if(param)refresh(!config);
  }
  function readEditor(){
    if(!mounted||!config)return;
    const r=mounted.root,b=config.bands[selected];
    for(const input of r.querySelectorAll('input[type=number]'))if(input.value.trim()===''||!input.checkValidity())throw Error(input.getAttribute('aria-label')+'的范围是 '+input.min+'–'+input.max+'，离开输入框后会自动调整。');
    b.frequency=Number(r.querySelector('#frequency').value);b.gain=Number(r.querySelector('#gain').value);b.q=Number(r.querySelector('#q').value);
    b.type=r.querySelector('#type').value;b.enabled=r.querySelector('#band-enabled').checked;
    config.enabled=r.querySelector('#enabled').checked;config.preamp=Number(r.querySelector('#preamp').value);
  }
  function draw(){
    if(!mounted||!config)return;
    const r=mounted.root,rate=(lastState&&lastState.rate)||48000,cs=config.bands.map(b=>coefficients(b,rate));
    // Include filter centers and local detail so high-Q bands cannot fall between plot samples.
    const frequencies=new Set();for(let i=0;i<=220;i++)frequencies.add(20*Math.pow(1000,i/220));
    for(const band of config.bands)for(const offset of [-2,-1,-.5,0,.5,1,2])frequencies.add(Math.max(20,Math.min(20000,band.frequency*Math.pow(2,offset/band.q))));
    const points=[...frequencies].sort((a,b)=>a-b).map(f=>{let db=config.preamp;for(const c of cs)db+=response(c,Math.min(f,rate*.49999),rate);return (32+Math.log(f/20)/Math.log(1000)*468).toFixed(2)+','+(65-Math.max(-24,Math.min(24,db))*2).toFixed(2);});
    r.querySelector('.curve').setAttribute('points',points.join(' '));
    r.querySelector('.dots').innerHTML=config.bands.map((b,i)=>'<circle data-band="'+i+'" cx="'+(32+Math.log(b.frequency/20)/Math.log(1000)*468)+'" cy="'+(65-Math.max(-24,Math.min(24,b.gain+config.preamp))*2)+'" r="'+(i===selected?5:3)+'" opacity="'+(b.enabled?1:.35)+'"/>').join('');
    r.querySelectorAll('.band').forEach((button,i)=>{const b=config.bands[i];button.setAttribute('aria-pressed',String(i===selected));button.querySelector('strong').textContent=frequencyLabel(b.frequency);button.querySelector('small').textContent=(b.gain>0?'+':'')+b.gain+' dB';button.style.opacity=b.enabled?'1':'.45';});
  }
  function render(){
    if(!mounted||!config)return;
    const r=mounted.root,b=config.bands[selected];
    r.querySelector('#enabled').checked=config.enabled;r.querySelector('#preamp').value=config.preamp;
    r.querySelector('#frequency').value=b.frequency;r.querySelector('#gain').value=b.gain;r.querySelector('#q').value=b.q;
    r.querySelector('#type').value=b.type;r.querySelector('#band-enabled').checked=b.enabled;
    r.querySelectorAll('input[type=number]').forEach(input=>{input.dataset.lastValid=input.value;input.setAttribute('aria-invalid','false');});
    r.querySelector('.band-title').textContent='频段 '+(selected+1);draw();
  }
  function normalize(input,commit){
    if(input.type!=='number')return '';
    const lo=Number(input.min),hi=Number(input.max),step=Number(input.step)||1;
    let value=input.value.trim()===''?NaN:Number(input.value),notice='';
    if(!Number.isFinite(value)){
      if(!commit)return '';
      value=Number(input.dataset.lastValid);if(!Number.isFinite(value))value=lo;
      notice='已恢复上一次有效的'+input.getAttribute('aria-label')+'：'+value;
    }
    if(value>hi||((commit||lo<0)&&value<lo)){
      value=Math.max(lo,Math.min(hi,value));notice=input.getAttribute('aria-label')+'范围为 '+lo+'–'+hi+'，已调整为 '+value+((input.id==='gain'||input.id==='preamp')?' dB':'');
    }
    if(commit)value=Number((Math.round((value-lo)/step)*step+lo).toFixed(4));
    if(notice||commit)input.value=String(value);
    input.setAttribute('aria-invalid',String(!input.checkValidity()));
    if(input.checkValidity())input.dataset.lastValid=input.value;
    return notice;
  }
  function syncTheme(){
    if(!mounted)return;
    const s=getComputedStyle(mounted.nativeTab);
    if(mounted.host.style.color!==s.color)mounted.host.style.color=s.color;
    if(mounted.host.style.fontFamily!==s.fontFamily)mounted.host.style.fontFamily=s.fontFamily;
    const slider=mounted.pane.querySelector('.cmd-slider-track');
    if(slider){const c=getComputedStyle(slider).backgroundColor;if(c&&c!=='rgba(0, 0, 0, 0)')mounted.host.style.setProperty('--peq-accent',c);}
  }
  async function refresh(initial){
    syncTheme();
    if(busy||disposed||!mounted)return;busy=true;const target=mounted;
    try{const s=await transport.request('eq',null);if(target!==mounted||disposed)return;lastState=s;if(initial){config=clone(s.config);if(!config.bands.length)config.bands=[{enabled:true,type:'bell',frequency:1000,gain:0,q:1}];while(config.bands.length<10)config.bands.push({enabled:false,type:'bell',frequency:1000,gain:0,q:1});render();}status();}
    catch(e){if(target===mounted)report(e.message);}finally{busy=false;}
  }
  function mount(){
    if(disposed)return;
    const candidates=[...document.querySelectorAll('[role=tabpanel]')].filter(p=>/\/audioEffect\/soundEffect$/.test(p.id)&&p.getAttribute('aria-hidden')!=='true');
    const pane=candidates.length===1?candidates[0]:null;
    if(mounted&&mounted.pane===pane&&mounted.host.isConnected)return;
    release();if(!pane)return;
    const nativeTab=[...document.querySelectorAll('[role=tab]')].find(t=>/\/audioEffect\/soundEffect$/.test(t.id));
    if(!nativeTab||nativeTab.getAttribute('aria-selected')!=='true')return;
    const host=document.createElement('div');host.id='ncm-parametric-equalizer';
    const sourceStyle=getComputedStyle(nativeTab),slider=pane.querySelector('.cmd-slider-track');
    host.style.cssText='display:block;color:'+sourceStyle.color+';font-family:'+sourceStyle.fontFamily+';font-size:13px;';
    const accent=slider&&getComputedStyle(slider).backgroundColor;
    host.style.setProperty('--peq-accent',accent&&accent!=='rgba(0, 0, 0, 0)'?accent:'#36c6f4');
    const root=host.attachShadow({mode:'open'});
    root.innerHTML=`<style>
      :host{display:block}*{box-sizing:border-box}button,input,select{font:inherit;color:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--peq-accent);outline-offset:2px}
      [hidden]{display:none!important}.switcher{display:flex;align-items:center;gap:4px;margin:0 0 15px;padding-top:12px}.switcher button{border:0;background:transparent;opacity:.55;padding:5px 12px;border-radius:16px}.switcher button[aria-pressed=true]{background:rgba(127,127,127,.16);opacity:1;font-weight:600}.state{margin-left:auto;font-size:11px;opacity:.6}
      .editor{padding:0 24px 0 24px}.switcher{padding-left:24px;padding-right:24px}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}.enable{display:flex;align-items:center;gap:7px;font-weight:600}.enable input{accent-color:var(--peq-accent)}.preamp{font-size:12px;opacity:.8}.preamp input{width:55px}
      input[aria-invalid=true]{border-color:#ef6570}input[type=number],select{background:rgba(127,127,127,.08);border:1px solid rgba(127,127,127,.25);border-radius:6px;padding:5px 7px;min-width:0;width:100%;outline-offset:1px}select option{color:#222;background:#fff}.preamp input{width:55px;padding:3px}
      .graph{display:block;width:100%;height:136px;background:rgba(127,127,127,.06);border:1px solid rgba(127,127,127,.12);border-radius:8px}.grid{stroke:currentColor;opacity:.1;stroke-width:1}.labels{fill:currentColor;opacity:.4;font-size:10px}.curve{stroke:var(--peq-accent);stroke-width:2;fill:none}.dots{fill:var(--peq-accent);cursor:pointer}
      .bands{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin:10px 0}.band{background:transparent;border:1px solid rgba(127,127,127,.18);border-radius:6px;padding:4px 2px;display:flex;justify-content:center;gap:5px;align-items:baseline}.band[aria-pressed=true]{border-color:var(--peq-accent);background:rgba(127,127,127,.1)}.band strong{font-size:11px;font-weight:500}.band small{font-size:10px;opacity:.5}
      .band-header{display:flex;align-items:center;gap:10px;font-size:12px;margin:8px 0}.band-header select{width:124px;padding:3px 7px}.band-title{font-weight:600}.band-header label{margin-left:auto;opacity:.7}.band-header input{accent-color:var(--peq-accent)}.fields{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.fields label{font-size:11px;opacity:.8}.fields input{display:block;margin-top:4px}
      .footer{display:flex;align-items:center;gap:8px;margin-top:12px}.footer button{border:1px solid rgba(127,127,127,.3);border-radius:16px;background:transparent;padding:5px 13px;font-size:12px}.footer #apply{border-color:var(--peq-accent);background:var(--peq-accent);color:#102028;margin-left:auto}.message{font-size:11px;opacity:.6;min-height:17px;padding:7px 0 4px;white-space:normal}.editor{max-height:calc(100vh - 130px);overflow:auto}button:disabled{opacity:.4;cursor:default}
    </style><div class="switcher" role="group" aria-label="均衡器类型"><button data-mode="standard" aria-pressed="true">标准均衡器</button><button data-mode="parametric" aria-pressed="false">参数 EQ</button><span class="state"></span></div>
    <section class="editor" hidden><div class="toolbar"><label class="enable"><input id="enabled" type="checkbox">启用参数 EQ</label><label class="preamp">总增益（−30–0 dB） <input id="preamp" title="总增益范围 −30～0 dB，超出后自动调整" type="number" min="-30" max="0" step="0.1" aria-label="总增益"> dB</label></div>
    <svg class="graph" viewBox="0 0 520 142" role="img" aria-label="均衡器频率响应预览"><g class="grid"><path d="M32 17H500 M32 65H500 M32 113H500 M32 17V113 M141 17V113 M297 17V113 M453 17V113 M500 17V113"/></g><g class="labels"><text x="4" y="21">+24</text><text x="12" y="69">0</text><text x="4" y="117">−24</text><text x="30" y="134">20</text><text x="132" y="134">100</text><text x="291" y="134">1k</text><text x="445" y="134">10k</text><text x="481" y="134">20k Hz</text></g><polyline class="curve" points=""/><g class="dots"></g></svg>
    <div class="bands">${Array.from({length:10},(_,i)=>'<button class="band" data-band="'+i+'" aria-label="选择频段 '+(i+1)+'"><strong>—</strong><small>0 dB</small></button>').join('')}</div>
    <div class="band-header"><span class="band-title">频段 1</span><select id="type" aria-label="滤波类型"><option value="bell">钟形</option><option value="low_shelf">低音搁架</option><option value="high_shelf">高音搁架</option></select><label><input id="band-enabled" type="checkbox">启用此频段</label></div>
    <div class="fields"><label>频率 · Hz<input id="frequency" type="number" min="20" max="20000" step="1" aria-label="频率"></label><label>增益 · −18～+18 dB<input id="gain" title="频段增益范围 −18～+18 dB，超出后自动调整" type="number" min="-18" max="18" step="0.1" aria-label="增益"></label><label>Q 值 · 0.2–20<input id="q" type="number" min="0.2" max="20" step="0.01" aria-label="Q 值"></label></div>
    <div class="footer"><button id="apply">应用并保存</button></div><div class="message" role="status">仅对独占输出生效，请关闭原有 EQ 和其他音效。</div></section>`;
    const extra=nativeTab.parentElement.querySelector('.cmd-tabs-bar-extra');
    mounted={pane,host,root,nativeTab,paneHeight:pane.style.height,paneOverflow:pane.style.overflowY,hidden:[...pane.children].map(node=>({node,display:node.style.display})),extra,extraDisplay:extra?extra.style.display:''};
    pane.style.height='100%';pane.style.overflowY='auto';
    pane.insertBefore(host,pane.firstChild);
    root.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;applyMode();});
    root.querySelectorAll('[data-band]').forEach(b=>b.onclick=()=>{try{readEditor();selected=Number(b.dataset.band);render();}catch(e){report(e.message);}});
    root.querySelector('.dots').onclick=e=>{if(e.target.dataset.band!==undefined){try{readEditor();selected=Number(e.target.dataset.band);render();}catch(error){report(error.message);}}};
    root.querySelectorAll('input,select').forEach(input=>{
      function edit(commit){const notice=normalize(input,commit);try{readEditor();draw();report(notice||(input.id==='q'&&config.bands[selected].gain===0?'当前频段增益为 0 dB，改变 Q 不会改变曲线。':'有未应用的更改'));}catch(e){report(e.message);}}
      input.addEventListener('input',()=>edit(false));
      if(input.type==='number'){
        input.addEventListener('blur',()=>edit(true));
        input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();edit(true);}});
      }
    });
    root.querySelector('#apply').onclick=async()=>{
      if(busy||!config)return;const target=mounted;busy=true;root.querySelector('#apply').disabled=true;
      try{root.querySelectorAll('input[type=number]').forEach(input=>normalize(input,true));readEditor();draw();const s=await transport.request('eq',clone(config));if(target!==mounted)return;lastState=s;config=clone(s.config);status();report('已应用并保存');}
      catch(e){if(target===mounted)report(e.message);}finally{busy=false;if(target===mounted)root.querySelector('#apply').disabled=false;}
    };
    applyMode();if(config)render();status();
  }
  const observer=new MutationObserver(mount);observer.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['aria-selected','aria-hidden']});mount();
  const timer=setInterval(()=>{if(mounted&&mode==='parametric')refresh(false);},2000);
  transport.disposeEqualizer=()=>{disposed=true;observer.disconnect();clearInterval(timer);release();};
}
