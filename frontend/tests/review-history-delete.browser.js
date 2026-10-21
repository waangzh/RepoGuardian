// review-history-delete-preview.html?failure 可同时覆盖服务端拒绝后的重试。
(async () => {
  const results = [];
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const wait = async (predicate, message) => {
    for (let i = 0; i < 240; i++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw new Error(message);
  };
  const button = (root, label) => [...root.querySelectorAll("button")].find(item => item.textContent.trim() === label);
  const rows = () => [...document.querySelectorAll(".history-row")];
  const dialog = () => document.querySelector(".history-delete-dialog[open]");
  const requests = () => JSON.parse(document.querySelector("#request-log").textContent || "[]");
  const deletes = () => requests().filter(item => item.method === "DELETE");
  const navigate = label => {
    const target = button(document, label);
    assert(target, `缺少入口：${label}`);
    target.click();
  };
  const openDelete = async row => {
    button(row, "删除").click();
    await wait(dialog, "确认框未打开");
    assert(document.activeElement.textContent.trim() === "取消", "初始焦点不是取消");
    assert(dialog().textContent.includes("已发布到 GitHub 的评论不会删除"), "缺少删除范围说明");
  };
  await wait(() => button(document, "审查历史"), "应用未加载");
  navigate("审查历史");
  await wait(() => rows().length === 12, "历史第一页未加载");
  assert(rows().some(row => row.textContent.includes("尚未形成结论") && button(row, "删除").disabled), "运行中任务仍可删除");
  await openDelete(rows()[0]);
  button(dialog(), "取消").click();
  assert(!dialog() && deletes().length === 0 && rows().length === 12, "取消后发生删除");
  results.push("确认身份、取消焦点与运行中禁用");
  navigate("下一页");
  await wait(() => rows().length === 1, "历史末页未加载");
  await openDelete(rows()[0]);
  const confirm = button(dialog(), "确认删除");
  confirm.click(); confirm.click();
  await wait(() => !dialog() || dialog().querySelector('[role="alert"]'), "删除未返回结果");
  if (new URLSearchParams(location.search).has("failure")) {
    assert(dialog()?.textContent.includes("发布结果尚未确认") && rows().length === 1, "失败没有保留记录和原因");
    assert(deletes().length === 1, "重复点击提交了多次删除");
    button(dialog(), "确认删除").click();
    results.push("拒绝保留记录、原因及幂等重试入口");
  }
  await wait(() => !dialog() && rows().length === 12, "删除末页最后一条后未回到第一页");
  assert(!document.querySelector(".pagination"), "删除后分页总数未同步");
  results.push("末页删除、分页回退和总数更新");
  const currentId = rows()[0].querySelector("code").textContent.split(" · ")[0];
  button(rows()[0], "查看详情").click();
  await wait(() => button(document, "重新审查此 PR"), "未打开当前审查");
  navigate("审查历史");
  await wait(() => rows().length === 12, "回到历史失败");
  const search = document.querySelector(".rg-history-search input");
  search.value = currentId; search.dispatchEvent(new Event("input", { bubbles: true }));
  await wait(() => rows().length === 1, "筛选未生效");
  await openDelete(rows()[0]);
  button(dialog(), "确认删除").click();
  await wait(() => !dialog() && !rows().length, "当前记录删除后仍可见");
  assert(search.value === currentId, "删除丢失搜索条件");
  navigate("新建审查");
  await wait(() => document.querySelector(".hero-import"), "删除后仍保留旧审查工作区");
  assert(!document.querySelector(".rd-workspace"), "旧报告仍然可见");
  assert(document.documentElement.scrollWidth <= innerWidth, "页面存在横向溢出");
  results.push("筛选保留、当前任务及报告清理、响应式布局");
  return { passed: results, deleteRequests: deletes().length };
})()
