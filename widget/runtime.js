(function(root,factory){
 const api=factory();
 if(typeof module==='object' && module.exports) module.exports=api;
 else root.ImageRanker=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
 'use strict';
 const defaults={scheduleEnabled:false,manualHide:false,startTime:'08:00',endTime:'15:00',sunday:false,monday:true,tuesday:true,wednesday:true,thursday:true,friday:true,saturday:false,viewMode:'compare',imageFit:'contain',showNames:false,showText:true,showTitle:true,showStatus:true,showRankings:true,showButtonLabels:true,buttonSize:72,playVideos:true,loopVideos:true,textColor:'#eef3f8',accentColor:'#74d6e5',backgroundColor:'#111824',transparency:100};
 const days=['sunday','monday','tuesday','wednesday','thursday','friday','saturday'];
 function visibility(settings,now=new Date()){
  if(settings.manualHide) return {visible:false,reason:'Hidden manually'};
  if(!settings.scheduleEnabled) return {visible:true,reason:''};
  const valid=t=>typeof t==='string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(t);
  if(!valid(settings.startTime)||!valid(settings.endTime)||!Number.isFinite(now.getTime())) return {visible:false,reason:'Invalid schedule — use HH:MM'};
  const minutes=t=>Number(t.slice(0,2))*60+Number(t.slice(3));
  const start=minutes(settings.startTime),end=minutes(settings.endTime),current=now.getHours()*60+now.getMinutes(),day=now.getDay();
  let visible;
  if(start===end) visible=!!settings[days[day]];
  else if(start<end) visible=!!settings[days[day]] && current>=start && current<end;
  else visible=(current>=start && !!settings[days[day]]) || (current<end && !!settings[days[(day+6)%7]]);
  return {visible,reason:visible?'':'Outside scheduled hours'};
 }
 const translationKeys=["Image Ranker", "Compare", "Top 5", "Hide", "Show", "Hidden manually", "Outside scheduled hours", "Invalid schedule — use HH:MM", "Saving vote…", "Vote failed — pair retained. Tap to retry.", "Cannot connect to local ranker", "Provisional Top 5 — rated images only", "No rated images yet — compare to start", "Tap the image you prefer", "Indexing images…", "Waiting for two images", "Choose left image", "Choose right image"];
 translationKeys.push('Image unavailable — pair retained','Invalid local endpoint — use loopback HTTP','Pair unavailable — unanswered pair retained','Video unavailable — showing still preview');
 const translated={};
 function t(key){return translated[key]||key;}
 let translator=null;
 async function translate(){
  if(typeof window.tr!=='function'||translator===window.tr)return;
  translator=window.tr;
  await Promise.all(translationKeys.map(async key=>{try{const value=await window.tr(key);if(typeof value==='string')translated[key]=value;}catch{}}));
  document.querySelectorAll('[data-tr]').forEach(node=>node.textContent=t(node.dataset.tr));
  render();
 }
 let state=null,settings={...defaults,viewMode:'compare'},started=false,pending=false,localHidden=false,generation=0,polling=false;
 let imageFailure=false,videoPreview=false,lastNativeMode=null;
 const loaded=new Set();
 function el(id){return document.getElementById(id);}
 function url(relative){return new URL(relative,settings.endpoint).href;}
 async function request(route,body){
  const target=new URL('/api/'+route,settings.endpoint);target.searchParams.set('token',settings.apiToken);
  const response=await fetch(target.href,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  if(!response.ok)throw new Error('Request failed');return response.json();
 }
 function buttons(){el('compare').querySelectorAll('button').forEach(b=>b.disabled=pending||!!state?.error||loaded.size!==2);}
 async function vote(winner){
  if(pending||state?.error||loaded.size!==2||!state?.pair||!isVisible().visible)return;
  const pairId=state.pair.id;
  generation++;pending=true;buttons();el('status').textContent=t('Saving vote…');
  try{state=await request('vote',{pair_id:pairId,winner});render();}
  catch{
   // A lost response is not proof the transaction failed. Advance only after
   // the backend confirms this exact comparison was durably answered.
   let recovered=false;
   try{const data=await request('state');if(data.last_vote?.pair_id===pairId&&data.last_vote.winner===winner){state=data;render();recovered=true;}}catch{}
   if(!recovered&&isVisible().visible)el('status').textContent=t('Vote failed — pair retained. Tap to retry.');
  }
  finally{pending=false;buttons();}
 }
 function clearImages(){
  for(const id of ['compare','top5']){releaseMedia(el(id));delete el(id).dataset.pairId;}
  loaded.clear();videoPreview=false;
 }
 function releaseMedia(container){
  container.querySelectorAll('img,video').forEach(media=>{
   media.onerror=null;media.onload=null;media.onloadeddata=null;
   if(media.tagName==='VIDEO')media.pause();
   media.removeAttribute('src');
   if(media.tagName==='VIDEO')media.load();
  });
  container.replaceChildren();
 }
 function createMedia(item,onReady=()=>{},onFailure=()=>{}){
  const media=document.createElement(item.kind==='video'?'video':'img');
  if(item.kind==='video'){
   media.muted=true;media.defaultMuted=true;media.volume=0;media.setAttribute('muted','');
   media.autoplay=settings.playVideos;media.loop=settings.loopVideos;media.playsInline=true;media.controls=false;media.preload='auto';
   media.onvolumechange=()=>{if(!media.muted)media.muted=true;if(media.volume!==0)media.volume=0;};
   media.onloadeddata=()=>{if(media.isConnected)onReady();};
  }else{media.alt='';media.onload=()=>{if(media.isConnected)onReady();};}
  media.onerror=()=>{
   if(!media.isConnected)return;
   if(item.kind==='video'&&item.poster_url){
    videoPreview=true;
    const poster=createMedia({...item,kind:'image',url:item.poster_url},onReady,onFailure);
    media.pause();media.onerror=null;media.onloadeddata=null;media.removeAttribute('src');media.load();media.replaceWith(poster);
    el('status').textContent=t('Video unavailable — showing still preview');
   }else onFailure();
  };media.src=url(item.url);
  return media;
 }
 function syncPlayback(){
  el('root').querySelectorAll('video').forEach(video=>{
   video.muted=true;video.defaultMuted=true;video.volume=0;video.loop=settings.loopVideos;video.autoplay=settings.playVideos;
   if(!settings.playVideos||!isVisible().visible||document.hidden)video.pause();
   else if(video.paused&&!video.ended)video.play().catch(()=>{});
  });
 }
 function render(){
  const viewLabel=t(settings.viewMode==='top5'?'Compare':'Top 5'),hideLabel=t(localHidden?'Show':'Hide');
  el('view').querySelector('.button-label').textContent=viewLabel;el('hide').querySelector('.button-label').textContent=hideLabel;
  el('view').setAttribute('aria-label',viewLabel);el('hide').setAttribute('aria-label',hideLabel);el('hide').setAttribute('aria-pressed',String(localHidden||settings.manualHide));
  el('root').querySelector('header>div').hidden=!settings.showText||!settings.showTitle;
  el('status').hidden=!settings.showText||!settings.showStatus;
  for(const id of ['view','hide']){el(id).querySelector('.button-label').hidden=!settings.showText||!settings.showButtonLabels;el(id).querySelector('svg').toggleAttribute('hidden',settings.showText&&settings.showButtonLabels);}
  el('root').querySelectorAll('#top5 p').forEach(node=>node.hidden=!settings.showText||!settings.showRankings);
  const visible=isVisible();
  if(!visible.visible){clearImages();el('status').textContent=t(visible.reason);return;}
  el('compare').hidden=settings.viewMode==='top5';el('top5').hidden=settings.viewMode!=='top5';
  el('root').querySelectorAll('.image-name').forEach(node=>node.hidden=!settings.showText||!settings.showNames);
  if(settings.viewMode==='top5'){
   if(el('compare').children.length)clearImages();
   el('status').textContent=t('Provisional Top 5 — rated images only')+(videoPreview?' · '+t('Video unavailable — showing still preview'):'');
   if(!state?.top?.length){const message=document.createElement('p');message.textContent=t('No rated images yet — compare to start');message.hidden=!settings.showText||!settings.showStatus;el('top5').replaceChildren(message);delete el('top5').dataset.pairId;return;}
   const key=JSON.stringify(state.top.slice(0,5));
   if(el('top5').dataset.pairId===key)return;
   el('top5').dataset.pairId=key;releaseMedia(el('top5'));videoPreview=false;
   state.top.slice(0,5).forEach((item,index)=>{
    const article=document.createElement('article'),img=createMedia(item),label=document.createElement('p');
    label.textContent='#'+(index+1)+' · '+Math.round(item.rating)+' Elo';
    const name=document.createElement('span');name.className='image-name';name.textContent=item.name;name.hidden=!settings.showText||!settings.showNames;label.hidden=!settings.showText||!settings.showRankings;label.append(name);article.append(img,label);el('top5').append(article);
   });return;
  }
  if(el('top5').children.length)clearImages();
  buttons();
  el('status').textContent=t(state?.error?'Pair unavailable — unanswered pair retained':imageFailure?'Image unavailable — pair retained':videoPreview?'Video unavailable — showing still preview':state?.pair?'Tap the image you prefer':(state?.stats?.scanning?'Indexing images…':'Waiting for two images'));
  el('root').querySelectorAll('.image-name').forEach(node=>node.hidden=!settings.showText||!settings.showNames);
  for(const side of ['left','right']){const button=el('compare').querySelector('[data-testid="'+side+'-choice"]');if(button)button.setAttribute('aria-label',t('Choose '+side+' image'));}
  const key=state?.pair?.id||'';
  if(el('compare').dataset.pairId===key)return;
  el('compare').dataset.pairId=key;
  releaseMedia(el('compare'));loaded.clear();imageFailure=false;videoPreview=false;
  if(!state?.pair)return;
  for(const side of ['left','right']){
   const item=state.pair[side],button=document.createElement('button');
   button.dataset.testid=side+'-choice';button.dataset.imageId=item.id;button.setAttribute('aria-label',t('Choose '+side+' image'));button.disabled=true;
   const img=createMedia(item,()=>{loaded.add(item.id);buttons();},()=>{imageFailure=true;el('status').textContent=t('Image unavailable — pair retained');buttons();});
   const name=document.createElement('span');name.className='image-name';name.textContent=item.name;name.hidden=!settings.showText||!settings.showNames;
   button.onclick=()=>vote(item.id);button.append(img,name);el('compare').append(button);
  }
 }
 let timer=null,lastVisible=null,lastPoll=0;
 function connectionError(){
  try{const endpoint=new URL(settings.endpoint);if(endpoint.protocol!=='http:'||!['127.0.0.1','localhost'].includes(endpoint.hostname)||endpoint.username||endpoint.password||endpoint.search||endpoint.hash)throw new Error();}
  catch{return 'Invalid local endpoint — use loopback HTTP';}
  return '';
 }
 function isVisible(){const reason=connectionError();return reason?{visible:false,reason}:visibility({...settings,manualHide:settings.manualHide||localHidden});}
 async function poll(){
  if(document.hidden||!isVisible().visible||pending||polling)return;
  lastPoll=Date.now();polling=true;const version=generation;
  try{const data=await request('state');if(version===generation&&!pending){state={...data,pair:state?.pair||data.pair};render();}}
  catch{if(version===generation&&isVisible().visible)el('status').textContent=t('Cannot connect to local ranker');}
  finally{polling=false;}
 }
 function tick(){
  const visible=isVisible().visible;
  if(visible!==lastVisible){lastVisible=visible;render();}
  if(visible&&!document.hidden&&Date.now()-lastPoll>=5000)poll();
 }
 function stop(){if(timer!==null){clearInterval(timer);timer=null;}}
 function start(){if(timer===null)timer=setInterval(tick,1000);}
 function getProperty(name){
  if(!Object.hasOwn(defaults,name)&&!['endpoint','apiToken'].includes(name))return undefined;
  if(Object.hasOwn(window,name)&&window[name]!==undefined&&window[name]!==null)return window[name];
  try{return Function('return typeof '+name+' !== "undefined" ? '+name+' : undefined')();}catch{return undefined;}
 }
 function readSettings(){
  const result={...defaults,...window.ImageRankerDefaults};
  for(const name of Object.keys(result)){const value=getProperty(name);if(value!==undefined&&value!==null)result[name]=value;}
  for(const name of ['scheduleEnabled','manualHide','showNames','showText','showTitle','showStatus','showRankings','showButtonLabels','playVideos','loopVideos',...days])result[name]=result[name]===true||result[name]===1||result[name]==='true'||result[name]==='1';
  result.viewMode=result.viewMode==='top5'?'top5':'compare';result.imageFit=result.imageFit==='cover'?'cover':'contain';
  for(const name of ['textColor','accentColor','backgroundColor'])if(!/^#[0-9a-f]{6}$/i.test(result[name]))result[name]=defaults[name];
  result.buttonSize=Number.isFinite(Number(result.buttonSize))?Math.max(44,Math.min(144,Number(result.buttonSize))):defaults.buttonSize;
  result.transparency=Number.isFinite(Number(result.transparency))?Math.max(0,Math.min(100,Number(result.transparency))):100;
  return result;
 }
 function update(){
  const next=readSettings(),nativeMode=next.viewMode;
  if(lastNativeMode===nativeMode)next.viewMode=settings.viewMode;
  lastNativeMode=nativeMode;settings=next;
  el('background').style.background=settings.backgroundColor;el('background').style.opacity=settings.transparency/100;
  el('root').style.color=settings.textColor;document.documentElement.style.setProperty('--accent',settings.accentColor);document.documentElement.style.setProperty('--image-fit',settings.imageFit);
  document.documentElement.style.setProperty('--button-size',settings.buttonSize+'px');
  document.documentElement.style.setProperty('--button-font',Math.max(14,Math.min(40,settings.buttonSize*.30))+'px');
  render();syncPlayback();translate();
 }
 function init(){
  if(started){update();return;}started=true;update();
  el('view').onclick=()=>{settings.viewMode=settings.viewMode==='top5'?'compare':'top5';render();};
  el('hide').onclick=()=>{localHidden=!localHidden;render();};
  document.addEventListener('visibilitychange',()=>{if(document.hidden){stop();clearImages();}else{render();start();poll();}});
  window.addEventListener('pagehide',()=>{stop();clearImages();});
  lastVisible=isVisible().visible;poll();start();
 }
 return {defaults,visibility,init,update,poll,readSettings,getProperty,tick,stop};
});
