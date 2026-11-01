const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const path=require('node:path');
const root=path.resolve(__dirname,'../widget');
const origin='http://phone-ranker.test';
const image=id=>({id,name:id+'.png',rating:1000,matches:1,kind:'image',url:'/api/image/'+id,poster_url:'/api/image/'+id});
const initial={pair:{id:'p1',left:image('left'),right:image('right')},top:Array.from({length:100},(_,i)=>image('rank'+i)),stats:{images:102,rated:100,votes:100,scanning:false}};
async function setup(t,hook=async()=>false){
 const browser=await chromium.launch({headless:true});t.after(()=>browser.close());
 const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
 const calls=[];
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  assert.equal(url.origin,origin,'phone UI does not contact foreign origins');
  if(await hook(route,request,url))return;
  if(url.pathname.startsWith('/api/')){
   calls.push({url:url.href,method:request.method(),body:request.postDataJSON()});
   if(url.pathname.startsWith('/api/image/'))return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#477c92"/></svg>'});
   return route.fulfill({json:request.method()==='POST'?{...initial,pair:{id:'p2',left:image('third'),right:image('right')}}:initial});
  }
  if(url.pathname==='/defaults.js')return route.fulfill({contentType:'application/javascript',body:'window.ImageRankerPhoneMode=true;window.ImageRankerDefaults={endpoint:location.origin,apiToken:""};'});
  const file=url.pathname==='/'?'index.html':url.pathname.slice(1);
  try{
   let body=await fs.readFile(path.join(root,file));
   if(file==='index.html')body=Buffer.from(body.toString().replace('</head>','<link rel="stylesheet" href="phone.css" /></head>').replace('</main>','<form class="phone-session" method="post" action="/logout"><button>Disconnect phone</button></form></main>'));
   await route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.json')?'application/json':'text/html',body});
  }catch{await route.fulfill({status:404,body:'missing fixture asset'});}
 });
 await page.goto(origin,{waitUntil:'domcontentloaded'});
 return {page,calls};
}

test('expired phone authentication clears private media and returns to pairing',async t=>{
 let reads=0,expired=false;
 const {page}=await setup(t,async(route,request,url)=>{
  if(url.pathname==='/api/state'&&++reads>1){expired=true;await route.fulfill({status:401,json:{error:'Session expired'}});return true;}
  if(url.pathname==='/'&&expired){await route.fulfill({contentType:'text/html',body:'<h1 id="pair-again">Pair your phone</h1>'});return true;}
  return false;
 });
 await page.waitForSelector('#compare button:not([disabled])');
 await page.evaluate(()=>{ImageRanker.poll();});
 await page.waitForSelector('#pair-again',{timeout:2000});
 assert.equal(await page.locator('img,video').count(),0);
});

test('phone comparison, favorites and disconnect controls fit touch viewports',async t=>{
 const {page}=await setup(t);await page.waitForSelector('#compare button:not([disabled])');
 for(const [width,height] of [[320,568],[360,640],[390,844],[430,932],[844,390]]){
  await page.setViewportSize({width,height});
  for(const mode of ['compare','top5']){
   await page.evaluate(mode=>{window.viewMode=mode;icueEvents.onDataUpdated();},mode);
   const nav=await page.locator('nav button').first().boundingBox();assert.ok(nav.height<=52,'phone header controls scale down from native monitor size');
   const selector='nav button,#recovery:not([hidden]) button,#pager:not([hidden]) button,.phone-session button,'+(mode==='compare'?'#compare button':'#top5 article');
   const boxes=await page.locator(selector).evaluateAll(nodes=>nodes.map(node=>{const b=node.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height,right:b.right,bottom:b.bottom};}));
   for(const b of boxes){assert.ok(b.w>=44&&b.h>=44,`${width}x${height} touch target`);assert.ok(b.x>=0&&b.y>=0&&b.right<=width+.1&&b.bottom<=height+.1,`${width}x${height} ${mode} viewport bounds`);}
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   if(process.env.WIDGET_SCREENSHOT_DIR&&width===390)await page.screenshot({path:path.join(process.env.WIDGET_SCREENSHOT_DIR,'phone-'+mode+'-fixtures.png')});
  }
 }
});

test('phone mode uses only same-origin cookie transport without a core API key',async t=>{
 const {page,calls}=await setup(t);
 await page.waitForSelector('#compare button:not([disabled])',{timeout:2000});
 assert.equal(await page.evaluate(()=>ImageRanker.readSettings().apiToken),'');
 assert.equal(await page.getByTestId('skip-pair').isVisible(),true);
 await page.getByTestId('left-choice').tap();
 await page.waitForFunction(()=>document.querySelector('#compare').dataset.pairId==='p2');
 assert.deepEqual(calls.find(c=>c.method==='POST').body,{pair_id:'p1',winner:'left'});
 assert.equal(calls.every(c=>!new URL(c.url).searchParams.get('token')),true);
 await page.evaluate(()=>{window.endpoint='http://different.test';icueEvents.onDataUpdated();});
 assert.equal(await page.locator('img,video').count(),0);
 assert.match(await page.getByTestId('status').textContent(),/Invalid local endpoint/);
});
