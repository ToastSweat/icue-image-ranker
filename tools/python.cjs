// Invoke the project-local Python by absolute path; avoid CMD/MSYS path parsing.
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const root=path.resolve(__dirname,'..');
const python=path.join(root,'.venv',process.platform==='win32'?'Scripts':'bin',process.platform==='win32'?'python.exe':'python');
if(!fs.existsSync(python)){
 console.error('Project Python is missing. Recreate .venv and install requirements.txt.');
 process.exitCode=1;
}else{
 const result=spawnSync(python,process.argv.slice(2),{cwd:root,stdio:'inherit',shell:false});
 if(result.error)console.error('Project Python could not run: '+result.error.message);
 process.exitCode=result.status===null?1:result.status;
}
