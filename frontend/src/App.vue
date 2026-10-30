<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRaw, watch } from 'vue';
import { cancelReview, createReview, getAvailableModels, getReview, getReviewCapabilities, retryOverview, retryReview, subscribeToReview } from './api/client';
import type { AppPage } from './types/operations'; import type { Run } from './types/review'; import type { ReviewCapabilities, ReviewPlanOptions } from './types/reviewPlan'; import { reviewPrimarySections, type ReviewSection } from './types/workspace'; import { defaultPlan, phaseLabel, terminal } from './utils/nativeReview';
import AppSidebar from './components/layout/AppSidebar.vue'; import AppHeader from './components/layout/AppHeader.vue'; import NewReviewPage from './components/pages/NewReviewPage.vue'; import ReviewHistoryPage from './components/pages/ReviewHistoryPage.vue'; import SettingsPage from './components/pages/SettingsPage.vue'; import ReviewWorkspace from './components/review/ReviewWorkspace.vue';
const page = ref<AppPage>('dashboard'), section = ref<ReviewSection>('overview'), mobileOpen = ref(false);
const prUrl = ref(''), model = ref(''), models = ref<string[]>([]);
const capabilities = ref<ReviewCapabilities | null>(null);
const options = ref<ReviewPlanOptions>(defaultPlan());
const task = ref<Run | null>(null), trackedId = ref<string | null>(null);
const error = ref(''), busy = ref(false), opening = ref(false), restoring = ref(true);
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let closeStream: (() => void) | null = null;
let generation = 0, catalogGeneration = 0;
let disposed = false;
const taskActive = computed(() => !!task.value && !terminal(task.value.status));
const pageTitle = computed(() => page.value === 'history' ? '审查历史' : page.value === 'settings' ? '系统设置' : task.value ? reviewPrimarySections.find(item => item.id === section.value)?.label || '当前审查' : '新建审查');
const statusText = computed(() => task.value ? phaseLabel(task.value.phase) : '只读静态审查');
function forgetTask() { generation++; controller?.abort(); closeStream?.(); closeStream = null;
if (timer) clearTimeout(timer); timer = null; task.value = null; trackedId.value = null; opening.value = false; busy.value = false; restoring.value = false;
try { localStorage.removeItem('rg-review-task'); } catch {} }
function newReview() { forgetTask(); error.value = ''; page.value = 'dashboard'; section.value = 'overview'; mobileOpen.value = false; prUrl.value = ''; }
function navigate(value: ReviewSection) { page.value = 'dashboard'; section.value = value; mobileOpen.value = false; }
function rereview() { const url = task.value?.pr_url, saved = task.value?.configuration.options; newReview();
if (url) prUrl.value = url;
if (saved) options.value = structuredClone(toRaw(saved)); }
async function loadModels() { const owner = ++catalogGeneration;
try { const value = await getAvailableModels();
if (owner === catalogGeneration) models.value = value.models.map(item => item.id); } catch {} }
async function follow(id: string) { if (disposed) return; page.value = 'dashboard'; mobileOpen.value = false;
if (trackedId.value !== id) { task.value = null; section.value = 'overview'; } trackedId.value = id; opening.value = true; busy.value = false; closeStream?.(); controller?.abort();
if (timer) clearTimeout(timer);
const owner = ++generation, abort = new AbortController(); controller = abort;
let refreshing = false;
try { localStorage.setItem('rg-review-task', id); } catch {} async function refresh() { if (owner !== generation || refreshing) return; refreshing = true;
if (timer) clearTimeout(timer);
try { const result = await getReview(id, abort.signal);
if (owner !== generation) return;
if (result.id !== id) throw new Error('返回的记录不属于当前审查任务'); task.value = result; error.value = '';
if (!terminal(result.status)) timer = setTimeout(refresh, 1200); else { closeStream?.(); closeStream = null; } } catch (cause) { if (owner !== generation || abort.signal.aborted) return; error.value = cause instanceof Error ? cause.message : '连接中断'; timer = setTimeout(refresh, 3000); } finally { refreshing = false;
if (owner === generation) opening.value = false; } } await refresh();
if (owner === generation && task.value && !terminal(task.value.status)) closeStream = subscribeToReview(id, () => { void refresh(); }); }
async function start() { if (busy.value) return;
const input = structuredClone(toRaw(options.value)); input.paths = input.paths?.map(path => path.trim()).filter(Boolean) || null;
if (!input.focus.length) { error.value = '请至少选择一个审查重点。';
return; } const owner = generation; busy.value = true; error.value = '';
try { const result = await createReview({ pr_url: prUrl.value, depth: input.depth, paths: input.paths, focus: input.focus, limits: input.limits, ...(model.value ? { model: model.value } : {}), include_pr_discussions: input.include_pr_discussions });
if (owner !== generation) return; await follow(result.id); } catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '启动失败'; } finally { if (!disposed && owner === generation) busy.value = false; } }
async function stop() { if (!task.value || busy.value) return; busy.value = true;
try { await cancelReview(task.value.id); await follow(task.value.id); } catch (cause) { error.value = cause instanceof Error ? cause.message : '停止失败'; } finally { if (!disposed) busy.value = false; } }
async function retry() { if (!task.value || busy.value) return; busy.value = true;
try { const result = await retryReview(task.value.id); await follow(result.id); } catch (cause) { error.value = cause instanceof Error ? cause.message : '重试失败'; } finally { if (!disposed) busy.value = false; } }
async function retrySummary() { if (!task.value || busy.value) return; busy.value = true;
try { const result = await retryOverview(task.value.id); await follow(result.id); } catch (cause) { error.value = cause instanceof Error ? cause.message : '重试总览失败'; } finally { if (!disposed) busy.value = false; } }
watch(page, () => { mobileOpen.value = false; });
watch(pageTitle, title => { if (typeof document !== 'undefined') document.title = title + ' · RepoGuardian'; }); onMounted(async () => { const owner = generation; void loadModels();
try { const value = await getReviewCapabilities();
if (disposed || owner !== generation) return; capabilities.value = value; options.value.include_pr_discussions = value.pr_discussions_default ?? false; } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '能力读取失败'; } let saved: string | null = null;
try { saved = localStorage.getItem('rg-review-task'); } catch {} try { if (saved) await follow(saved); } finally { if (!disposed) restoring.value = false; } }); onBeforeUnmount(() => { disposed = true; generation++; catalogGeneration++; controller?.abort(); closeStream?.();
if (timer) clearTimeout(timer); });
</script>
<template>
<div class="app rg-app" @keydown.esc="mobileOpen = false">
<button v-if="mobileOpen" class="rg-nav-backdrop" aria-label="关闭导航遮罩" @click="mobileOpen = false" />
<AppSidebar v-model="page" :section="section" :has-review="!!task" :has-preview="restoring || !!trackedId" :task-active="taskActive" :mobile-open="mobileOpen" :busy="busy || opening" @section="navigate" @create="newReview" @close="mobileOpen = false" />
<div class="main">
<AppHeader :title="pageTitle" :status-text="statusText" :menu-open="mobileOpen" @menu="mobileOpen = !mobileOpen" />
<main id="workspace-content" class="page">
<p v-if="error" class="notice warn space-bottom" role="alert">{{ error }}</p>
<ReviewHistoryPage v-if="page === 'history'" :busy="busy || opening" @open="follow" @create="newReview" @deleted="task?.id === $event && forgetTask()" />
<SettingsPage v-else-if="page === 'settings'" @saved="loadModels" />
<template v-else>
<ReviewWorkspace v-if="task" :key="task.id" :task="task" :section="section" :busy="busy || opening" @navigate="navigate" @cancel="stop" @retry="retry" @overview-retry="retrySummary" @rereview="rereview" />
<section v-else-if="restoring || opening || trackedId" class="card card-pad workspace-loading" role="status">
<h1>正在打开审查记录…</h1>
<button v-if="trackedId && !opening" class="btn secondary space-top" @click="follow(trackedId)">重试打开任务</button>
</section>
<NewReviewPage v-else v-model:pr-url="prUrl" v-model:model="model" v-model:options="options" :models="models" :capabilities="capabilities" :busy="busy" @create="start" @open="follow" @history="page = 'history'" />
</template></main></div></div></template>
