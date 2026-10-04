const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const {spawnSync}=require('node:child_process');
const root=path.join(__dirname,'../widget');
test('native package declares documented controls with translated XML-safe metadata',()=>{
 assert.ok(fs.existsSync(path.join(root,'manifest.json')),'native manifest exists');
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json'),'utf8'));
 assert.equal(manifest.min_app_version,'5.47');assert.equal(manifest.min_framework_version,'1.4.0');assert.equal(manifest.interactive,true);assert.deepEqual(manifest.supported_devices,[{type:'dashboard_lcd'}]);
 assert.ok(fs.existsSync(path.join(root,manifest.preview_icon)));
 const html=fs.readFileSync(path.join(root,'index.html'),'utf8');assert.ok(html.startsWith('<!DOCTYPE html>'));
 const xml=spawnSync(process.execPath,[path.join(root,'../tools/python.cjs'),'-c','import sys,xml.etree.ElementTree as E; E.fromstring(sys.stdin.read())'],{input:html.slice(0,html.indexOf('</head>')+7)+'</html>',encoding:'utf8'});assert.equal(xml.status,0,xml.stderr);
 const translations=JSON.parse(fs.readFileSync(path.join(root,'translation.json'),'utf8')).en.translation;
 for(const m of html.matchAll(/tr\('([^']+)'\)/g))assert.ok(translations[m[1]],'translation exists: '+m[1]);
 const metas=[...html.matchAll(/<meta name="x-icue-property"[^>]+\/>/g)].map(m=>Object.fromEntries([...m[0].matchAll(/([\w-]+)="([^"]*)"/g)].map(x=>[x[1],x[2]])));
 assert.ok(metas.length>=20);
 const vm=require('node:vm');const ctx={window:{}};vm.runInNewContext(fs.readFileSync(path.join(root,'defaults.example.js'),'utf8'),ctx);
 assert.equal(ctx.window.ImageRankerDefaults.apiToken,'','public template has no private key');
 const {defaults}=require('../widget/runtime.js');
 for(const meta of metas){assert.ok(['switch','slider','textfield','combobox','color'].includes(meta['data-type']));const value=vm.runInNewContext(meta['data-default']);if(meta.content==='apiToken')assert.equal(value==='',true,'public source has no private key');else if(meta.content==='endpoint')assert.equal(value,'http://127.0.0.1:47831');else assert.equal(value,defaults[meta.content],meta.content);}
 const group=JSON.parse(html.match(/id="x-icue-groups">([\s\S]*?)<\/script>/)[1]);assert.equal(group.at(-1).title,"tr('Personalization')");
 assert.match(html,/icueEvents\s*=\s*\{/);
});
