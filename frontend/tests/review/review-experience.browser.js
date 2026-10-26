(async () => {
  const assert = (ok, text) => { if (!ok) throw new Error(text); };
  const wait = async (predicate, text) => { for (let i = 0; i < 200; i++) {
    if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 25));
  } throw new Error(text); };
  const button = label => [...document.querySelectorAll('button')].find(node => node.textContent.trim() === label);
  await wait(() => button('详细审查报告'), '报告未加载');
  button('详细审查报告').click();
  await wait(() => document.querySelector('#rd-detail-appendix details'), '详细报告未打开');
  const detail = document.querySelector('#rd-detail-appendix details');
  detail.open = true;
  detail.scrollIntoView();
  const position = window.scrollY;
  const copy = [...document.querySelectorAll('.rd-aside button')].find(node => node.textContent.includes('复制'));
  document.querySelector('[aria-label="刷新同任务报告"]').click();
  await wait(() => copy.disabled, '刷新期间未暂停交付');
  assert(detail.isConnected && detail.open, '刷新开始时章节被卸载');
  await wait(() => document.body.textContent.includes('报告已更新，已保留当前阅读位置与选择'), '缺少更新提示');
  assert(document.querySelector('[role="tab"][aria-selected="true"]').textContent.includes('详细审查报告'), '刷新切回其他报告');
  assert(detail.isConnected && detail.open, '刷新后章节展开状态丢失');
  assert(Math.abs(window.scrollY - position) < 90, '刷新导致阅读位置明显跳动');
  const select = document.querySelector('select[aria-label="验收场景"]');
  select.value = 'clean'; select.dispatchEvent(new Event('change', { bubbles: true }));
  await wait(() => document.querySelector('[role="tab"][aria-selected="true"]')?.textContent.includes('PR 评论草稿'), '跨任务未恢复默认报告');
  return { passed: ['刷新保留详细报告', '章节 DOM 与展开状态保持', '滚动位置保持', '更新期间暂停交付', '切换任务恢复默认'] };
})()
