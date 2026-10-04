// Build a PRIVATE native package from public templates plus ignored local config.
const fs=require('node:fs');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const runtimeFiles=['index.html','manifest.json','runtime.js','style.css','translation.json','resources/icon.svg'];

function stageWidget(root){
 root=path.resolve(root);
 const config=JSON.parse(fs.readFileSync(path.join(root,'config.json'),'utf8'));
 const token=config.api_token,port=config.port;
 if(typeof token!=='string'||!/^[a-zA-Z0-9_-]{32,256}$/.test(token)||new Set(token).size<8||!Number.isInteger(port)||port<1||port>65535)throw new Error('Invalid local API configuration.');
 if(typeof config.archive_root!=='string'||!path.isAbsolute(config.archive_root)||!fs.statSync(config.archive_root).isDirectory())throw new Error('Invalid local archive directory.');
 const source=path.join(root,'widget'),stage=path.join(root,'build','widget');
 fs.rmSync(stage,{recursive:true,force:true});
 fs.mkdirSync(stage,{recursive:true});
 for(const name of runtimeFiles){
  const target=path.join(stage,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(path.join(source,name),target);
 }
 const endpoint='http://127.0.0.1:'+port;
 let html=fs.readFileSync(path.join(stage,'index.html'),'utf8'),count=0;
 html=html.replace(/<meta name="x-icue-property" content="(apiToken|endpoint)"[^>]+\/>/g,(tag,name)=>{
  count++;return tag.replace(/data-default="[^"]*"/,`data-default="'${name==='apiToken'?token:endpoint}'"`);
 });
 if(count!==2)throw new Error('Connection metadata is missing.');
 fs.writeFileSync(path.join(stage,'index.html'),html);
 fs.writeFileSync(path.join(stage,'defaults.js'),'window.ImageRankerDefaults = '+JSON.stringify({endpoint,apiToken:token})+';\n');
 return stage;
}

function main(args=process.argv.slice(2)){
 const root=path.resolve(__dirname,'..');
 try{
  if(args.length>1||(args.length===1&&args[0]!=='--validate'))throw new Error('Unknown build option.');
  const stage=stageWidget(root),sdk=path.join(root,'node_modules','icuewidget-cli');
  const info=JSON.parse(fs.readFileSync(path.join(sdk,'package.json'),'utf8'));
  const cli=path.join(sdk,info.bin.icuewidget);
  const command=args[0]==='--validate'?['validate',stage]:['package',stage,'--output',path.join(root,'dist','Image Ranker.icuewidget')];
  const result=spawnSync(process.execPath,[cli,...command],{cwd:root,stdio:'inherit',shell:false});
  if(result.error)throw result.error;
  return result.status===null?1:result.status;
 }catch{
  console.error('Widget build failed. Run npm run setup, install npm dependencies, and check local config.json.');
  return 1;
 }
}
module.exports={stageWidget,main};
if(require.main===module)process.exitCode=main();
