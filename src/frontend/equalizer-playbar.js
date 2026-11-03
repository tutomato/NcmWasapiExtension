/* Uses the original playbar's native tag styling; never creates a floating UI. */
{
  const api=window.__codexOutputOptions, tags=new Map();
  let latest=null, stopped=false, polling=false, reconciling=false, scheduled=false;
  const nativeState=()=>typeof api.readNativeEqState==='function'?api.readNativeEqState():{enabled:false};
  function removeTags(){for(const tag of tags.values())tag.remove();tags.clear();}
  function containers(){
    const found=new Set(document.querySelectorAll('#page_pc_mini_bar .right-side > .right'));
    for(const anchor of document.querySelectorAll('[data-testid="tid_playbar_soundquality"]')){
      const container=anchor.closest('.right');if(container)found.add(container);
    }
    return found;
  }
  async function open(){
    try{
      try{localStorage.setItem('ncm-parametric-eq-open',String(Date.now()));}catch(_){}
      await api.openNativeEqualizer();
    }catch(e){for(const tag of tags.values())tag.title=e.message;}
  }
  function update(){
    if(stopped)return;
    const native=nativeState();
    if(!latest || !latest.config.enabled || native.enabled){removeTags();return;}
    const current=containers();
    for(const [parent,tag]of tags)if(!current.has(parent)||!tag.isConnected){tag.remove();tags.delete(parent);}
    for(const parent of current){
      let tag=tags.get(parent);
      if(!tag){
        const template=[...parent.querySelectorAll('.cmd-tag')].find(t=>!t.hasAttribute('data-ncm-parametric'));
        tag=template?template.cloneNode(false):document.createElement('div');
        if(!template){tag.className='cmd-tag cmd-tag-square cmd-tag-size-medium';tag.style.cssText='border:1px solid currentColor;border-radius:4px;padding:1px 6px;font:inherit;color:inherit;cursor:pointer;';}
        for(const name of ['id','data-log','data-popupid','aria-controls','aria-expanded','aria-haspopup','data-testid'])tag.removeAttribute(name);
        tag.setAttribute('data-ncm-parametric','');tag.setAttribute('data-testid','ncm-parametric-eq-playbar');
        tag.setAttribute('role','button');tag.setAttribute('tabindex','0');tag.setAttribute('aria-label','参数 EQ，打开均衡器');
        const content=document.createElement('div');content.className='cmd-tag-content';content.textContent='参数 EQ';tag.appendChild(content);
        tag.onclick=open;tag.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
        const quality=parent.querySelector('[data-testid="tid_playbar_soundquality"]');
        let reference=quality;while(reference&&reference.parentElement!==parent)reference=reference.parentElement;
        parent.insertBefore(tag,reference?reference.nextSibling:parent.firstChild);tags.set(parent,tag);
      }
      tag.title=latest.active?'参数 EQ 已启用，点击设置':'参数 EQ 已开启，等待 WASAPI 独占输出';
    }
  }
  async function reconcile(){
    if(stopped||reconciling||!latest||!latest.config.enabled||!nativeState().enabled)return;
    reconciling=true;
    try{transport.setEqState(await transport.request('eq',{action:'enable',enabled:false}));}
    catch(e){for(const tag of tags.values())tag.title=e.message;}
    finally{reconciling=false;update();}
  }
  transport.setEqState=state=>{if(stopped)return;latest=state;update();reconcile();};
  const observer=new MutationObserver(()=>{
    if(scheduled)return;scheduled=true;
    Promise.resolve().then(()=>{scheduled=false;update();});
  });
  observer.observe(document.body,{childList:true,subtree:true});
  const unsubscribe=typeof api.subscribeNativeEq==='function'?api.subscribeNativeEq(()=>{update();reconcile();}):()=>{};
  async function poll(){
    if(stopped||polling)return;polling=true;
    try{transport.setEqState(await transport.request('eq',null));}catch(_){}finally{polling=false;}
  }
  const timer=setInterval(poll,1500);poll();
  transport.disposeEqPlaybar=()=>{stopped=true;clearInterval(timer);unsubscribe();observer.disconnect();removeTags();delete transport.setEqState;};
}
