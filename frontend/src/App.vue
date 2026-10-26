<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRaw } from 'vue';
import { cancelReview, createReview, deleteReview, getAvailableModels, getReview, getReviewCapabilities, listReviews, resumeReview, subscribeToReview } from './api/client';
import type { ReviewPreviewResponse, ReviewTask } from './types/review';
import type { ReviewPlanOptions, ReviewCapabilities } from './types/reviewPlan';
import { usePreviewJob } from './composables/usePreviewJob';
import { phaseLabel, terminal } from './utils/nativeReview';
import NativeReview from './components/review/NativeReview.vue';
import NativeSettings from './components/pages/NativeSettings.vue';
import StatusBadge from './components/common/StatusBadge.vue';

const page = ref('review');
const prUrl = ref('');
const model = ref('');
const models = ref<string[]>([]);
const capabilities = ref<ReviewCapabilities | null>(null);
const options = ref<ReviewPlanOptions>({depth:'standard',focus:['correctness','security','performance','compatibility'],scope:{mode:'all',primary_files:[]},limits:{model_calls:null,tool_calls:null,concurrency:null,timeout_seconds:null,output_tokens:null},include_pr_discussions:true});
const selectedPaths = ref('');
const preview = ref<ReviewPreviewResponse | null>(null);
const task = ref<ReviewTask | null>(null);
const error = ref('');
const busy = ref(false);
const history = ref<ReviewTask[]>([]);
const historyPage = ref(1);
const historyTotal = ref(0);
const search = ref('');
const tracker = usePreviewJob();
const {job,tracking,connectionError} = tracker;
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let closeStream: (() => void) | null = null;
let generation = 0;
let historyGeneration = 0;
const validUrl = computed(() => /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*\/?$/.test(prUrl.value));
const selectedMode = computed({get:()=>options.value.scope.mode,set:(value:'all'|'selected')=>{options.value.scope.mode=value;preview.value=null;}});
const limitLabels: Record<keyof ReviewPlanOptions['limits'],string> = {model_calls:'每个 Agent 模型调用上限',tool_calls:'每个 Agent 工具调用上限',concurrency:'Agent 并发上限',timeout_seconds:'总期限（秒）',output_tokens:'单次输出 Token 上限'};
const focusLabels = {correctness:'正确性',security:'安全',performance:'性能',compatibility:'跨模块契约'};
function forgetTask() {
 generation++;controller?.abort();closeStream?.();closeStream=null;if(timer) clearTimeout(timer);timer=null;busy.value=false;
 task.value=null;try{localStorage.removeItem('rg-review-task');}catch{/* Session-only operation. */}
}
function newReview() {forgetTask();tracker.detach(true);preview.value=null;error.value='';page.value='review';}
function rereview() {const url=task.value?.pr_url;const plan=task.value?.configuration.options;newReview();if(url)prUrl.value=url;if(plan)options.value=structuredClone(toRaw(plan));}
async function follow(id:string) {
 tracker.detach(true);
 closeStream?.();closeStream=null;
 const owner=++generation;controller?.abort();if(timer)clearTimeout(timer);
 const abort=new AbortController();controller=abort;
 let refreshing = false;
 try{localStorage.setItem('rg-review-task',id);}catch{/* Session-only operation. */}
 async function refresh(){
  if(owner!==generation||refreshing)return;
  refreshing=true;if(timer)clearTimeout(timer);timer=null;
  try{const result=await getReview(id,abort.signal);if(owner!==generation)return;task.value=result;error.value='';if(!terminal(result.status))timer=setTimeout(refresh,1200);else {closeStream?.();closeStream=null;}}
  catch(e){if(owner!==generation||abort.signal.aborted)return;error.value=e instanceof Error?e.message:'连接中断';timer=setTimeout(refresh,3000);}
  finally{refreshing=false;}
 }
 await refresh();
 if(owner===generation&&task.value&&!terminal(task.value.status))closeStream=subscribeToReview(id,()=>{void refresh();});
}
async function prepare() {
 if(!validUrl.value||tracking.value)return;
 const owner = generation;
 preview.value=null;error.value='';busy.value=true;
 try{
  const plan=structuredClone(toRaw(options.value));
  plan.scope.primary_files=plan.scope.mode==='selected'?selectedPaths.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean):[];
  const result=await tracker.start({pr_url:prUrl.value,model:model.value||undefined,plan});
  if(owner!==generation)return;
  if(result?.status==='completed'&&result.result){preview.value=result.result;options.value=structuredClone(toRaw(result.result.effective_plan));}
  else if(result?.error)error.value=result.error;
 }catch(e){if(owner===generation)error.value=e instanceof Error?e.message:'准备失败';}finally{if(owner===generation)busy.value=false;}
}
async function restorePreview() {
 try{const result=await tracker.resume(async value=>{prUrl.value=value.request.pr_url;model.value=value.request.model||'';if(value.request.plan)options.value=structuredClone(toRaw(value.request.plan));});
  if(result?.status==='completed')preview.value=result.result;
  else if(result?.status==='consumed'&&result.consumed_task_id)await follow(result.consumed_task_id);
 }catch(e){error.value=e instanceof Error?e.message:'恢复预览失败';}
}
async function start() {
 if(!preview.value)return;busy.value=true;error.value='';
 try{const result=await createReview(prUrl.value,preview.value);tracker.detach(true);preview.value=null;await follow(result.task_id);}
 catch(e){error.value=e instanceof Error?e.message:'启动失败';}finally{busy.value=false;}
}
async function stop(){if(!task.value)return;try{await cancelReview(task.value.id);await follow(task.value.id);}catch(e){error.value=e instanceof Error?e.message:'停止失败';}}
async function resume(){if(!task.value)return;try{const result=await resumeReview(task.value.id);await follow(result.id);}catch(e){error.value=e instanceof Error?e.message:'继续失败';}}
async function loadHistory(){const owner=++historyGeneration;try{const result=await listReviews(search.value,historyPage.value);if(owner!==historyGeneration)return;history.value=result.items;historyTotal.value=result.total;}catch(e){if(owner===historyGeneration)error.value=e instanceof Error?e.message:'历史读取失败';}}
async function remove(id:string){if(!confirm('删除该审查记录？正在发布或等待对账的记录不能删除。'))return;try{await deleteReview(id);if(task.value?.id===id)forgetTask();await loadHistory();}catch(e){error.value=e instanceof Error?e.message:'删除失败';}}
function goHistory(){page.value='history';void loadHistory();}
onMounted(async()=>{
 try{capabilities.value=await getReviewCapabilities();options.value.include_pr_discussions=capabilities.value.pr_discussions_default??false;}catch(e){error.value=e instanceof Error?e.message:'能力读取失败';}
 getAvailableModels().then(value=>{models.value=value.models.map(m=>m.id);}).catch(()=>{});
 let saved:string|null=null;try{saved=localStorage.getItem('rg-review-task');}catch{/* Session-only operation. */}
 if(saved&&/^[a-f0-9]{32}$/.test(saved))await follow(saved);else await restorePreview();
});
onBeforeUnmount(()=>{generation++;historyGeneration++;controller?.abort();closeStream?.();if(timer)clearTimeout(timer);tracker.detach();});
</script>
<template>
 <div class="native-app"><aside class="native-sidebar"><a class="native-brand" href="#" @click.prevent="newReview"><span>RG</span>RepoGuardian</a><p class="native-eyebrow">原生多视角代码审查</p><nav aria-label="主导航"><button :aria-current="page==='review'?'page':undefined" @click="page='review'">当前审查</button><button :aria-current="page==='history'?'page':undefined" @click="goHistory">审查历史</button><button :aria-current="page==='settings'?'page':undefined" @click="page='settings'">系统设置</button></nav><button class="button button--primary" @click="newReview">新建审查</button><p class="native-sidebar-note">固定快照 · 只读工具<br>独立核验 · 显式发布</p></aside>
 <main class="native-main"><header class="native-topbar"><span>{{ page==='history'?'审查历史':page==='settings'?'系统设置':task?'当前审查':'新建审查' }}</span><small>LangGraph + LangChain</small></header><div class="native-content"><p v-if="error" role="alert" class="native-error">{{ error }}</p>
 <NativeSettings v-if="page==='settings'" />
 <section v-else-if="page==='history'" class="native-panel"><h1>审查历史</h1><form @submit.prevent="historyPage=1;loadHistory()" class="native-actions"><label class="native-grow">搜索 PR、标题或任务<input v-model="search" placeholder="仓库、PR 或关键词" /></label><button class="button button--secondary">搜索</button></form><div class="native-table-wrap"><table><thead><tr><th>审查</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in history" :key="item.id"><td><button class="native-link" @click="page='review';preview=null;tracker.detach();follow(item.id)">{{ item.pr.title }}</button><small>{{ item.pr.owner }}/{{ item.pr.repo }} #{{ item.pr.number }}</small></td><td><StatusBadge :status="item.status" /></td><td>{{ new Date(item.created_at).toLocaleString() }}</td><td><button v-if="terminal(item.status)" class="button button--ghost" @click="remove(item.id)">删除</button></td></tr></tbody></table></div><div class="native-actions"><button class="button button--secondary" :disabled="historyPage===1" @click="historyPage--;loadHistory()">上一页</button><span>{{ historyPage }} 页 · 共 {{ historyTotal }} 条</span><button class="button button--secondary" :disabled="historyPage*20>=historyTotal" @click="historyPage++;loadHistory()">下一页</button></div></section>
 <NativeReview v-else-if="task" :task="task" @cancel="stop" @resume="resume" @rereview="rereview" />
 <template v-else><section class="native-intro"><p class="native-eyebrow">从真实源码出发</p><h1>让不同视角，<br>独立检查同一次变更。</h1><p>行为审查关注回归，契约审查追踪调用链，风险专家按需介入。只有通过可信证据和独立核验的问题，才进入交付草稿。</p></section>
 <section v-if="!preview" class="native-panel"><h2>准备审查方案</h2><form @submit.prevent="prepare" class="native-form"><label>GitHub Pull Request<input v-model="prUrl" type="url" required placeholder="https://github.com/owner/repo/pull/123" :disabled="busy||tracking" /></label><label>模型（可选）<input v-model="model" list="native-models" placeholder="使用服务端默认模型" :disabled="busy||tracking" /><datalist id="native-models"><option v-for="id in models" :key="id" :value="id" /></datalist></label><div class="native-form-grid"><label>审查深度<select v-model="options.depth" :disabled="busy||tracking"><option value="quick">快速初筛</option><option value="standard">标准审查</option><option value="deep">深度核验</option></select></label><label>主审范围<select v-model="selectedMode" :disabled="busy||tracking"><option value="all">全部合格变更文件</option><option value="selected">指定变更文件</option></select></label></div><label v-if="selectedMode==='selected'">文件路径（每行一个）<textarea v-model="selectedPaths" required :disabled="busy||tracking" /></label><fieldset :disabled="busy||tracking"><legend>关注重点</legend><label v-for="(label,key) in focusLabels" :key="key" class="native-check"><input v-model="options.focus" type="checkbox" :value="key" />{{ label }}</label></fieldset><label class="native-check"><input v-model="options.include_pr_discussions" type="checkbox" :disabled="busy||tracking||!capabilities?.pr_discussions_available" />纳入冻结的 PR 讨论上下文</label><details><summary>运行保护上限（留空使用服务端默认）</summary><div class="native-form-grid"><label v-for="(label,key) in limitLabels" :key="key">{{ label }}<input :value="options.limits[key as keyof ReviewPlanOptions['limits']]??''" type="number" min="1" :disabled="busy||tracking" @input="options.limits[key as keyof ReviewPlanOptions['limits']]=($event.target as HTMLInputElement).value?Number(($event.target as HTMLInputElement).value):null" /></label></div></details><button class="button button--primary" :disabled="!validUrl||!options.focus.length||busy||tracking">{{ busy||tracking?'正在准备固定快照…':'生成审查方案' }}</button></form><p class="native-muted">此步骤只获取 PR、固定源码与静态索引，不调用审查模型。</p></section>
 <section v-if="job&&!preview" class="native-panel" aria-live="polite"><h2>仓库准备进度</h2><p v-for="(stage,index) in job.stages" :key="index">{{ stage.message }} <span v-if="stage.percent!==null">{{ stage.percent }}%</span></p><p v-if="connectionError">{{ connectionError }}</p><div class="native-actions"><button v-if="tracking" class="button button--secondary" @click="tracker.cancel">取消预览</button><button v-if="connectionError" class="button button--secondary" @click="restorePreview">重新连接</button></div></section>
 <section v-if="preview" class="native-panel"><p class="native-eyebrow">确认后才开始模型审查</p><h2>审查方案已准备</h2><p>{{ preview.changed_files.filter(f=>f.included).length }} 个主审文件 · {{ preview.assignments.length }} 个独立审查任务 · {{ preview.goals.length }} 个源码检查范围</p><dl class="native-facts"><div><dt>HEAD</dt><dd><code>{{ preview.snapshot.head_sha }}</code></dd></div><div><dt>有效 BASE</dt><dd><code>{{ preview.snapshot.effective_base_sha }}</code></dd></div><div><dt>期限 / 并发</dt><dd>{{ preview.limits.timeout_seconds }} 秒 / {{ preview.limits.concurrency }}</dd></div></dl><ul><li v-for="assignment in preview.assignments" :key="assignment.id">{{ phaseLabel(assignment.reviewer) }} · {{ assignment.focus_paths.join('、') }}</li></ul><p v-for="warning in preview.warnings" :key="warning">{{ warning }}</p><p class="native-muted">核验与必要补查会产生额外模型调用。用量由供应商实际返回记录，未知不计为零。方案有效至 {{ new Date(preview.expires_at).toLocaleTimeString() }}。</p><div class="native-actions"><button class="button button--primary" :disabled="busy" @click="start">{{ busy?'启动中…':'确认方案并开始审查' }}</button><button class="button button--secondary" :disabled="busy" @click="preview=null;tracker.detach(true)">修改方案</button></div></section>
 </template></div></main></div>
</template>
