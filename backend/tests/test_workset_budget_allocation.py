import pytest

from app.agents.providers import LLMProviderError
from app.models.review import ExecutionBudget, ReviewUnitComplexity, UnitDiffBatch
from app.review.unit_completion import is_review_unit_complete, is_reusable_review_unit_result
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_worksets import UNIT_WORKSET_VERSION
from test_large_unit_worksets import WorksetProvider, large_file, review_unit
from test_unit_review_summary import _state


def five_batch_input(complexity='large', estimated_tokens=200000):
    # 17 canonical hunks split deterministically into 4/4/4/2/3 under the active-set cap.
    file = large_file(count=1, width=8)
    hunks = []
    for index in range(17):
        hunk = file.hunks[0].model_copy(deep=True)
        hunk.hunk_id = f'hunk-{index}'
        hunk.new_start = index + 1
        hunk.lines[0].new_line_no = index + 1
        hunk.lines[0].content = f'{index}:' + 'x' * 8
        hunk.added_lines[0].line_no = index + 1
        hunk.added_lines[0].content = f'{index}:' + 'x' * 8
        hunks.append(hunk)
    file.hunks = hunks
    file.additions = 17
    unit = review_unit().model_copy(update={'complexity': ReviewUnitComplexity(complexity), 'estimated_tokens': estimated_tokens})
    state = _state()
    state['changed_files'] = [file.model_dump(mode='json')]
    return unit, state


@pytest.mark.asyncio
@pytest.mark.parametrize('complexity', ['small', 'medium', 'large'])
async def test_five_batches_complete_with_real_default_unit_budget(complexity):
    unit, state = five_batch_input(complexity)
    provider = WorksetProvider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=30)
    baseline = runner._budget_for(unit)
    result = await runner.execute_unit(unit, state)
    assert len(result.diff_batches) == 5
    assert len(provider.requests) == 5
    assert all(entry.status == 'completed' for entry in result.diff_batches), result.model_dump()
    assert is_review_unit_complete(result) and is_reusable_review_unit_result(result)
    budget = result.execution_budget
    assert budget.diagnosis_attempts == 5
    assert budget.max_diagnosis_attempts == baseline.max_diagnosis_attempts + 4
    assert budget.max_model_calls == baseline.max_model_calls + 4
    assert budget.model_calls <= budget.max_model_calls
    assert budget.max_token_usage == baseline.max_token_usage
    assert budget.token_usage <= budget.max_token_usage
    assert budget.max_context_retrievals == baseline.max_context_retrievals


@pytest.mark.asyncio
async def test_insufficient_total_tokens_still_reviews_first_affordable_batch():
    unit, state = five_batch_input(estimated_tokens=0)
    provider = WorksetProvider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=30)
    baseline = runner._budget_for(unit)
    result = await runner.execute_unit(unit, state)
    assert len(result.diff_batches) == 5
    assert sum(item.admission.get('reserved_tokens', 0) for item in result.diff_batches) > baseline.max_token_usage
    assert provider.requests and result.diff_batches[0].status == 'completed'
    assert any(item.status == 'skipped' for item in result.diff_batches)
    assert result.execution_budget.max_token_usage == baseline.max_token_usage
    assert result.execution_budget.token_usage <= baseline.max_token_usage
    assert result.input_coverage.omitted_targets
    assert result.review_summary.status == 'unknown'
    assert not is_review_unit_complete(result) and not is_reusable_review_unit_result(result)


class DiagnosisOnlyProvider(WorksetProvider):
    def unit_decision_admission(self, state, model):
        raise LLMProviderError('model_input_limit_exceeded: fixture decision unavailable')


