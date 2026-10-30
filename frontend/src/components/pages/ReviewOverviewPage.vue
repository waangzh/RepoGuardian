<script setup lang="ts">
import { computed } from 'vue'; import type { Finding, Report, Run } from '../../types/review'; import type { ReviewSection } from '../../types/workspace'; import { phaseLabel, terminal } from '../../utils/nativeReview'; import AppIcon from '../common/AppIcon.vue';
const props = defineProps<{ task: Run; report: Report | null; findings: Finding[]; loading: boolean }>(); defineEmits<{ navigate: [section: ReviewSection]; 'retry-overview': [] }>();
const tone = computed(() => props.task.status === 'failed' ? 'danger' : props.task.status === 'cancelled' ? 'info' : props.findings.length ? 'danger' : 'success');
const excluded = computed(() => Object.entries(props.report?.excluded || {}));
</script>
<template>
<div class="rg-overview">
<section class="ov-panel ov-verdict" :data-tone="tone">
<div class="ov-verdict__inner">
<div class="ov-verdict__kicker">
<AppIcon name="shield" />本轮结论 · {{ terminal(task.status) ? '固定快照审查' : '执行中' }}</div>
<h2>{{ task.status === 'failed' ? '审查中断，尚无完整结论' : task.status === 'cancelled' ? '审查已停止，保留已有结果' : findings.length ? '发现需要处理的已准入问题' : terminal(task.status) ? '未记录已准入问题' : '审查正在进行' }}</h2>
<p class="ov-verdict__description">候选必须经过源码证据和独立核验才能进入 Findings；零问题不代表没有缺陷。</p>
<div class="ov-verdict__bottom">
<div class="ov-verdict__meta">
<span>
<AppIcon name="activity" />{{ phaseLabel(task.phase) }}</span>
<span>
<AppIcon name="units" />{{ task.plan.units.length }} 个审查单元</span>
</div>
<button class="ov-link" @click="$emit('navigate','findings')">查看问题 <AppIcon name="arrow-right" />
</button>
</div>
</div>
</section>
<div class="ov-main">
<section class="ov-panel ov-results">
<div class="ov-metrics">
<div class="ov-metric">
<button class="ov-metric__head" @click="$emit('navigate','findings')">
<span class="ov-metric__title">
<AppIcon name="shield" />已准入问题</span>
<strong :class="{ 'ov-count-danger': !!findings.length }">{{ findings.length }}</strong>
<small>可由来源证据追溯</small>
</button>
</div>
<div class="ov-metric">
<button class="ov-metric__head" @click="$emit('navigate','findings')">
<span class="ov-metric__title">
<AppIcon name="help" />候选</span>
<strong>{{ report?.candidates.length ?? '—' }}</strong>
<small>候选不等于已确认问题</small>
</button>
</div>
<div class="ov-metric">
<button class="ov-metric__head" @click="$emit('navigate','activity')">
<span class="ov-metric__title">
<AppIcon name="units" />工作项</span>
<strong>{{ report?.units.length ?? '—' }}</strong>
<small>状态和失败原因见活动</small>
</button>
</div>
</div>
</section>
<section class="ov-panel ov-understanding">
<header class="ov-section-head">
<h2>PR 目标与代码变化</h2>
<button class="ov-link" @click="$emit('navigate','changes')">查看 Diff <AppIcon name="external" :size="14" />
</button>
</header>
<dl class="ov-change__rows">
<div>
<dt>作者声明</dt>
<dd class="rg-preserve-lines">{{ task.pr.body || task.pr.title }}<p class="ov-change__limitation">作者描述属于不可信背景，不表示目标已由源码验证。</p>
</dd>
</div>
<div>
<dt>变更范围</dt>
<dd>{{ task.changed_files.length }} 个文件，新增 {{ task.changed_files.reduce((sum,file)=>sum+file.additions,0) }} 行、删除 {{ task.changed_files.reduce((sum,file)=>sum+file.deletions,0) }} 行。</dd>
</div>
<div v-if="report?.overview.output">
<dt>变更总览</dt>
<dd v-for="item in report.overview.output.change_overview" :key="item.text" class="rg-preserve-lines">{{ item.text }}</dd>
</div>
</dl>
<p v-if="report?.overview.status === 'failed'" class="muted small">变更总览生成失败；固定 Diff 仍可读取。<button class="link" @click="$emit('retry-overview')">重试总览</button>
</p>
</section>
</div>
<section class="ov-panel ov-boundary">
<header class="ov-section-head">
<h2>检查边界</h2>
</header>
<p class="ov-note">
<AppIcon name="info" />只读静态审查。未执行目标仓库的测试、构建或脚本；零问题不等于代码已证明正确。</p>
<ul v-if="report?.reasons.length" class="ov-limitations">
<li v-for="reason in report.reasons" :key="reason">{{ reason }}</li>
</ul>
<p v-if="Object.values(report?.overview.omitted || {}).some(value => value > 0)" class="ov-note"><AppIcon name="info" />变更总览输入存在省略：<template v-for="(count, key) in report?.overview.omitted" :key="key"><span v-if="count">{{ key }} {{ count }} 项；</span></template>引用仍仅能证明已展示材料，不能据此保证覆盖完整。</p>
<ul v-if="excluded.length" class="ov-limitations">
<li v-for="item in excluded" :key="item[0]">
<code>{{ item[0] }}</code> · {{ item[1] }}</li>
</ul>
</section>
</div>
</template>
