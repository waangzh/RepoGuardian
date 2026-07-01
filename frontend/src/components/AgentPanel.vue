<script setup lang="ts">
import type { AgentEvent, PatchResult, TestRunResult } from "../types/review";

defineProps<{
  events: AgentEvent[];
  staticResults: TestRunResult[];
  patches: PatchResult[];
  testResults: TestRunResult[];
}>();
</script>

<template>
  <section class="panel agent-panel">
    <div class="panel-head">
      <h2>Agent 执行</h2>
      <span>{{ events.length }}</span>
    </div>

    <div class="agent-section">
      <h3>决策日志</h3>
      <div v-if="events.length === 0" class="empty">暂无 Agent 动作</div>
      <div v-for="event in events" :key="`${event.created_at}:${event.action}`" class="agent-row">
        <strong>{{ event.action }}</strong>
        <span>{{ event.status }}</span>
        <p>{{ event.reason }}</p>
        <small v-if="event.message">{{ event.message }}</small>
      </div>
    </div>

    <div class="agent-section">
      <h3>静态分析</h3>
      <div v-if="staticResults.length === 0" class="empty">未运行</div>
      <div v-for="result in staticResults" :key="`${result.tool}:${result.command}`" class="tool-row">
        <strong>{{ result.command }}</strong>
        <span :data-pass="result.passed">{{ result.passed ? "通过" : "失败" }}</span>
        <small>exit {{ result.exit_code }} / {{ result.duration.toFixed(2) }}s</small>
      </div>
    </div>

    <div class="agent-section">
      <h3>Patch</h3>
      <div v-if="patches.length === 0" class="empty">未生成 patch</div>
      <details v-for="patch in patches" :key="patch.id" class="patch-block">
        <summary>
          <strong>{{ patch.id.slice(0, 8) }}</strong>
          <span>{{ patch.status }}</span>
          <small v-if="patch.issue_id">{{ patch.issue_id }}</small>
        </summary>
        <p v-if="patch.error" class="error">{{ patch.error }}</p>
        <pre>{{ patch.diff_content }}</pre>
      </details>
    </div>

    <div class="agent-section">
      <h3>测试结果</h3>
      <div v-if="testResults.length === 0" class="empty">未运行</div>
      <div v-for="result in testResults" :key="`${result.tool}:${result.command}`" class="tool-row">
        <strong>{{ result.command }}</strong>
        <span :data-pass="result.passed">{{ result.passed ? "通过" : "失败" }}</span>
        <small>exit {{ result.exit_code }} / {{ result.duration.toFixed(2) }}s</small>
      </div>
    </div>
  </section>
</template>