@pytest.mark.asyncio
async def test_resume_preserves_used_budget_and_does_not_allocate_it_again():
    # 支持性交接使每批请求更大；估算必须覆盖首轮 5 批加一次重试。
    unit, state = five_batch_input(estimated_tokens=240000)
    provider = DiagnosisOnlyProvider('error')
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=30)
    first = await runner.execute_unit(unit, state)
    assert len(provider.requests) == 5 and not is_review_unit_complete(first)
    assert first.execution_budget.diagnosis_attempts == 5
    completed_ids = {entry.id for entry in first.diff_batches if entry.status == 'completed'}
    provider.failure = None
    state['review_unit_results'] = [first.model_dump(mode='json')]
    second = await runner.execute_unit(unit, state)
    assert len(provider.requests) == 6 and is_review_unit_complete(second)
    assert second.execution_budget.diagnosis_attempts == 6
    for metric in ('model_calls', 'diagnosis_attempts', 'token_usage'):
        assert getattr(second.execution_budget, metric) > getattr(first.execution_budget, metric)
        assert getattr(second.execution_budget, f'max_{metric}') == getattr(first.execution_budget, f'max_{metric}')
    assert all(entry.reason == 'restored_valid_batch' for entry in second.diff_batches if entry.id in completed_ids)
    state['review_unit_results'] = [second.model_dump(mode='json')]
    third = await runner.execute_unit(unit, state)
    assert len(provider.requests) == 6 and third.execution_budget == second.execution_budget


@pytest.mark.asyncio
async def test_parent_timeout_retains_batch_aware_limits_and_actual_consumption():
    unit, state = five_batch_input()
    provider = DiagnosisOnlyProvider('timeout')
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=1)
    result = await runner.execute_unit(unit, state)
    assert len(provider.requests) == 2
    assert result.diff_batches[0].status == 'completed'
    # Parent cancellation leaves the in-flight outcome unknown, rather than claiming completion.
    assert result.diff_batches[1].status != 'completed'
    assert result.input_coverage.reason == 'review_unit_timed_out'
    assert result.execution_budget.max_diagnosis_attempts == 7
    assert result.execution_budget.max_model_calls == 11
    assert result.execution_budget.model_calls == len(provider.requests) + provider.decisions
    assert result.execution_budget.diagnosis_attempts == 2
    assert result.execution_budget.token_usage == (
        sum(item[2]['reserved_tokens'] for item in provider.requests) + 1000 * provider.decisions)
    assert result.input_coverage.omitted_targets and not is_review_unit_complete(result)


@pytest.mark.parametrize('complexity,attempts,calls', [('small', 1, 3), ('medium', 2, 5), ('large', 3, 7)])
@pytest.mark.parametrize('count', [1, 5, 32])
def test_workset_budget_has_one_base_diagnosis_per_batch_and_fixed_overhead(complexity, attempts, calls, count):
    unit, _ = five_batch_input(complexity)
    budget = ReviewUnitExecutor._budget_for(unit, workset_count=count)
    assert budget.max_diagnosis_attempts == count + attempts - 1
    assert budget.max_model_calls == count + calls - 1
    assert budget.max_token_usage == ReviewUnitExecutor._budget_for(unit).max_token_usage


def test_future_holdback_fits_all_dimensions_and_does_not_starve_current_batch():
    entries = [UnitDiffBatch(id=f'b{i}', parent_unit_id='u', input_hash=f'h{i}', ranges=[], version=UNIT_WORKSET_VERSION,
                            admission={'admitted': True, 'reserved_tokens': cost})
               for i, cost in enumerate([4000, 1000, 1000])]
    budget = ExecutionBudget(model_calls=1, max_model_calls=3, diagnosis_attempts=1,
                             max_diagnosis_attempts=3, token_usage=1000, max_token_usage=6000)
    held = ReviewUnitExecutor._workset_holdback(budget, entries, 2000, 100)
    assert held['workset_ids'] == ['b1']
    assert held['model_calls'] == held['diagnosis_attempts'] == 1
    assert held['token_usage'] == 1100
    assert budget.can_consume(model_calls=1 + held['model_calls'],
                             diagnosis_attempts=1 + held['diagnosis_attempts'],
                             token_usage=2000 + held['token_usage'])


@pytest.mark.parametrize('count', [0, 33])
def test_workset_budget_refuses_counts_outside_scheduler_bound(count):
    unit, _ = five_batch_input()
    with pytest.raises(ValueError, match='invalid_unit_workset_count'):
        ReviewUnitExecutor._budget_for(unit, workset_count=count)
