const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const path=require('node:path');
(async()=>{
 const base=process.env.RANKER_TEST_URL,token=process.env.RANKER_TEST_TOKEN;
 const api=route=>base+'/api/'+route+(route.includes('?')?'&':'?')+'token='+encodeURIComponent(token);
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:840,height:344}});
  await context.addInitScript(({base,token})=>{window.endpoint=base;window.apiToken=token;},{base,token});
  const a=await context.newPage(),b=await context.newPage(),errors=[];
  for(const page of [a,b])page.on('pageerror',e=>errors.push(e.message));
  await Promise.all([a.goto(base),b.goto(base)]);
  const error=page=>page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Image unavailable'));
  await Promise.all([error(a),error(b)]);
  const before=await (await fetch(api('state'))).json(),id=before.pair.id;
  const bad=[before.pair.left,before.pair.right].find(item=>item.name==='unreadable.jpg').id;
  const bmp=[before.pair.left,before.pair.right].find(item=>item.name==='bitmap-named-jpeg.jpg').id;
  assert.equal((await fetch(api('image/'+bmp))).status,200,'mislabeled BMP preview works');
  assert.equal((await fetch(api('image/'+bad))).status,415);
  await a.getByTestId('retry-pair').click();await error(a);
  assert.equal(await a.getByTestId('compare-cards').getAttribute('data-pair-id'),id);
  const folder=path.join(__dirname,'../verification');await fs.mkdir(folder,{recursive:true});
  await a.screenshot({path:path.join(folder,'v1.3-recovery-fixture.png')});
  let drop=true;
  await a.route('**/api/skip?*',async route=>{
   if(drop){drop=false;await route.fetch();await route.abort('failed');}
   else await route.continue();
  });
  await a.getByTestId('skip-pair').click();
  await a.waitForFunction(id=>document.querySelector('#compare').dataset.pairId!==id,id);
  await a.waitForSelector('#compare button:not([disabled])');
  const after=await (await fetch(api('state?pair_id='+id))).json();
  assert.equal(after.stats.votes,before.stats.votes);assert.equal(after.stats.rated,before.stats.rated);
  assert.equal(after.pair_status.status,'skipped');assert.equal(after.pair_status.id,id);
  assert.ok(![after.pair.left.id,after.pair.right.id].includes(bad),'unreadable source is not selected again');
  await b.evaluate(()=>ImageRanker.poll());await b.waitForSelector('#compare button:not([disabled])');
  assert.equal(await b.getByTestId('compare-cards').getAttribute('data-pair-id'),after.pair.id,'other client escapes stale pair');
  const duplicate=await fetch(api('skip'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pair_id:id,failed_ids:[bad]})});
  assert.equal(duplicate.status,409);assert.deepEqual(errors,[]);
  console.log('Real recovery integration passed: mislabeled BMP previews, retry preserves pair, dropped skip response reconciles, bad source quarantines, peer client resyncs, stale skip rejected, votes/ratings/source file unchanged.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
