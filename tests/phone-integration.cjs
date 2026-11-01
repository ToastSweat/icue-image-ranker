const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const path=require('node:path');
(async()=>{
 const phone=process.env.PHONE_TEST_URL,core=process.env.CORE_TEST_URL,key=process.env.CORE_TEST_TOKEN,code=process.env.PHONE_TEST_CODE;
 const browser=await chromium.launch({headless:true});
 try{
  assert.equal((await fetch(phone+'/api/state')).status,401);
  const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  const page=await context.newPage(),errors=[],urls=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>urls.push(r.url()));
  await page.goto(phone);
  const input=page.locator('input[name="code"],input[name="pairing_code"],input[type="password"]').first();
  const inputBox=await input.boundingBox(),submitBox=await page.locator('form button').first().boundingBox();
  assert.ok(inputBox.height>=44&&submitBox.height>=44,'pairing controls have phone-sized touch targets');
  await input.fill(code);
  const loginResponse=page.waitForResponse(r=>new URL(r.url()).pathname==='/login'&&r.request().method()==='POST');
  await page.locator('form button[type="submit"],form button:not([type])').first().tap();
  const login=await loginResponse;
  if(login.status()!==303)assert.fail('login status/body/origin: '+login.status()+' / '+await login.text()+' / '+(await login.request().allHeaders()).origin);
  await page.waitForSelector('#compare button:not([disabled])');
  assert.equal(await page.evaluate(()=>ImageRanker.readSettings().apiToken),'');
  const cookie=(await context.cookies()).find(c=>c.httpOnly);
  assert.ok(cookie,'session cookie is HttpOnly');assert.equal(cookie.sameSite,'Strict');
  assert.equal((await page.content()).includes(key),false);
  assert.equal(urls.some(url=>url.includes(key)),false);
  const state=await page.evaluate(()=>fetch('/api/state',{cache:'no-store'}).then(r=>r.json()));
  assert.equal(JSON.stringify(state).includes(key),false);
  for(const item of [state.pair.left,state.pair.right,...state.top])for(const field of ['url','poster_url'])if(item[field])assert.equal(new URL(item[field],phone).searchParams.has('token'),false);
  await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2);
  assert.equal(await page.locator('video').evaluate(v=>v.muted&&v.defaultMuted&&v.volume===0),true);
  assert.equal(await page.evaluate(()=>caches.keys().then(v=>v.length)),0,'no private offline cache');
  assert.equal(await page.evaluate(()=>navigator.serviceWorker?navigator.serviceWorker.getRegistrations().then(v=>v.length):0),0,'no private service worker');
  const desktop=await browser.newPage({viewport:{width:840,height:344}});
  await desktop.addInitScript(({core,key})=>{window.endpoint=core;window.apiToken=key;},{core,key});
  await desktop.goto(core);await desktop.waitForSelector('#compare button:not([disabled])');
  const initial=await page.getByTestId('compare-cards').getAttribute('data-pair-id');
  await page.getByTestId('left-choice').tap();
  await page.waitForFunction(id=>document.querySelector('#compare').dataset.pairId!==id,initial);
  await desktop.evaluate(()=>ImageRanker.poll());
  assert.equal(await desktop.getByTestId('compare-cards').getAttribute('data-pair-id'),await page.getByTestId('compare-cards').getAttribute('data-pair-id'));
  const saved=await (await fetch(core+'/api/state',{headers:{Authorization:'Bearer '+key}})).json();assert.equal(saved.stats.votes,1);
  for(const file of ['/config.json','/defaults.example.js','/.git/config','/data/rankings.sqlite3','/api/export']){
   const status=await page.evaluate(file=>fetch(file).then(r=>r.status),file);assert.ok([403,404].includes(status),'private path denied: '+file);
  }
  const dir=path.join(__dirname,'../verification');await fs.mkdir(dir,{recursive:true});await page.screenshot({path:path.join(dir,'phone-real-stack-fixtures.png')});
  await page.locator('.phone-session button').tap();
  await page.waitForSelector('input[name="code"],input[name="pairing_code"],input[type="password"]');
  assert.equal(await page.locator('img,video').count(),0);
  assert.equal(await page.evaluate(()=>fetch('/api/state').then(r=>r.status)),401);
  assert.deepEqual(errors,[]);
  console.log('Phone full-stack passed: pairing/cookie auth, real muted clip+images, vote saved once, desktop sync, no corekey/media cache/private paths, logout revokes access.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
