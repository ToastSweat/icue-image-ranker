const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const helper=path.resolve(__dirname,'../tools/phone-launcher.cjs');
function request(url,body){return new Promise((resolve,reject)=>{const u=new URL(url);const r=http.request(u,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/x-www-form-urlencoded',Origin:u.origin,'Content-Length':Buffer.byteLength(body)}:{}},s=>{const chunks=[];s.on('data',c=>chunks.push(c));s.on('end',()=>resolve({status:s.statusCode,headers:s.headers,body:Buffer.concat(chunks).toString()}));});r.setTimeout(3000,()=>r.destroy(new Error('gateway stalled')));r.on('error',reject);r.end(body);});}
test('phone launcher detaches service stdout from the selecting console and pairing still responds',async t=>{
 assert.ok(fs.existsSync(helper),'safe phone launcher exists');
 const root=fs.mkdtempSync(path.join(process.env.TMPDIR||os.tmpdir(),'ranker-phone-launch-'));
 fs.mkdirSync(path.join(root,'tools'));fs.copyFileSync(path.resolve(__dirname,'../tools/phone-server.cjs'),path.join(root,'tools/phone-server.cjs'));
 fs.writeFileSync(path.join(root,'config.json'),JSON.stringify({port:47831,api_token:'launcher-test-only-private-key-0123456789abcdef'}));
 const api=require(helper);let owner;
 t.after(async()=>{if(owner){try{await api.stopPhone(root);}catch{try{process.kill(owner.pid);}catch{}}}fs.rmSync(root,{recursive:true,force:true,maxRetries:10,retryDelay:50});});
 owner=await api.startPhone(root,['--bind','127.0.0.1','--subnet','127.0.0.0/8','--port','0']);
 assert.notEqual(owner.pid,process.pid);
 assert.equal((await request(owner.url)).status,200);
 const response=await request(new URL('/login',owner.url),'code='+owner.pairing_code);
 assert.equal(response.status,303);
 const fresh=api.readOwner(root);assert.notEqual(fresh.pairing_code,owner.pairing_code);
 assert.equal((await request(owner.url)).status,200,'code rotation cannot block the service on console output');
 const log=fs.readFileSync(path.join(root,'data/phone-gateway.stdout.log'),'utf8');assert.ok(log.includes('Phone URL:'));
 await api.stopPhone(root);owner=null;
 assert.equal(fs.existsSync(path.join(root,'data/phone-access.json')),false);
});
