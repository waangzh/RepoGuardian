// Vite 合成验收页：publication-preview.html?scene=findings&section=report
// agent-browser eval --stdin < tests/review/comment-delivery-guards.browser.js
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
  const button = (root, label) => [...root.querySelectorAll("button")]
    .find(item => item.textContent.trim() === label);
  const output = () => document.querySelector('output[aria-label="已复制内容"]').textContent;
  const notice = () => [...document.querySelectorAll('[role="status"]')]
    .some(item => item.textContent.includes("评论内容已更新，请重新确认"));
  const selectSnapshot = value => {
    const select = document.querySelector('select[aria-label="快照场景"]');
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const navigation = label => {
    const target = [...document.querySelectorAll('nav[aria-label="审查结果导航"] button')]
      .find(item => item.textContent.trim().startsWith(label));
    assert(target, `缺少导航：${label}`);
    target.click();
  };
  const copyReport = () => document.querySelector(".rd-aside .btn.primary").click();
  const dialog = () => document.querySelector("dialog[open]");
  let version = 1;
  let calls = 0;
  let lastResponse;
  const marker = () => `交付核验注记：修复状态 ${version} 待重新核验。`;
  const originalFetch = window.fetch;
  window.fetch = async (input, init) => {
    const response = await originalFetch(input, init);
    if (typeof input !== "string" || !input.endsWith("/review-comment-check")) return response;
    const body = await response.json();
    const base = body.preview_markdown;
    assert(typeof base === "string" && body.review_comment_markdown.startsWith(base), "夹具缺少稳定正文");
    const suffix = body.review_comment_markdown.slice(base.length);
    body.preview_markdown = base + "\n\n" + marker();
    body.review_comment_markdown = body.preview_markdown + suffix;
    calls += 1;
    lastResponse = body;
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  };
  const originalCreate = URL.createObjectURL;
  const originalClick = HTMLAnchorElement.prototype.click;
  let exported;
  try {
    await wait(() => document.querySelector(".rd-comment"), "报告未加载");
    selectSnapshot("current");
    copyReport();
    await wait(() => notice() && document.querySelector(".rd-comment").textContent.includes(marker()), "报告未刷新预览");
    assert(!output() && calls === 1 && !dialog(), "正文变化时不得交付或进入旧快照确认");
    copyReport();
    await wait(() => output(), "再次确认后未复制");
    assert(output() === lastResponse.review_comment_markdown, "复制内容与检查正文不一致");
    results.push("报告：正文变化先更新预览，再次操作才复制检查后的正文");

    selectSnapshot("current");
    version = 2;
    URL.createObjectURL = blob => { exported = blob; return originalCreate(blob); };
    HTMLAnchorElement.prototype.click = function () { if (!this.download) originalClick.call(this); };
    button(document.querySelector(".rd-aside"), "导出 Markdown").click();
    await wait(() => notice() && document.querySelector(".rd-comment").textContent.includes(marker()), "导出未刷新预览");
    assert(!exported && !output(), "正文变化时不得导出或误执行复制");
    button(document.querySelector(".rd-aside"), "导出 Markdown").click();
    await wait(() => exported, "再次确认后未导出");
    assert(await exported.text() === lastResponse.review_comment_markdown, "导出内容与检查正文不一致");
    results.push("导出：正文变化先停止，第二次操作生成同一版本 Markdown");

    selectSnapshot("unknown");
    version = 3;
    copyReport();
    await wait(() => notice() && document.querySelector(".rd-comment").textContent.includes(marker()), "未知状态未先刷新预览");
    assert(!dialog() && !output(), "未知状态的正文变化不能沿用确认");
    copyReport();
    await wait(() => dialog()?.querySelector(".comment-confirmation"), "未知状态缺少独立确认");
    assert(dialog().contains(document.activeElement), "确认焦点不在弹窗内");
    assert(!output(), "未知状态确认前不得复制");
    button(dialog(), "仍复制旧版审查记录").click();
    await wait(() => output() && !dialog(), "未知状态确认后未交付");
    assert(output() === lastResponse.review_comment_markdown && output().includes("快照检查：unknown"), "未知快照说明丢失");
    results.push("未知 Head：先确认更新正文，再显式确认快照限制");

    navigation("结果概览");
    await wait(() => document.querySelector(".ov-next__delivery > button.button"), "概览复制入口缺失");
    selectSnapshot("current");
    version = 4;
    document.querySelector(".ov-next__delivery > button.button").click();
    await wait(dialog, "概览预览未打开");
    dialog().querySelector("footer .button--primary").click();
    await wait(() => notice() && dialog()?.textContent.includes(marker()), "概览弹窗未刷新正文");
    assert(!output() && dialog(), "概览正文更新时应保留弹窗且不得复制");
    dialog().querySelector("footer .button--primary").click();
    await wait(() => output() && !dialog(), "概览再次确认后未复制");
    assert(output() === lastResponse.review_comment_markdown, "概览复制版本不一致");
    results.push("概览：原弹窗展示更新正文，重新确认后复制");

    navigation("问题与证据");
    await wait(() => document.querySelector(".comment-draft"), "单条评论入口未加载");
    selectSnapshot("current");
    version = 5;
    const singleCopy = () => document.querySelector(".comment-draft header button").click();
    singleCopy();
    await wait(() => notice() && document.querySelector(".comment-draft__preview").textContent.includes(marker()), "单条评论未刷新正文");
    assert(!output(), "单条正文更新时不得复制");
    assert(document.querySelector(".comment-draft textarea").value === lastResponse.preview_markdown, "单条 Markdown 原文未同步");
    singleCopy();
    await wait(output, "单条评论再次确认后未复制");
    assert(output() === lastResponse.review_comment_markdown, "单条评论复制版本不一致");
    results.push("单条评论：更新后的预览和 Markdown 原文一致，第二次操作才复制");
    return { passed: results, checks: calls };
  } finally {
    window.fetch = originalFetch;
    URL.createObjectURL = originalCreate;
    HTMLAnchorElement.prototype.click = originalClick;
  }
})();
