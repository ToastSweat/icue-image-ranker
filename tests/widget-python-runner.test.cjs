const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
test('project Python runner uses the local venv without a shell',()=>{
 const r=spawnSync(process.execPath,['tools/python.cjs','-c','import sys; print(sys.executable)'],{cwd:root,encoding:'utf8'});
 assert.equal(r.status,0,r.stderr);
 assert.equal(path.resolve(r.stdout.trim()).toLowerCase(),path.join(root,'.venv','Scripts','python.exe').toLowerCase());
});
test('project Python runner forwards a failing command status',()=>{
 const r=spawnSync(process.execPath,['tools/python.cjs','-c','import sys; sys.exit(7)'],{cwd:root,encoding:'utf8'});
 assert.equal(r.status,7,r.stderr);
});
