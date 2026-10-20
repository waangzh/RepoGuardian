// 启动 Vite 后打开 publication-preview.html?scene=clean&section=overview，
// 使用 agent-browser eval --stdin < tests/review-comment-copy.browser.js 执行。
(async () => {
  const results = [];
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const wait = async (predicate, message) => {
    for (let i = 0; i < 200; i += 1) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw new Error(message);
  };
  const button = (root, text) => [...root.querySelectorAll("button")].find(item => item.textContent.trim() === text);
  const click = (root, text) => {
    const target = button(root, text);
    assert(target && !target.disabled, `按钮不可用：${text}`);
    target.click();
  };
  const select = (label, value) => {
    const target = document.querySelector(`select[aria-label="${label}"]`);
    target.value = value;
    target.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const output = () => document.querySelector('output[aria-label="已复制内容"]').textContent;
  const dialog = () => document.querySelector("dialog[open]");
  const stale = () => [...document.querySelectorAll(".ov-main h2")].some(item => item.textContent.includes("PR 已更新"));
  const overviewCopy = async () => {
    const trigger = document.querySelector(".ov-next__delivery > button.button");
    assert(trigger, "历史情景仍保留复制组件");
    trigger.click();
    await wait(dialog, "预览框未打开");
    dialog().querySelector("footer .button--primary").click();
  };
  const confirmation = async status => {
    await wait(() => dialog()?.querySelector(".comment-confirmation"), "确认控件被卸载");
    assert(dialog().textContent.includes(status === "stale" ? "PR 已更新" : "暂时无法确认 PR 是否有新提交"), "确认状态错误");
    assert(dialog().contains(document.activeElement), "焦点应留在对话框内");
    assert(document.activeElement.textContent.trim() === "取消", "初始焦点应为取消");
    assert(!output(), "确认前不得复制");
  };

  await wait(() => document.querySelector(".ov-next__delivery > button.button"), "概览未加载");
  select("快照场景", "current");
  await overviewCopy();
  await wait(() => output().includes("快照检查：current") && !dialog(), "当前快照复制失败");
  results.push("current 只复制检查后的正文");

  select("快照场景", "stale");
  await overviewCopy();
  await confirmation("stale");
  assert(stale(), "应立即切换旧快照情景");
  click(dialog(), "取消");
  await wait(() => !dialog(), "取消未关闭确认框");
  assert(!output() && stale(), "取消不得复制或清除过期状态");
  await overviewCopy();
  await confirmation("stale");
  click(dialog(), "仍复制带旧快照说明的历史记录");
  await wait(() => output().includes("快照检查：stale") && !dialog(), "历史复制失败");
  assert(stale(), "复制成功不得恢复当前版本");
  results.push("stale 情景不卸载；取消不复制；确认后保留说明与过期状态");

  click(document.querySelector('nav[aria-label="审查结果导航"]'), "审查报告");
  await wait(() => document.querySelector(".rd-aside"), "报告未加载");
  assert(document.querySelector(".rd-aside").textContent.includes("PR 已更新"), "跨页面应保留旧快照事实");
  select("快照场景", "unknown");
  click(document.querySelector(".rd-aside"), "复制 PR 评论");
  await confirmation("unknown");
  click(dialog(), "仍复制带未知状态说明的历史记录");
  await wait(() => output().includes("快照检查：unknown") && !dialog(), "未知状态确认失败");
  assert(document.querySelector(".rd-aside").textContent.includes("PR 已更新"), "unknown 不得清除已知 stale");
  results.push("报告共用确认区；unknown 保留服务端说明和已知过期事实");

  select("快照场景", "stale");
  const originalCreate = URL.createObjectURL;
  const originalClick = HTMLAnchorElement.prototype.click;
  let exported;
  URL.createObjectURL = blob => { exported = blob; return originalCreate(blob); };
  HTMLAnchorElement.prototype.click = function () { if (!this.download) originalClick.call(this); };
  try {
    click(document.querySelector(".rd-aside"), "导出 Markdown");
    await confirmation("stale");
    click(dialog(), "仍导出带旧快照说明的历史记录");
    await wait(() => exported && !dialog(), "确认后未导出");
    assert((await exported.text()).includes("快照检查：stale"), "导出应保留旧快照说明");
    assert(!output(), "导出不能误执行复制");
  } finally {
    URL.createObjectURL = originalCreate;
    HTMLAnchorElement.prototype.click = originalClick;
  }
  click(document.querySelector(".rd-aside"), "复制 PR 评论");
  await confirmation("stale");
  click(dialog(), "重新审查此 PR");
  await wait(() => document.querySelector('output[aria-label="重新审查入口"]') && !dialog(), "报告重审未接入真实事件链");
  results.push("历史导出保留原动作与说明；报告重审事件接通");

  click(document.querySelector('nav[aria-label="审查结果导航"]'), "结果概览");
  await wait(stale, "返回概览丢失过期状态");
  select("快照场景", "current");
  await overviewCopy();
  await wait(() => output().includes("快照检查：current") && !dialog(), "重新检查 current 失败");
  assert(!stale(), "只有明确 current 才能清除过期状态");
  results.push("明确 current 检查可恢复当前状态");

  for (const scene of ["failed", "cancelled"]) {
    select("快照场景", "stale");
    select("验收场景", scene);
    await wait(() => document.querySelector(".ov-next__delivery > button.button")?.textContent.includes("复制部分审查记录"), "任务切换未重置状态");
    await overviewCopy();
    await confirmation("stale");
    assert(document.querySelector(".ov-next__delivery > button.button").textContent.includes("历史部分审查记录"), "历史记录不能丢失部分审查语义");
    click(dialog(), "取消");
  }
  results.push("切换任务重置状态；失败和取消保留部分记录语义");
  select("验收场景", "clean");
  await wait(() => document.querySelector(".ov-next__delivery > button.button")?.textContent.includes("复制 PR 评论"), "场景未恢复");
  select("快照场景", "unknown");
  await overviewCopy();
  await confirmation("unknown");
  // 留下确认框，供外层以真实 Escape 键验证原生取消和焦点恢复。
  return { passed: results, awaitingEscape: true };
})();
