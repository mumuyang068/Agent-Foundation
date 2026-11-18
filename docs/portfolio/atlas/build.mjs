import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {validateAtlas} from './vendor/archify/renderers/atlas/validate.mjs';
import {renderAtlas} from './vendor/archify/renderers/atlas/render.mjs';
import {adaptAtlasLayout} from './atlas-layout.mjs';

const root=path.dirname(fileURLToPath(import.meta.url));
const model=JSON.parse(fs.readFileSync(path.join(root,'atlas.json'),'utf8'));
const repositoryIndex=process.argv.indexOf('--repository');
if(repositoryIndex!==-1){
 const repository=process.argv[repositoryIndex+1];
 if(!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository||''))throw Error('Expected owner/repository');
 for(const source of model.sources){
  const absolute=path.resolve(root,source.url);
  const relative=path.relative(path.resolve(root,'../../..'),absolute).replaceAll('\\','/');
  if(relative.startsWith('..'))throw Error('Source escapes repository');
  source.url=`https://github.com/${repository}/blob/main/${relative}`;
 }
}
const result=validateAtlas(model);
if(!result.ok)throw Error(JSON.stringify(result));
const html=adaptAtlasLayout(renderAtlas(model));
if(/file:\/\/\/|[A-Z]:[\\/](?:Project|Codex|Users)/.test(JSON.stringify(model)))throw Error('Nonportable link in public atlas model');
fs.writeFileSync(path.join(root,'index.html'),html);
console.log(JSON.stringify({views:model.views.length,errors:result.errors.length,warnings:result.warnings.length}));
