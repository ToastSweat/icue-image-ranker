const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const path=require('node:path');
(async()=>{
 const base=process.env.RANKER_TEST_URL,token=process.env.RANKER_TEST_TOKEN;
 const api=route=>base+'/api/'+route+'?token='+encodeURIComponent(token);
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1688,height:696}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(({base,token})=>{window.endpoint=base;window.apiToken=token;},{base,token});
  await page.goto(base);
  const ready=()=>page.waitForSelector('[data-testid="left-choice"]:not([disabled])');
  await ready();assert.equal(await page.locator('#compare video').count(),2);
  await page.waitForFunction(()=>[...document.querySelectorAll('#compare video')].every(v=>v.readyState>=2&&v.currentTime>.1));
  assert.equal(await page.locator('#compare video').evaluateAll(videos=>videos.every(v=>v.muted&&v.defaultMuted&&v.volume===0&&!v.controls&&v.loop)),true);
  const state=await (await fetch(api('state'))).json(),item=state.pair.left;
  assert.equal(item.kind,'video');
  const videoUrl=new URL(item.url,base).href;
  const fixture=await fs.readFile(path.join(__dirname,'fixtures/muted-test.mp4'));
  const partial=await fetch(videoUrl,{headers:{Range:'bytes=0-31'}});assert.equal(partial.status,206);
  assert.equal(partial.headers.get('content-length'),'32');assert.ok(Buffer.from(await partial.arrayBuffer()).equals(fixture.subarray(0,32)));
  const head=await fetch(videoUrl,{method:'HEAD'});assert.equal(head.status,200);assert.equal((await head.arrayBuffer()).byteLength,0);
  const invalid=await fetch(videoUrl,{headers:{Range:'bytes=999999999-'}});assert.equal(invalid.status,416);
  assert.equal((await fetch(base+'/api/video/'+item.id)).status,401);
  const poster=await fetch(new URL(item.poster_url,base));assert.equal(poster.status,200);assert.equal(poster.headers.get('content-type'),'image/jpeg');
  const before=await page.getByTestId('compare-cards').getAttribute('data-pair-id');
  await page.evaluate(()=>window.oldVideos=[...document.querySelectorAll('#compare video')]);
  await page.evaluate(()=>ImageRanker.poll());assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),before);
  for(let i=0;i<4;i++){
   await ready();const id=await page.getByTestId('compare-cards').getAttribute('data-pair-id');
   await page.getByTestId(i%2?'right-choice':'left-choice').click();
   await page.waitForFunction(id=>document.querySelector('#compare').dataset.pairId!==id,id);
  }
  assert.equal(await page.evaluate(()=>oldVideos.every(v=>v.paused&&v.getAttribute('src')===null)),true);
  await page.getByTestId('top5-toggle').click();
  await page.waitForFunction(()=>document.querySelectorAll('#top5 video').length===2&&[...document.querySelectorAll('#top5 video')].every(v=>v.readyState>=2));
  const dir=path.join(__dirname,'../verification');await fs.mkdir(dir,{recursive:true});
  await page.screenshot({path:path.join(dir,'v1.2-video-top5-fixtures.png')});
  await page.evaluate(()=>window.topVideos=[...document.querySelectorAll('#top5 video')]);
  await page.getByTestId('manual-hide').click();assert.equal(await page.locator('video,img').count(),0);
  assert.equal(await page.evaluate(()=>topVideos.every(v=>v.paused&&v.getAttribute('src')===null)),true);
  const after=await (await fetch(api('state'))).json();assert.equal(after.stats.votes,4);
  await page.getByTestId('manual-hide').click();await page.getByTestId('top5-toggle').click();await ready();
  assert.equal(await page.getByTestId('compare-cards').getAttribute('data-pair-id'),after.pair.id);
  await page.screenshot({path:path.join(dir,'v1.2-video-comparison-fixtures.png')});
  await page.evaluate(()=>{window.scheduleEnabled=true;window.startTime='bad';icueEvents.onDataUpdated();});
  assert.equal(await page.locator('video,img').count(),0);assert.deepEqual(errors,[]);
  console.log('Real video integration passed: H264+AAC fixture plays muted; MP4 hard links deduplicate; byte ranges/HEAD/auth/posters work; 4 votes persist; hide/schedule/old-pair replacement unload every video.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
