<script setup lang="ts">
import { computed, ref } from "vue";
import { Dialog, DialogPanel, DialogTitle } from "@headlessui/vue";
import type { ReviewSummary } from "../../types/publication";
import type { ReviewSituation } from "../../utils/reviewSituation";
import type { ReviewSection } from "../../types/workspace";
import { overviewFacts } from "../../utils/overviewPresentation";
import { coverageLabel, reportValidationLabel } from "../../utils/reportPresentation";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{
  plan: ReviewSummary | null;
  situation: ReviewSituation | null;
  execution: string;
}>();
const emit = defineEmits<{ navigate: [section: ReviewSection] }>();
const explanationOpen = ref(false);
const facts = computed(() => overviewFacts(props.plan, null));
const technicalReasons = computed(() => [...new Set([
  ...(props.plan?.outcome.reason_codes || []),
  ...(props.plan?.digest.merge_decision?.unresolved_reasons || []),
])]);
const kicker = computed(() => ({
  clean: "已检查范围内无阻断问题", nonblocking: "有改进建议", blockers: "需要优先处理",
  evidence_gaps: "需要补充核实", scope_incomplete: "范围未完整覆盖",
  stale: "PR 已更新", failed: "执行错误中断", cancelled: "用户已停止",
  validation_failed: "已有验证失败",
})[props.situation?.key || "evidence_gaps"]);
function navigate(section: ReviewSection) {
  explanationOpen.value = false;
  emit("navigate", section);
}
</script>

<template>
  <section class="ov-panel ov-verdict" :data-tone="situation?.tone || 'info'" aria-labelledby="overview-conclusion">
    <div class="ov-verdict__inner">
      <div class="ov-verdict__kicker"><AppIcon :name="situation?.icon || 'info'" :size="18" />本轮结论 · {{ plan ? kicker : "摘要暂不可用" }}</div>
      <h2 id="overview-conclusion">{{ situation?.title || "暂时无法形成完整结论" }}</h2>
      <p class="ov-verdict__description">{{ situation?.description || "尚未取得后端审查摘要，请重试加载或查看技术记录。缺失数据不能视为零问题。" }}</p>
      <p v-if="facts.limitations[0]" class="ov-verdict__limitation"><AppIcon name="info" :size="15" />{{ facts.limitations[0] }}</p>
      <div class="ov-verdict__bottom">
        <div class="ov-verdict__meta">
          <span><AppIcon name="activity" :size="15" />{{ execution }}</span>
          <span><AppIcon name="units" :size="15" />{{ facts.examined ?? "未知" }} / {{ facts.total ?? "未知" }} 项目标已检查</span>
          <span><AppIcon name="flask" :size="15" />{{ facts.validation === "not_executed" ? "仅静态审查" : reportValidationLabel(facts.validation) }}</span>
        </div>
        <button type="button" class="ov-link" @click="explanationOpen = true">结论如何得出 <AppIcon name="arrow-right" :size="15" /></button>
      </div>
    </div>
  </section>

  <Dialog :open="explanationOpen" class="ov-dialog" @close="explanationOpen = false">
    <div class="ov-dialog__scrim" aria-hidden="true" />
    <div class="ov-dialog__position">
      <DialogPanel class="ov-dialog__panel">
        <header class="ov-dialog__header">
          <div><span>结论与证据</span><DialogTitle>结论的形成依据</DialogTitle></div>
          <button type="button" class="ov-icon-button" aria-label="关闭结论说明" @click="explanationOpen = false"><AppIcon name="x" :size="19" /></button>
        </header>
        <div class="ov-dialog__body">
          <p>结论结合实际检查范围、问题证据、未决事项、运行可靠性和动态验证形成，不能仅凭问题数量或目标完成率判断。</p>
          <template v-if="plan">
            <dl class="ov-explanation">
              <div><dt>审查完整性</dt><dd>{{ coverageLabel(plan.outcome.review_completeness) }}</dd></div>
              <div><dt>运行可靠性</dt><dd>{{ ({ reliable: "可靠", unreliable: "存在限制", unknown: "未知" })[plan.outcome.runtime_reliability] || "未知" }}</dd></div>
              <div><dt>动态验证</dt><dd>{{ reportValidationLabel(facts.validation) }}</dd></div>
            </dl>
            <h3>后端记录的限制</h3>
            <ul v-if="facts.limitations.length"><li v-for="item in facts.limitations" :key="item">{{ item }}</li></ul>
            <p v-else>轻量摘要未提供具体限制条目；详细范围与证据可在覆盖页或报告中核对。</p>
            <details v-if="technicalReasons.length"><summary>技术原因代码</summary><ul><li v-for="reason in technicalReasons" :key="reason"><code>{{ reason }}</code></li></ul></details>
            <details v-if="plan.withheld_findings?.length"><summary>保留但未交付的问题记录</summary><ul><li v-for="item in plan.withheld_findings" :key="`${item.candidate_id}:${item.reason_code}`">{{ item.candidate_id }} · {{ item.reason_code }}</li></ul></details>
          </template>
          <p v-else>摘要尚未取得，完整性、问题数量与验证状态保持未知。</p>
          <h3>阅读时需要区分</h3>
          <ul><li>目标已检查：仅描述约定静态目标的检查记录。</li><li>未核验线索：调查提示，不等于已确认缺陷。</li><li>验证未执行或未知：不能推断项目测试、构建或 CI 已通过。</li></ul>
        </div>
        <footer class="ov-dialog__footer">
          <button type="button" class="btn secondary" @click="explanationOpen = false">返回概览</button>
          <button type="button" class="btn primary" @click="navigate('scope')">查看覆盖与限制 <AppIcon name="arrow-right" :size="15" /></button>
        </footer>
      </DialogPanel>
    </div>
  </Dialog>
</template>
