import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'vite';
import {createSSRApp, ref, toRaw} from 'vue';
import {renderToString} from '@vue/server-renderer';

test('当前图投影分离候选、覆盖、确认和未知用量',async()=>{
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType:'custom'});
 try{
  const {observedUsage,terminal,safeGitHubLink}=await server.ssrLoadModule('/src/utils/nativeReview.ts');
  const results=[{model_calls:2,input_tokens:10,output_tokens:5}];
  assert.deepEqual(observedUsage({discovery_results:results,verification_results:[]}),{calls:2,tokens:15});
  assert.deepEqual(observedUsage({discovery_results:[{model_calls:1,input_tokens:null,output_tokens:null}],verification_results:[]}),{calls:1,tokens:null});
  assert.deepEqual(observedUsage(null),{calls:null,tokens:null});
  assert.equal(terminal('failed'),true);assert.equal(terminal('verifying_issues'),false);
  assert.equal(safeGitHubLink('javascript:alert(1)'),undefined);
  assert.equal(safeGitHubLink('https://github.com.evil.test/'),undefined);
  assert.equal(safeGitHubLink('https://user:pass@github.com/a/b'),undefined);
  assert.equal(safeGitHubLink('https://github.com/a/b/pull/1'),'https://github.com/a/b/pull/1');
 }finally{await server.close();}
});

test('原生审查页面如实显示运行中、失败、取消，不从零问题推导成功',async()=>{
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType:'custom'});
 try{
  const {default:Component}=await server.ssrLoadModule('/src/components/review/ReviewWorkspace.vue');
  for(const status of ['reviewing','failed','cancelled']){
   const task={id:'a'.repeat(32),status,phase:status,error:status==='failed'?'timeout':null,
     pr:{owner:'o',repo:'r',number:1,title:'<script>alert(1)</script>'},snapshot:{head_sha:'b'.repeat(40),effective_base_sha:'c'.repeat(40)},
     configuration:{provider:'fixture',model:'fixture',options:{include_pr_discussions:false}},steps:[],changed_files:[],review_report:null};
   const html=await renderToString(createSSRApp(Component,{task}));
   assert.match(html,/未执行目标仓库|未执行目标代码/);
   assert.doesNotMatch(html,/<script>alert/);
   assert.match(html,/&lt;script&gt;/);
   assert.doesNotMatch(html,/NaN|undefined/);
   if(status==='reviewing')assert.match(html,/停止审查/);
   else assert.doesNotMatch(html,/>停止审查</);
  }
 }finally{await server.close();}
});

test('方案克隆从 Vue 原始值创建，保留选择且不修改冻结方案',()=>{
 const plan=ref({scope:{mode:'selected',primary_files:['safe.py']},focus:['correctness']});
 const clone=structuredClone(toRaw(plan.value));
 clone.scope.primary_files.push('other.py');
 assert.deepEqual(plan.value.scope.primary_files,['safe.py']);
});

test('失败分支补查入口受一次次数、剩余额度和原总期限限制',async()=>{
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true,entries:[],include:[]},appType:'custom'});
 try{
  const {resumeLabel,orderedProgress}=await server.ssrLoadModule('/src/utils/nativeReview.ts');
  const task={status:'completed_with_warnings',retry_count:0,created_at:new Date().toISOString(),
   configuration:{limits:{model_calls:24,tool_calls:80,timeout_seconds:1800}},
   review_report:{termination:'partial',discovery_results:[{status:'failed',model_calls:17,tool_calls:21}],verification_results:[]},
   steps:[{name:'later',started_at:'2026-10-27T11:56:02Z',updated_at:'2026-10-27T11:56:20Z'},
    {name:'earlier',started_at:'2026-10-27T11:56:00Z',updated_at:'2026-10-27T11:56:38Z'}]};
  assert.equal(resumeLabel(task),'补查失败分支');
  assert.equal(resumeLabel({...task,retry_count:1}),null);
  assert.equal(resumeLabel({...task,status:'cancelled'}),null);
  assert.equal(resumeLabel({...task,created_at:'2020-01-01T00:00:00Z'}),null);
  for(const result of [{status:'failed',model_calls:null,tool_calls:21},{status:'failed',model_calls:17,tool_calls:null},
   {status:'failed',model_calls:24,tool_calls:21},{status:'failed',model_calls:17,tool_calls:80}]){
   assert.equal(resumeLabel({...task,review_report:{...task.review_report,discovery_results:[result]}}),null);
  }
  assert.equal(resumeLabel({...task,status:'failed',review_report:null}),'从检查点继续');
  assert.deepEqual(orderedProgress(task).map(step=>step.name),['earlier','later']);
  assert.deepEqual(task.steps.map(step=>step.name),['later','earlier']);
 }finally{await server.close();}
});

test('活动页直接显示分支失败原因、实际用量和无候选核验边界',async()=>{
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true,entries:[],include:[]},appType:'custom'});
 try{
  const {default:Component}=await server.ssrLoadModule('/src/components/pages/ReviewActivityPage.vue');
  const failure={id:'agent',status:'failed',error:'GraphRecursionError',model_calls:17,tool_calls:21,
   input_tokens:144450,output_tokens:1315,tool_failures:12,previous_attempts:[],
   policy_exclusions:2,incomplete_searches:1,structured_output_retries:1};
  const task={phase:'failed',configuration:{limits:{model_calls:24,tool_calls:80}},
   steps:[{name:'agent',message:'behavior',status:'failed',started_at:'2026-10-27T11:56:00Z',
    updated_at:'2026-10-27T11:56:23Z',data:{...failure}},
   {name:'arbitrate',message:'arbitrate',status:'completed',started_at:null,
    updated_at:'2026-10-27T11:56:38Z',data:{candidates:0}}],
   review_report:{budget:{},assignments:[{id:'agent',reviewer:'behavior',objective:'<script>untrusted</script>',focus_paths:['safe.py']}],
    discovery_results:[failure],verification_results:[]}};
  const html=await renderToString(createSSRApp(Component,{task}));
  assert.match(html,/达到执行步数上限，未提交最终审查结果/);
  assert.match(html,/17 \/ 24/);assert.match(html,/工具失败 12 次/);
  assert.match(html,/无候选进入独立核验/);assert.match(html,/开始：/);assert.match(html,/结束：/);
  assert.match(html,/策略排除记录 2/);assert.match(html,/结构化输出纠错 1/);
  assert.match(html,/&lt;script&gt;untrusted/);assert.doesNotMatch(html,/<script>|NaN|undefined/);
 }finally{await server.close();}
});

test('整轮预算优先使用账本，共享批量结果不重复计量',async()=>{
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true,entries:[],include:[]},appType:'custom'});
 try{
  const {observedUsage}=await server.ssrLoadModule('/src/utils/nativeReview.ts');
  const result={execution_id:'shared',model_calls:2,input_tokens:10,output_tokens:5};
  const report={discovery_results:[],verification_results:[{...result,id:'a'},{...result,id:'b'}],budget:{}};
  assert.deepEqual(observedUsage(report),{calls:2,tokens:15});
  assert.deepEqual(observedUsage({...report,budget:{model_calls:3,input_tokens:100,output_tokens:20}}),{calls:3,tokens:120});
  assert.deepEqual(observedUsage({...report,budget:{model_calls:3,input_tokens:null,output_tokens:20}}),{calls:3,tokens:null});
 }finally{await server.close();}
});
