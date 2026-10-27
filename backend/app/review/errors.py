"""Safe, user-facing explanations for execution failures."""

ERROR_LABELS = {
    "ReviewBudgetExceeded": "达到整轮模型调用或累计输入预算，未完成范围保留为未知",
    "BatchVerificationIncomplete": "批量核验缺失、重复或包含范围外裁决，相关候选未完成",
    "FinalizationRequired": "已进入最终提交阶段，拒绝继续探索；未取得有效最终结果",
    "GraphRecursionError": "达到执行步数上限，未提交最终审查结果",
    "ModelCallLimitExceededError": "模型调用预算已耗尽",
    "ToolCallLimitExceededError": "工具调用预算已耗尽",
    "StructuredOutputValidationError": "最终输出未通过结构校验",
    "MultipleStructuredOutputsError": "最终输出包含相互冲突的结构化结果",
    "TimeoutError": "模型或工具调用超时",
    "APITimeoutError": "模型请求超时",
    "RateLimitError": "模型服务请求受限",
    "AuthenticationError": "模型服务认证失败",
    "APIConnectionError": "模型服务连接失败",
    "review_deadline": "达到本轮审查总期限",
    "review_agents_failed": "所有独立审查分支执行失败",
}


def execution_error_text(code):
    return ERROR_LABELS.get(code, "执行异常，未取得有效结果")
