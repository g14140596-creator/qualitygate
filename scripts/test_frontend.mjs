// Dependency-free render-logic smoke tests. This is not a browser layout/E2E test.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const dirty=JSON.parse(fs.readFileSync(path.join(root,'docs/example-dirty-report.json'),'utf8'));
const clean=JSON.parse(fs.readFileSync(path.join(root,'docs/example-clean-report.json'),'utf8'));
const elements=new Map();
function element(s){if(!elements.has(s))elements.set(s,{innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},classList:{toggle(){},add(){},remove(){}},style:{setProperty(){}},addEventListener(){},showModal(){},close(){},matches(){return false},querySelector(){return element('child')},querySelectorAll(){return []}});return elements.get(s)}
const ds=[dirty,clean].map(r=>({id:r.job.dataset_id,name:r.job.dataset_name,sha256:r.job.dataset_sha256,row_count:240,byte_count:20000,columns:['order_id','customer_email','amount','status','created_at','paid_at','country_code']}));
const rs=[{id:dirty.job.ruleset_id,name:dirty.job.ruleset_name,version:1,rules:dirty.job.rules,updated_at:dirty.job.created_at}];
const fixtureJobs=[clean.job,dirty.job];
async function fetcher(url){let data;if(url==='/api/datasets')data=ds;else if(url==='/api/rulesets')data=rs;else if(url.startsWith('/api/jobs?'))data={items:fixtureJobs,total:2};else if(url.includes('/issues?'))data={items:dirty.issues.slice(0,10),total:66};else if(url.startsWith('/api/jobs/'))data=dirty.job;else throw Error('Unexpected request '+url);return {ok:true,json:async()=>data}}
const context=vm.createContext({console,fetch:fetcher,document:{querySelector:element,querySelectorAll(){return []},addEventListener(){},hidden:false},window:{addEventListener(){}},location:{hash:'#overview'},setInterval(){},setTimeout(){return 0},clearTimeout(){},URLSearchParams,structuredClone,crypto:{randomUUID(){return 'fixture-key'}}});
vm.runInContext(fs.readFileSync(path.join(root,'qualitygate/static/app.js'),'utf8'),context);
await new Promise(r=>setImmediate(r));
assert.match(element('#main').innerHTML,/每一份数据/);
assert.match(element('#main').innerHTML,/最近两次对比/);
vm.runInContext('renderDatasets()',context);assert.match(element('#main').innerHTML,/synthetic_orders_dirty/);
vm.runInContext('renderRules()',context);assert.match(element('#main').innerHTML,/订单号不能为空/);
vm.runInContext('renderGuide()',context);assert.match(element('#main').innerHTML,/20,000/);
context.location.hash='#job/'+dirty.job.id;
await vm.runInContext(`renderJob('${dirty.job.id}',renderVersion)`,context);
assert.match(element('#main').innerHTML,/问题明细/);assert.match(element('#issue-results').innerHTML,/row|行|R0/);
vm.runInContext('openRules(rulesets[0].id)',context);assert.match(element('#rule-editor').innerHTML,/data-param="dataset_id"/);
assert.equal(vm.runInContext('esc(`<img src=x onerror="alert(1)">`)',context),'&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
console.log('8 frontend render/escaping checks passed (VM + in-memory DOM stub; no browser/layout validation).');
