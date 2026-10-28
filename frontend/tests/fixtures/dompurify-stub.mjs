// SSR 测试环境没有 DOM，真实 DOMPurify 无法初始化；渲染测试只验证组件结构，清洗行为由浏览器构建保证。
export default { sanitize: value => value };
