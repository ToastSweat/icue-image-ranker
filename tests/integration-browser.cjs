const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
(async()=>{
 const base=process.env.RANKER_TEST_URL, token=process.env.RANKER_TEST_TOKEN;
 const api=(route)=>base+'/api/'+route+'?token='+encodeURIComponent(token);
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1688,height:696}});
  const errors=[], requests=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>requests.push({kind:new URL(r.url()).pathname,method:r.method()}));
  await page.addInitScript(({base,token})=>{window.endpoint=base;window.apiToken=token;}, {base,token});
  await page.goto(base);
  const ready=()=>page.waitForSelector('[data-testid="left-choice"]:not([disabled])');
  await ready();
  const firstPair=await page.getByTestId('compare-cards').getAttribute('data-pair-id');
  await page.evaluate(()=>ImageRanker.poll());
  assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),firstPair);
  const firstWinner=await page.getByTestId('left-choice').getAttribute('data-image-id');
  let dropResponse=true;
  await page.route('**/api/vote?*',async route=>{
   if(dropResponse){dropResponse=false;await route.fetch();await route.abort('failed');}
   else await route.continue();
  });
  for(let i=0;i<12;i++){
   await ready();
   const previous=await page.getByTestId('compare-cards').getAttribute('data-pair-id');
   await page.getByTestId(i%2?'right-choice':'left-choice').click();
   await page.waitForFunction(p=>document.querySelector('[data-testid="compare-cards"]').dataset.pairId!==p,previous);
  }
  await ready();
  const state=await (await fetch(api('state'))).json();
  assert.equal(state.stats.votes,12); assert.equal(state.top.length,5);
  const duplicate=await fetch(api('vote'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pair_id:firstPair,winner:firstWinner})});
  assert.equal(duplicate.status,409);
  await page.getByTestId('top5-toggle').click();
  await page.waitForSelector('#top5 img');
  assert.equal(await page.locator('#top5 img').count(),5);
  const screenshots=path.join(__dirname,'../verification');await fs.mkdir(screenshots,{recursive:true});
  await page.screenshot({path:path.join(screenshots,'integration-top5-landscape.png')});
  await page.getByTestId('manual-hide').click();assert.equal(await page.locator('img').count(),0);
  const before=requests.filter(r=>r.kind.startsWith('/api/image/')).length;
  await page.evaluate(()=>ImageRanker.poll());
  assert.equal(requests.filter(r=>r.kind.startsWith('/api/image/')).length,before);
  await page.getByTestId('manual-hide').click();await page.getByTestId('top5-toggle').click();await ready();
  assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),state.pair.id);
  await page.setViewportSize({width:696,height:840});
  await page.screenshot({path:path.join(screenshots,'integration-compare-portrait.png')});
  await page.evaluate(()=>{window.scheduleEnabled=true;window.startTime='invalid';icueEvents.onDataUpdated();});
  assert.equal(await page.locator('img').count(),0);
  await page.reload(); // browser defaults deliberately restore unrestricted preview
  await ready();assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),state.pair.id);
  const exported=await fetch(api('export'));assert.equal(exported.status,200);
  const data=await exported.json();assert.ok(data);
  assert.equal((await fetch(base+'/api/state')).status,401);
  assert.equal((await fetch(base+'/config.json')).status,404);
  assert.equal((await fetch(base+'/server.py')).status,404);
  assert.equal((await fetch(base+'/data/rankings.sqlite3')).status,404);
  assert.deepEqual(errors,[]);
  assert.equal(requests.filter(r=>r.method==='POST').length,12);
  console.log('Real-server browser integration passed: 12 persisted fixture votes, Top 5, pair continuity, manual/schedule privacy, duplicate rejection, export, auth/static isolation, no console errors.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
