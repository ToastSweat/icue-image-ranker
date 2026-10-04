const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const http=require('node:http');
const path=require('node:path');
const root=path.join(__dirname,'../widget');
const image=(id)=>({id,name:id+'.png',rating:1000,matches:1,url:'/api/image/'+id});
const initial={pair:{id:'p1',left:image('left'),right:image('right')},top:[image('left')],stats:{images:3,votes:1,rated:1,scanning:false}};
async function setup(t,hook=async()=>{},before=async()=>{}){
 const server=http.createServer(async(req,res)=>{try{
  const filename=path.join(root,req.url==='/'?'index.html':req.url.split('?')[0]);
  const body=await fs.readFile(filename);res.setHeader('Content-Type',filename.endsWith('.js')?'application/javascript':filename.endsWith('.css')?'text/css':filename.endsWith('.json')?'application/json':'text/html');res.end(body);
 }catch{res.writeHead(404);res.end('missing widget');}});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>{server.close(r);server.closeAllConnections();}));
 const browser=await chromium.launch({headless:true});t.after(()=>browser.close());
 const page=await browser.newPage({viewport:{width:840,height:344}});const calls=[];
 await page.route('**/api/**',async route=>{
  const req=route.request();calls.push({url:req.url(),method:req.method(),body:req.postDataJSON()});
  if(await hook(route,req,calls))return;
  if(req.url().includes('/api/video/'))await route.fulfill({contentType:'video/mp4',body:await fs.readFile(path.join(__dirname,'fixtures/muted-test.mp4'))});
  else if(req.url().includes('/api/image/'))await route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#477c92"/></svg>'});
  else await route.fulfill({json:initial});
 });
 await before(page);
 await page.goto('http://127.0.0.1:'+server.address().port);
 return {page,calls};
}
async function ready(page){await page.waitForSelector('#compare button:not([disabled])');}
test('video comparison plays silently and retains the same video until an actual vote',async t=>{
 const video={...image('left'),kind:'video',url:'/api/video/left',poster_url:'/api/image/left'};
 const mixed={...initial,pair:{...initial.pair,left:video}};
 const {page,calls}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:mixed});return true;}});
 await page.waitForSelector('#compare video',{timeout:2000});await ready(page);
 await page.waitForFunction(()=>document.querySelector('#compare video').currentTime>.1);
 assert.deepEqual(await page.locator('#compare video').evaluate(v=>({muted:v.muted,defaultMuted:v.defaultMuted,volume:v.volume,loop:v.loop,controls:v.controls,inline:v.playsInline})),{muted:true,defaultMuted:true,volume:0,loop:true,controls:false,inline:true});
 if(process.env.WIDGET_SCREENSHOT_DIR){await page.setViewportSize({width:1688,height:696});await page.screenshot({path:path.join(process.env.WIDGET_SCREENSHOT_DIR,'v1.2-mixed-video-fixtures.png')});}
 await page.evaluate(()=>{window.savedVideo=document.querySelector('#compare video');});
 await page.evaluate(()=>ImageRanker.poll());assert.equal(await page.evaluate(()=>savedVideo===document.querySelector('#compare video')),true);
 assert.equal(calls.filter(c=>c.method==='POST').length,0);
 await page.evaluate(()=>{window.playVideos=false;window.loopVideos=false;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('#compare video').evaluate(v=>v.paused),true);assert.equal(await page.locator('#compare video').evaluate(v=>v.loop),false);
 await page.evaluate(()=>{window.playVideos=true;icueEvents.onDataUpdated();});
 await page.waitForFunction(()=>!document.querySelector('#compare video').paused);
 await page.getByTestId('manual-hide').click();assert.equal(await page.locator('video,img').count(),0);
 assert.deepEqual(await page.evaluate(()=>({paused:savedVideo.paused,source:savedVideo.getAttribute('src')})),{paused:true,source:null});
 const reads=calls.filter(c=>c.url.includes('/api/video/')).length;await page.evaluate(()=>ImageRanker.poll());assert.equal(calls.filter(c=>c.url.includes('/api/video/')).length,reads);
 await page.getByTestId('manual-hide').click();await ready(page);
 assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),'p1');
});

