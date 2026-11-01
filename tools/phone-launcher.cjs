'use strict';
// The long-running server must never inherit an interactive Windows console:
// selecting/copying a code can otherwise block console output inside pairing.
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn,spawnSync}=require('node:child_process');
const PROJECT=path.resolve(__dirname,'..');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function readOwner(root=PROJECT){
 const owner=JSON.parse(fs.readFileSync(path.join(root,'data/phone-access.json'),'utf8'));
 if(!Number.isInteger(owner.pid)||owner.pid<=0||!/^\d{8}$/.test(owner.pairing_code)||typeof owner.url!=='string')throw new Error('Invalid phone owner status.');
 return owner;
}
function alive(pid){try{process.kill(pid,0);return true;}catch(error){if(error.code==='ESRCH')return false;throw error;}}
async function startPhone(root=PROJECT,args=[]){
 root=path.resolve(root);
 if(!Array.isArray(args)||args.some(v=>typeof v!=='string')||args.includes('--state-file'))throw new Error('Managed startup uses its private data/phone-access.json owner file.');
 const ownerFile=path.join(root,'data/phone-access.json');
 if(fs.existsSync(ownerFile)){const previous=readOwner(root);if(alive(previous.pid))throw new Error('Phone gateway already registered; run npm run phone:stop before restarting.');}
 const data=path.join(root,'data');fs.mkdirSync(data,{recursive:true});
 const childArgs=[...args],configIndex=childArgs.indexOf('--config');
 if(configIndex>=0&&childArgs[configIndex+1])childArgs[configIndex+1]=path.resolve(root,childArgs[configIndex+1]);
 const out=fs.openSync(path.join(data,'phone-gateway.stdout.log'),'a');
 const err=fs.openSync(path.join(data,'phone-gateway.stderr.log'),'a');
 let child;
 // Windows console/process teardown can retain a child's current directory.
 // All service paths are absolute/module-anchored; do not pin the checkout.
 try{child=spawn(process.execPath,[path.join(root,'tools/phone-server.cjs'),...childArgs],{cwd:os.homedir(),detached:true,windowsHide:true,stdio:['ignore',out,err]});}
 finally{fs.closeSync(out);fs.closeSync(err);}
 let spawnError=null;child.on('error',error=>{spawnError=error;});child.unref();
 const deadline=Date.now()+7000;
 while(Date.now()<deadline){
  if(spawnError||child.exitCode!==null||child.signalCode!==null)throw new Error('Phone gateway could not start; check the private data/phone-gateway.stderr.log.');
  try{const owner=readOwner(root);if(owner.pid===child.pid)return owner;}catch{}
  await sleep(30);
 }
 try{child.kill();}catch{}
 throw new Error('Phone gateway startup deadline exceeded; check its private logs.');
}
function processCommand(pid){
 if(process.platform==='win32'){
  const script='$p=Get-CimInstance Win32_Process -Filter "ProcessId='+pid+'";if($p){$p|Select-Object Name,CommandLine|ConvertTo-Json -Compress}';
  const result=spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',script],{encoding:'utf8',windowsHide:true,shell:false});
  if(result.status!==0)throw new Error('Cannot verify phone process ownership.');
  return result.stdout.trim()?JSON.parse(result.stdout):null;
 }
 try{return {Name:path.basename(process.execPath),CommandLine:fs.readFileSync('/proc/'+pid+'/cmdline','utf8').replaceAll('\0',' ')};}catch(error){if(error.code==='ENOENT')return null;throw error;}
}
async function stopPhone(root=PROJECT){
 root=path.resolve(root);const file=path.join(root,'data/phone-access.json');
 if(!fs.existsSync(file))return false;
 const owner=readOwner(root);
 if(owner.pid===process.pid)throw new Error('Refusing to stop the controller process.');
 const info=processCommand(owner.pid);
 if(info){
  const command=String(info.CommandLine||'').replaceAll('\\','/').toLowerCase();
  const expected=path.join(root,'tools/phone-server.cjs').replaceAll('\\','/').toLowerCase();
  if(!/^node(?:\.exe)?$/i.test(info.Name)||!command.includes(expected))throw new Error('Owner PID does not belong to this phone gateway; nothing stopped.');
  process.kill(owner.pid,'SIGTERM');
  const deadline=Date.now()+5000;
  while(alive(owner.pid)&&Date.now()<deadline)await sleep(30);
  if(alive(owner.pid))throw new Error('Phone gateway did not stop; owner file retained.');
 }
 try{if(readOwner(root).pid===owner.pid)fs.unlinkSync(file);}catch(error){if(error.code!=='ENOENT')throw error;}
 return true;
}
async function main(args=process.argv.slice(2)){
 if(args.length===1&&args[0]==='--stop'){await stopPhone();console.log('Owned phone gateway stopped; the normal ranker is unchanged.');return;}
 const owner=args.length===1&&args[0]==='--show-code'?readOwner():await startPhone(PROJECT,args);
 console.log('Phone URL: '+owner.url+'\nCurrent one-time pairing code: '+owner.pairing_code+'\nThe gateway runs independently with private file logs. This viewer can be closed safely.');
}
module.exports={startPhone,stopPhone,readOwner};
if(require.main===module)main().catch(error=>{console.error('Phone launcher: '+error.message);process.exitCode=1;});
