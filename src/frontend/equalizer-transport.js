/* Dedicated acknowledged transport for the native audio-effect subwindow.
 * Transport identifiers and UI source are inserted by the controller.
 */
(function () {
  const token=__TOKEN__, owner=__OWNER__, globalName=__GLOBAL__;
  const old=window[globalName];if(old&&old.dispose)old.dispose();
  const pending=new Map();let sequence=0;
  const transport={owner,installed:false,disposeEqualizer:null,
    request(kind,value){
      if(kind!=='eq'||window[globalName]!==transport)return Promise.reject(Error('EQ 窗口连接已更新'));
      return new Promise((resolve,reject)=>{
        const id=String(++sequence),timer=setTimeout(()=>{pending.delete(id);reject(Error('EQ 设置响应超时'));},10000);
        pending.set(id,{resolve,reject,timer});
        try{window[__BINDING__](JSON.stringify({token,owner,id,kind,value}));}
        catch(e){clearTimeout(timer);pending.delete(id);reject(e);}
      });
    },
    settle(id,ok,value){const p=pending.get(id);if(!p)return false;pending.delete(id);clearTimeout(p.timer);if(ok)p.resolve(value);else p.reject(Error(value||'无法应用 EQ 设置'));return true;},
    dispose(){transport.installed=false;if(transport.disposeEqualizer)transport.disposeEqualizer();for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('EQ 窗口已关闭'));}pending.clear();}
  };
  window[globalName]=transport;
  try{__UI__;transport.installed=true;return {installed:true,owner};}
  catch(e){transport.dispose();delete window[globalName];throw e;}
})()