test('unsupported video falls back to its real poster without skipping the comparison',async t=>{
 const video={...image('left'),kind:'video',url:'/api/video/left',poster_url:'/api/image/left'};
 const mixed={...initial,pair:{...initial.pair,left:video}};
 const {page,calls}=await setup(t,async(route,req)=>{
  if(req.url().includes('/api/state')){await route.fulfill({json:mixed});return true;}
  if(req.url().includes('/api/video/')){await route.fulfill({contentType:'video/mp4',body:'unsupported media fixture'});return true;}
 });
 await page.waitForSelector('#compare button:not([disabled])',{timeout:2000});
 assert.equal(await page.locator('#compare video').count(),0);assert.equal(await page.locator('#compare img').count(),2);
 assert.match(await page.getByTestId('status').textContent(),/still preview/);
 assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),'p1');
 await page.evaluate(()=>ImageRanker.poll());assert.match(await page.getByTestId('status').textContent(),/still preview/);
 assert.equal(calls.filter(c=>c.method==='POST').length,0);
});

test('Top 5 videos fit every slot and scheduled hiding unloads all playback',async t=>{
 const top=Array.from({length:5},(_,i)=>({...image('rank'+i),kind:'video',url:'/api/video/rank'+i,poster_url:'/api/image/rank'+i}));
 const {page}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:{...initial,top}});return true;}});await ready(page);
 await page.getByTestId('top5-toggle').click();
 await page.waitForFunction(()=>document.querySelectorAll('#top5 video').length===5&&[...document.querySelectorAll('#top5 video')].every(v=>v.readyState>=2));
 await page.evaluate(()=>{window.savedVideos=[...document.querySelectorAll('#top5 video')];window.buttonSize=144;icueEvents.onDataUpdated();});
 await page.evaluate(()=>ImageRanker.poll());assert.equal(await page.evaluate(()=>savedVideos[0]===document.querySelector('#top5 video')),true);
 for(const [width,height] of [[840,344],[840,696],[1688,696],[2536,696],[696,416],[696,840],[696,1688],[696,2536]]){
  await page.setViewportSize({width,height});
  const boxes=await page.locator('#top5 article,#top5 video').evaluateAll(nodes=>nodes.map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom};}));
  for(const b of boxes)assert.ok(b.x>=0&&b.y>=0&&b.right<=width+.1&&b.bottom<=height+.1,`${width}x${height} video bounds`);
 }
 await page.evaluate(()=>{savedVideos[0].muted=false;savedVideos[0].volume=1;});
 await page.waitForFunction(()=>savedVideos[0].muted&&savedVideos[0].volume===0);
 await page.evaluate(()=>{window.scheduleEnabled=true;window.startTime='invalid';icueEvents.onDataUpdated();});
 assert.equal(await page.locator('video,img').count(),0);
 assert.equal(await page.evaluate(()=>savedVideos.every(v=>v.paused&&v.getAttribute('src')===null)),true);
});

test('polling preserves exact pair DOM and vote advances only after commit',async t=>{
 let finish;
 const next={...initial,pair:{...initial.pair,id:'p2',left:image('third')}};
 const {page,calls}=await setup(t,async(route,req)=>{
  if(req.method()==='POST'){await new Promise(r=>finish=r);await route.fulfill({json:next});return true;}
 });
 await ready(page);
 await page.evaluate(()=>window.savedImage=document.querySelector('#compare img'));
 await page.evaluate(()=>ImageRanker.poll());
 assert.equal(await page.evaluate(()=>window.savedImage===document.querySelector('#compare img')),true);
 await page.getByTestId('left-choice').click();
 await page.waitForFunction(()=>document.querySelectorAll('#compare button:disabled').length===2);
 assert.equal(await page.getByTestId('left-choice').getAttribute('data-image-id'),'left');
 finish();await page.waitForFunction(()=>document.querySelector('[data-testid="left-choice"]').dataset.imageId==='third');
 assert.deepEqual(calls.find(x=>x.method==='POST').body,{pair_id:'p1',winner:'left'});
});
test('lost vote response reconciles only with a durable matching vote receipt',async t=>{
 let committed=false;
 const next={...initial,last_vote:{pair_id:'p1',winner:'left'},pair:{...initial.pair,id:'p2',left:image('third')}};
 const {page,calls}=await setup(t,async(route,req)=>{
  if(req.method()==='POST'){committed=true;await route.abort('failed');return true;}
  if(committed&&req.url().includes('/api/state')){await route.fulfill({json:next});return true;}
 });
 await ready(page);await page.getByTestId('left-choice').click();
 await page.waitForFunction(()=>document.querySelector('[data-testid="left-choice"]').dataset.imageId==='third',{},{timeout:2000});
 assert.equal(calls.filter(c=>c.method==='POST').length,1);
});

