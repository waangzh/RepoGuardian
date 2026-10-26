import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'vite';

test('方案创建只提交预览 ID 与固定双侧 SHA，拒绝沿用旧模型参数', async () => {
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType:'custom'});
 const previous=globalThis.fetch;
 try{
  const {createReview}=await server.ssrLoadModule('/src/api/client.ts');
  const preview={preview_id:'a'.repeat(32),snapshot:{head_sha:'b'.repeat(40),effective_base_sha:'c'.repeat(40)}};
  let payload;
  globalThis.fetch=async (_url,init)=>{payload=JSON.parse(init.body);return new Response(JSON.stringify({task_id:'review',status:'queued'}),{status:202});};
  await createReview('https://github.com/o/r/pull/1',preview);
  assert.deepEqual(payload,{pr_url:'https://github.com/o/r/pull/1',preview_id:'a'.repeat(32),expected_head_sha:'b'.repeat(40),expected_effective_base_sha:'c'.repeat(40)});
 }finally{globalThis.fetch=previous;await server.close();}
});

test('方案或 PR 变化保留服务端拒绝原因',async()=>{
 const server=await createServer({server:{middlewareMode:true,hmr:false},optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType:'custom'});
 const previous=globalThis.fetch;
 try{
  const {createReview}=await server.ssrLoadModule('/src/api/client.ts');
  globalThis.fetch=async()=>new Response(JSON.stringify({detail:{code:'review_plan_stale'}}),{status:409});
  await assert.rejects(createReview('https://github.com/o/r/pull/1',{preview_id:'a',snapshot:{head_sha:'b',effective_base_sha:'c'}}),/review_plan_stale/);
 }finally{globalThis.fetch=previous;await server.close();}
});
