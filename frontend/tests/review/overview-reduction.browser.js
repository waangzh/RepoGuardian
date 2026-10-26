// 在 publication-preview.html?scene=clean&section=overview 中执行。
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
  const button = (root, text) => [...root.querySelectorAll('button')].find(item => item.textContent.trim() === text);
  const click = (root, text) => {
    const target = button(root, text);
    assert(target && !target.disabled, `按钮不可用：${text}`);
    target.click();
  };
  const selectScene = value => {
    const select = document.querySelector('select[aria-label="验收场景"]');
    select.value = value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const overview = () => document.querySelector('.rg-overview');
  await wait(() => button(overview() || document, '查看完整报告'), '概览未就绪');
  const disclosures = [...overview().querySelectorAll('.ov-disclosure')];
  assert(disclosures.length === 3 && disclosures.every(item => !item.open), '补充信息应默认收起');
  assert(!overview().innerText.includes('结论依据与可信边界'), '可信边界不应默认占据首屏');
  const primary = [...document.querySelectorAll('.rg-overview .primary, .head-actions .primary')].filter(item => item.getClientRects().length);
  assert(primary.length === 1 && primary[0].textContent.includes('查看完整报告'), '概览应只有一个主操作');
  const steps = overview().querySelector('.ov-step-details');
  assert(!steps.open && !steps.innerText.includes('核对版本与 CI'), '步骤应默认收起');
  steps.querySelector('summary').click();
  await wait(() => steps.open && steps.innerText.includes('核对版本与 CI'), '步骤无法展开');
  steps.querySelector('summary').click();
  assert(!steps.open, '步骤无法收起');
  assert(!overview().querySelector('.ov-note'), '正常状态不应显示额外解释条');
  assert(!overview().querySelector('.ov-snapshot').textContent.includes('动态验证'), '快照不应重复验证状态');
  assert(!button(overview(), '阅读详细审查报告'), '已有报告动作时不应重复入口');
  assert(document.documentElement.scrollWidth <= innerWidth + 1, '页面存在横向溢出');
  results.push(`折叠、单一报告入口与无溢出：${innerWidth}px`);
  disclosures[0].querySelector('summary').click();
  assert(disclosures[0].open && overview().innerText.includes('结论依据与可信边界'), '检查范围无法展开');
  disclosures[0].querySelector('summary').click();

  const trigger = button(overview(), '结论如何得出');
  trigger.focus();
  trigger.click();
  await wait(() => document.querySelector('[role="dialog"]'), '结论说明未打开');
  const dialog = document.querySelector('[role="dialog"]');
  assert(dialog.textContent.includes('不能代替项目 CI 或人工审批'), '通用边界说明应保留');
  click(dialog, '返回概览');
  await wait(() => !document.querySelector('[role="dialog"]'), '结论说明未关闭');
  await wait(() => document.activeElement === trigger, '关闭后未恢复触发按钮焦点');
  click(overview(), '查看完整报告');
  await wait(() => document.querySelector('.rd-aside'), '报告动作未接入');
  click(document.querySelector('nav[aria-label="审查结果导航"]'), '结果概览');
  await wait(overview, '无法返回概览');
  results.push('结论说明、焦点恢复与报告跳转');

  selectScene('inconclusive');
  await wait(() => button(overview(), '查看未检查范围'), '未完整范围入口缺失');
  click(overview(), '查看未检查范围');
  await wait(() => !overview(), '范围入口未跳转');
  assert(document.querySelector('nav[aria-label="审查结果导航"] button.active')?.textContent.includes('覆盖'), '范围入口未定位覆盖页');
  click(document.querySelector('nav[aria-label="审查结果导航"]'), '结果概览');
  await wait(overview, '无法返回概览');
  selectScene('error');
  await wait(() => button(overview(), '重试加载摘要'), '缺失摘要应保留重试入口');
  click(overview(), '重试加载摘要');
  await wait(() => button(overview(), '查看未检查范围'), '重试摘要未恢复情境操作');
  results.push('情境化范围入口与摘要重试');
  return results;
})()