test('top5 view and manual hide remove all images without advancing pending pair',async t=>{
 const {page,calls}=await setup(t);await ready(page);
 await page.getByTestId('top5-toggle').click();
 await page.waitForSelector('#top5 img');assert.equal(await page.locator('#compare img').count(),0);
 assert.match(await page.getByTestId('status').textContent(),/Provisional/);
 await page.getByTestId('manual-hide').click();
 assert.equal(await page.locator('img').count(),0);
 const before=calls.filter(c=>c.url.includes('/api/image/')).length;
 await page.evaluate(()=>ImageRanker.poll());
 assert.equal(calls.filter(c=>c.url.includes('/api/image/')).length,before);
 await page.getByTestId('top5-toggle').click();assert.equal(await page.locator('img').count(),0);
 await page.getByTestId('manual-hide').click();await ready(page);
 assert.equal(await page.getByTestId('left-choice').getAttribute('data-image-id'),'left');
});
test('native callbacks preserve false/zero and lexical settings; invalid schedule hides top5 immediately',async t=>{
 const {page,calls}=await setup(t);await ready(page);
 await page.evaluate(()=>{window.viewMode='top5';window.transparency=0;window.showNames=false;icueEvents.onDataUpdated();});
 await page.waitForSelector('#top5 img');
 assert.equal(await page.locator('#background').evaluate(e=>getComputedStyle(e).opacity),'0');
 await page.addScriptTag({content:"let scheduleEnabled=true; let startTime='bad';"});
 await page.evaluate(()=>icueEvents.onDataUpdated());
 assert.equal(await page.locator('img').count(),0);assert.match(await page.getByTestId('status').textContent(),/Invalid schedule/);
 const before=calls.filter(c=>c.url.includes('/api/image/')).length;
 await page.evaluate(()=>ImageRanker.poll());
 assert.equal(calls.filter(c=>c.url.includes('/api/image/')).length,before);
 await page.evaluate(()=>{window.scheduleEnabled=false;icueEvents.onDataUpdated();});
 await page.waitForSelector('#top5 img');
});
test('one-second schedule boundary scrubs top5 and repeated initialization does not duplicate polls',async t=>{
 const {page,calls}=await setup(t,undefined,async p=>{
  await p.clock.install({time:new Date(2026,9,5,14,59,59)});
  await p.addInitScript(()=>{window.scheduleEnabled=true;window.viewMode='top5';});
 });
 await page.waitForSelector('#top5 img');
 await page.evaluate(()=>{icueEvents.onICUEInitialized();icueEvents.onICUEInitialized();});
 await page.clock.runFor(1000);
 assert.equal(await page.locator('img').count(),0);
 assert.match(await page.getByTestId('status').textContent(),/Outside scheduled/);
 const before=calls.length;
 await page.clock.runFor(6000);assert.equal(calls.length,before);
});
test('native presentation settings update existing images without pair replacement',async t=>{
 const {page}=await setup(t);await ready(page);
 await page.evaluate(()=>{window.saved=document.querySelector('#compare img');window.imageFit='cover';window.showNames=true;window.accentColor='#ff8833';icueEvents.onDataUpdated();});
 assert.equal(await page.locator('#compare img').first().evaluate(e=>getComputedStyle(e).objectFit),'cover');
 assert.equal(await page.locator('#compare .image-name').first().textContent(),'left.png');
 assert.equal(await page.evaluate(()=>window.saved===document.querySelector('#compare img')),true);
 await page.evaluate(()=>{window.showNames=false;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('#compare .image-name:not([hidden])').count(),0);
 assert.equal(await page.evaluate(()=>ImageRanker.getProperty('alert(1)')),undefined);
});
test('native Promise translations localize controls and accessible choices',async t=>{
 const {page}=await setup(t,undefined,p=>p.addInitScript(()=>{window.tr=key=>Promise.resolve('T:'+key);}));
 await ready(page);await page.waitForFunction(()=>document.querySelector('#view').textContent==='T:Top 5');
 assert.equal(await page.getByTestId('left-choice').getAttribute('aria-label'),'T:Choose left image');
 assert.equal(await page.getByTestId('status').textContent(),'T:Tap the image you prefer');
});
test('late polling response cannot undo a committed vote',async t=>{
 let finish,reads=0;
 const next={...initial,pair:{...initial.pair,id:'p2',left:image('third')}};
 const {page}=await setup(t,async(route,req)=>{
  if(req.method()==='POST'){await route.fulfill({json:next});return true;}
  if(req.url().includes('/api/state')&&++reads===2){await new Promise(r=>finish=r);await route.fulfill({json:initial});return true;}
 });
 await ready(page);
 await page.evaluate(()=>{window.oldPoll=ImageRanker.poll();});
 await page.waitForTimeout(30);
 await page.getByTestId('left-choice').click();await page.waitForFunction(()=>document.querySelector('[data-testid="left-choice"]').dataset.imageId==='third');
 finish();await page.evaluate(()=>window.oldPoll);
 assert.equal(await page.getByTestId('left-choice').getAttribute('data-image-id'),'third');
});
test('both images must load before voting and missing image keeps pair disabled',async t=>{
 let release;
 const {page,calls}=await setup(t,async(route,req)=>{
  if(req.url().includes('/api/image/right')){await new Promise(r=>release=r);await route.fulfill({status:404,body:'missing'});return true;}
 });
 await page.waitForSelector('#compare img');assert.equal(await page.locator('#compare button:disabled').count(),2);
 await page.evaluate(()=>document.querySelector('[data-testid="left-choice"]').click());assert.equal(calls.filter(c=>c.method==='POST').length,0);
 release();await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Image unavailable'));
 assert.equal(await page.locator('#compare button:disabled').count(),2);
 assert.equal(await page.getByTestId('left-choice').getAttribute('data-image-id'),'left');
});
test('all eight EDGE slots keep compare tap targets and top5 cards within viewport',async t=>{
 const top=Array.from({length:5},(_,i)=>image('rank'+i));
 const {page}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:{...initial,top}});return true;}});await ready(page);
 const slots=[[840,344],[840,696],[1688,696],[2536,696],[696,416],[696,840],[696,1688],[696,2536]];
 for(const [width,height] of slots){
  await page.setViewportSize({width,height});
  for(const mode of ['compare','top5']){
   await page.evaluate(mode=>{window.viewMode=mode;window.showNames=true;icueEvents.onDataUpdated();},mode);
   const boxes=await page.locator(mode==='compare'?'#compare button':'#top5 article').evaluateAll(nodes=>nodes.map(e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height,right:b.right,bottom:b.bottom};}));
   if(process.env.WIDGET_SCREENSHOT_DIR && ((width===840&&height===344)||(width===696&&height===840)))await page.screenshot({path:path.join(process.env.WIDGET_SCREENSHOT_DIR,`widget-${width}x${height}-${mode}.png`)});
   assert.equal(boxes.length,mode==='compare'?2:5);
   for(const b of boxes){assert.ok(b.w>=44&&b.h>=44,`${width}x${height} ${mode} target size`);assert.ok(b.x>=0&&b.y>=0&&b.right<=width&&b.bottom<=height,`${width}x${height} ${mode} bounds`);}
  }
 }
});
test('invalid native endpoint fails closed without image requests or token disclosure',async t=>{
 const {page,calls}=await setup(t);await ready(page);
 await page.evaluate(()=>{window.endpoint='https://example.com';icueEvents.onDataUpdated();});
 assert.equal(await page.locator('img').count(),0);
 assert.match(await page.getByTestId('status').textContent(),/Invalid local endpoint/);
 const before=calls.length;await page.evaluate(()=>ImageRanker.poll());assert.equal(calls.length,before);
});
test('polling never replaces an unanswered pair even when server changes',async t=>{
 let reads=0;
 const next={...initial,pair:{...initial.pair,id:'p2',left:image('third')}};
 const {page}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:++reads===1?initial:next});return true;}});
 await ready(page);await page.evaluate(()=>ImageRanker.poll());
 assert.equal(await page.getByTestId('left-choice').getAttribute('data-image-id'),'left');
});
test('delayed native initialization applies settings even after browser fallback started',async t=>{
 const {page}=await setup(t);await ready(page);
 await page.addScriptTag({content:'let manualHide=true;'});
 await page.evaluate(()=>icueEvents.onICUEInitialized());
 assert.equal(await page.locator('img').count(),0);assert.match(await page.getByTestId('status').textContent(),/Hidden manually/);
});
test('late native translations update existing accessible labels without replacing images',async t=>{
 const {page}=await setup(t,undefined,p=>p.addInitScript(()=>{window.tr=key=>new Promise(r=>(window.translationResolvers??=[]).push(()=>r('T:'+key)));}));
 await ready(page);await page.evaluate(()=>{window.saved=document.querySelector('#compare img');window.translationResolvers.forEach(r=>r());});
 await page.waitForFunction(()=>document.querySelector('#view').textContent==='T:Top 5');
 assert.equal(await page.getByTestId('left-choice').getAttribute('aria-label'),'T:Choose left image');
 assert.equal(await page.evaluate(()=>window.saved===document.querySelector('#compare img')),true);
});
test('backend unavailable-pair report disables cached choices and keeps the unanswered pair',async t=>{
 let reads=0;
 const {page}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:++reads===1?initial:{...initial,error:'Pair source unavailable'}});return true;}});
 await ready(page);await page.evaluate(()=>ImageRanker.poll());
 assert.equal(await page.locator('#compare button:disabled').count(),2);
 assert.match(await page.getByTestId('status').textContent(),/Pair unavailable/);
 assert.equal(await page.getByTestId('left-choice').getAttribute('data-image-id'),'left');
});
test('text controls remove visible copy without replacing pending images or hiding actions',async t=>{
 const {page}=await setup(t);await ready(page);
 await page.evaluate(()=>{window.savedImage=document.querySelector('#compare img');window.showNames=true;window.showText=false;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('header strong').isVisible(),false);
 assert.equal(await page.locator('#status').isVisible(),false);
 assert.equal(await page.locator('#compare .image-name').first().isVisible(),false);
 assert.equal(await page.locator('#view .button-label').isVisible(),false);
 assert.equal(await page.getByTestId('top5-toggle').isVisible(),true);
 assert.equal(await page.getByTestId('top5-toggle').getAttribute('aria-label'),'Top 5');
 assert.equal(await page.evaluate(()=>window.savedImage===document.querySelector('#compare img')),true);
 await page.getByTestId('top5-toggle').click();await page.waitForSelector('#top5 img');
 assert.equal(await page.locator('#top5 p').first().isVisible(),false);
 await page.evaluate(()=>{window.showText=true;window.showTitle=false;window.showStatus=false;window.showRankings=false;window.showButtonLabels=false;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('header strong').isVisible(),false);assert.equal(await page.locator('#status').isVisible(),false);
 assert.equal(await page.locator('#top5 p').first().isVisible(),false);
 await page.evaluate(()=>{window.showTitle=true;window.showStatus=true;window.showRankings=true;window.showButtonLabels=true;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('header strong').isVisible(),true);assert.equal(await page.locator('#status').isVisible(),true);
 assert.equal(await page.locator('#top5 p').first().isVisible(),true);
 assert.equal(await page.locator('#view .button-label').isVisible(),true);
});

test('button size slider scales live and remains usable at every EDGE slot',async t=>{
 const top=Array.from({length:5},(_,i)=>image('rank'+i));
 const {page}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:{...initial,top}});return true;}});await ready(page);
 assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),72);
 await page.evaluate(()=>{window.buttonSize=44;icueEvents.onDataUpdated();});
 assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),44);
 const slots=[[840,344],[840,696],[1688,696],[2536,696],[696,416],[696,840],[696,1688],[696,2536]];
 for(const [width,height] of slots){
  await page.setViewportSize({width,height});
  for(const size of [44,144])for(const text of [true,false])for(const mode of ['compare','top5']){
   await page.evaluate(({size,text,mode})=>{window.buttonSize=size;window.showText=text;window.showNames=true;window.viewMode=mode;icueEvents.onDataUpdated();},{size,text,mode});
   const boxes=await page.locator('nav button,'+(mode==='compare'?'#compare button':'#top5 article')).evaluateAll(nodes=>nodes.map(e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height,right:b.right,bottom:b.bottom};}));
   for(const b of boxes){assert.ok(b.w>=44&&b.h>=44,`${width}x${height} ${mode} size${size} target size`);assert.ok(b.x>=0&&b.y>=0&&b.right<=width+.1&&b.bottom<=height+.1,`${width}x${height} ${mode} size${size} bounds`);}
   assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),size);
   if(process.env.WIDGET_SCREENSHOT_DIR&&width===2536&&height===696&&size===144&&mode==='compare'){await ready(page);await page.screenshot({path:path.join(process.env.WIDGET_SCREENSHOT_DIR,`v1.1-large-text-${text?'on':'off'}.png`)});}
  }
 }
 await page.evaluate(()=>{window.buttonSize=999;icueEvents.onDataUpdated();});assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),144);
 await page.evaluate(()=>{window.buttonSize=-3;icueEvents.onDataUpdated();});assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),44);
 await page.evaluate(()=>{window.buttonSize=NaN;icueEvents.onDataUpdated();});assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),72);
});

