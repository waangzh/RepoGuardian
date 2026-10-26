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
  const {default:Component}=await server.ssrLoadModule('/src/components/review/NativeReview.vue');
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