test('transparency composites all backgrounds over wallpaper without fading images',async t=>{
 const {page}=await setup(t);await ready(page);
 const src=page.url();await page.evaluate(()=>ImageRanker.stop());
 await page.setContent(`<body style="margin:0;background:rgb(210,110,30)"><iframe src="${src}" style="width:840px;height:344px;border:0;display:block"></iframe></body>`);
 const frame=page.frames().find(f=>f.parentFrame()===page.mainFrame());
 await frame.waitForSelector('#compare button:not([disabled])');
 const {spawnSync}=require('node:child_process');
 async function pixels(points){
  const png=await page.screenshot();
  const r=spawnSync(process.execPath,[path.join(__dirname,'../tools/python.cjs'),'-c','import sys,json,io; from PIL import Image; im=Image.open(io.BytesIO(sys.stdin.buffer.read())).convert("RGB"); print(json.dumps([im.getpixel(tuple(p)) for p in json.loads(sys.argv[1])]))',JSON.stringify(points)],{input:png});
  assert.equal(r.status,0,r.stderr.toString());return JSON.parse(r.stdout.toString());
 }
 await frame.addScriptTag({content:'let transparency=0;'});
 await frame.evaluate(()=>{window.backgroundColor='#223344';icueEvents.onDataUpdated();});
 const box=await frame.locator('#compare button').first().boundingBox();
 const points=[[2,2],[Math.round(box.x+12),Math.round(box.y+12)],[Math.round(box.x+box.width/2),Math.round(box.y+box.height/2)]];
 const zero=await pixels(points);
 assert.deepEqual(zero[0],[210,110,30]);assert.deepEqual(zero[1],[210,110,30]);assert.deepEqual(zero[2],[71,124,146]);
 await frame.evaluate(()=>{transparency=50;icueEvents.onDataUpdated();});const half=await pixels(points);
 for(const i of [0,1])for(let c=0;c<3;c++)assert.ok(Math.abs(half[i][c]-([210,110,30][c]+[34,51,68][c])/2)<=1,'half-opacity pixel');
 assert.deepEqual(half[2],zero[2]);
 await frame.evaluate(()=>{transparency=100;icueEvents.onDataUpdated();});const opaque=await pixels(points);
 assert.deepEqual(opaque[0],[34,51,68]);assert.deepEqual(opaque[1],[34,51,68]);assert.deepEqual(opaque[2],zero[2]);
 await frame.evaluate(()=>{window.viewMode='top5';transparency=0;icueEvents.onDataUpdated();});await frame.waitForSelector('#top5 img');
 const card=await frame.locator('#top5 article').first().boundingBox();const topPixel=await pixels([[Math.round(card.x+card.width/2),Math.round(card.y+12)]]);
 assert.deepEqual(topPixel[0],[210,110,30]);
 if(process.env.WIDGET_SCREENSHOT_DIR)await page.screenshot({path:path.join(process.env.WIDGET_SCREENSHOT_DIR,'transparency-wallpaper.png')});
});

test('text-free empty leaderboard hides its message and lexical settings are honored',async t=>{
 const {page}=await setup(t,async(route,req)=>{if(req.url().includes('/api/state')){await route.fulfill({json:{...initial,top:[]}});return true;}});await ready(page);
 await page.addScriptTag({content:'let showText=false; let buttonSize=120;'});
 await page.evaluate(()=>{window.viewMode='top5';icueEvents.onDataUpdated();});
 assert.equal(await page.locator('#top5').innerText(),'');
 assert.equal(await page.locator('#view svg').isVisible(),true);
 assert.equal(Math.round((await page.getByTestId('top5-toggle').boundingBox()).height),120);
 await page.evaluate(()=>{showText=true;icueEvents.onDataUpdated();});
 assert.match(await page.locator('#top5').innerText(),/No rated images/);
});

test('native accent and background opacity affect only their intended layers',async t=>{
 const {page}=await setup(t);await ready(page);
 await page.evaluate(()=>{window.accentColor='#ff8833';window.backgroundColor='#223344';window.textColor='#ffffff';window.transparency=50;icueEvents.onDataUpdated();});
 await page.getByTestId('left-choice').focus();
 assert.equal(await page.getByTestId('left-choice').evaluate(e=>getComputedStyle(e).outlineColor),'rgb(255, 136, 51)');
 assert.equal(await page.locator('#background').evaluate(e=>getComputedStyle(e).opacity),'0.5');
 assert.equal(await page.getByTestId('root').evaluate(e=>getComputedStyle(e).opacity),'1');
 assert.equal(await page.locator('#compare img').first().evaluate(e=>getComputedStyle(e).opacity),'1');
});
test('optional names apply to top5 too without introducing markup from filenames',async t=>{
 const {page}=await setup(t);await ready(page);
 await page.evaluate(()=>{window.viewMode='top5';window.showNames=true;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('#top5 .image-name').first().textContent(),'left.png');
 await page.evaluate(()=>{window.showNames=false;icueEvents.onDataUpdated();});
 assert.equal(await page.locator('#top5 .image-name:not([hidden])').count(),0);
});
test('native host waits for initialization so persisted privacy settings cannot leak startup images',async t=>{
 const {page,calls}=await setup(t,undefined,p=>p.addInitScript(()=>{window.iCUE_initialized=false;}));
 assert.equal(calls.length,0);
 await page.evaluate(()=>{window.manualHide=true;window.iCUE_initialized=true;icueEvents.onICUEInitialized();});
 assert.equal(await page.locator('img').count(),0);assert.equal(calls.length,0);
 assert.match(await page.getByTestId('status').textContent(),/Hidden manually/);
});
test('comparison renders two loaded image vote targets with local assets',async t=>{
 const {page}=await setup(t);await ready(page);
 assert.equal(await page.locator('#compare button img').count(),2);
 assert.equal(await page.locator('#view').textContent(),'Top 5');
});
test('pending votes disable both choices; duplicate tap is ignored and failed vote retains pair',async t=>{
 let finish;let votes=0;
 const {page}=await setup(t,async(route,req)=>{
  if(req.method()==='POST'){votes++;await new Promise(r=>finish=r);await route.fulfill({status:503,json:{error:'offline'}});return true;}
 });
 await ready(page);
 await page.evaluate(()=>{document.querySelector('[data-testid="left-choice"]').click();document.querySelector('[data-testid="right-choice"]').click();});
 await page.waitForFunction(()=>document.querySelectorAll('#compare button:disabled').length===2);
 assert.equal(votes,1);
 finish();await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Vote failed'));
 assert.equal(await page.locator('[data-testid="left-choice"]').getAttribute('data-image-id'),'left');
 assert.equal(await page.locator('#compare button:disabled').count(),0);
});
