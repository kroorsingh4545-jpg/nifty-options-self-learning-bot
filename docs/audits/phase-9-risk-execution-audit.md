# Risk & Execution Audit Report

Date: 2026-11-05 (re-audited 2026-11-05 after the fixes for warnings 1, 3 and 4)
Scope (files / PR): branch `phase-9-learning-loop` vs `main`
- first audit (916a04a..bae4ddd): new `learning/` (`trade_journal`, `reflection_engine`, `hypothesis_generator`, `mutation_engine`, `validation_engine`, `promotion_gate`, `deployment_controller`, `loop`, `__main__`), `runtime/learning.py`, `core/autonomy.py`; changed `runtime/bot.py`, `runtime/rl.py`, `ml/inference_service.py`, `ml/model_registry.py`, `backtest/backtester.py`, `data/storage/{schema.sql,database.py,records.py,repositories.py}` (schema v5), `config/settings.py` + `settings.yaml`, `core/enums.py`
- re-audit (warning fixes): `ml/inference_service.py` (`record_decision`), `runtime/rl.py` (`_record`), `data/storage/repositories.py` (`mentioning`), `learning/trade_journal.py`, `runtime/learning.py`; tests in `test_learning_storage.py` and `test_trade_journal.py`
- tests: `test_learning_storage.py`, `test_trade_journal.py`, `test_learning_reflect_mutate.py`, `test_learning_verify_promote.py`, plus changes to `test_database.py`, `test_repositories.py`, `test_ml_training.py`, `test_rl_agent.py`

Mode audited: PAPER and backtest. LIVE order routing does not exist.
Data: SYNTHETIC only. No candidate has been verified on real data.
Tests: **1150 passed** (1144 at the first audit + 6 for the warning fixes). Run against the pre-fix `runtime/rl.py`, the new fault-injection test fails. ruff and mypy (strict, 126 files) clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | An RL ENTER is recorded first, and becomes a veto if the record cannot be built or written: `ml/inference_service.py:414-420`, with building and writing in one guard (`record_decision`, `:330-344`). A suspended strategy is vetoed. An exception in a filter vetoes (`core/interfaces.py:83`). Tests: `test_an_unrecorded_entry_approval_is_vetoed_and_rl_suspended`, `test_an_invalid_entry_record_vetoes_without_raising`, `test_entry_decisions_are_persisted` | The learning loop cannot create entries; it only changes config for the next session. |
| 2 | Exit logic | OK | Exits unchanged. The recorder and journal make no trading calls (`test_the_recorder_and_journal_never_act_on_trading`, AST guard). A management record that cannot be built no longer escapes the candle listener (`runtime/rl.py:104-114` → `record_decision`). Test: `test_management_record_failures_never_escape_the_candle_listener` (every trade still exits under its stop / target / square-off) | Warning 1 is fixed. |
| 3 | Stop-loss logic | OK | `stop_loss.value` bounded to 10-50%, at most 40% change per step. `stop_loss.mode` is never mutable (`learning/mutation_engine.py:147`). The whole config is re-validated (`:355-368`). The rupee risk budget per trade is never mutable. Tests: `test_bounds_grid_and_step_size`, `test_never_mutable`; the fault-injection backtest asserts `stop_loss >= initial_stop_loss` | |
| 4 | Take-profit logic | OK | Only the R-multiple target can be mutated (1-5R) | |
| 5 | Trailing-stop logic | OK | Activation and step stay within per-mode bounds. `trailing.enabled` and `trailing.mode` are never mutable. The trailing manager is unchanged. | |
| 6 | Position sizing | OK | `sizing.size_multiplier` is capped at 0.25-1.0 and can only shrink size; `sizing.risk_per_trade` is never mutable. Tests: `test_degradation_hypothesis_only_reduces_size`, the `size_multiplier 1.2` refusal | |
| 7 | Daily loss limits | OK | Everything under `risk.*` is never mutable, and an overlay that changes `risk.max_daily_loss` is refused. Tests: `test_never_mutable`, `test_overlays_only_accept_whitelisted_differences` | |
| 8 | Global loss limits | OK | Global limits sit outside the strategy config, and requests for them are refused by name (`global`, `capital`, `max_exposure`). Test: `test_never_mutable` | |
| 9 | Margin checks | N/A | Unchanged | |
| 10 | Order lifecycle handling | OK | Unchanged. The journal records rejected, cancelled and unknown orders. Test: `test_execution_quality_flags_failed_orders` | |
| 11 | Partial fills | N/A | Unchanged | |
| 12 | Order rejection handling | OK | Unchanged; rejections are journaled as errors | |
| 13 | Reconciliation | N/A | Unchanged | |
| 14 | Square-off logic | OK | Square-off and the entry cut-off are never mutable. `entry_window.end` is bounded to 14:45 and re-checked against the configured cut-off. Test: the `entry_window.end 15:00` refusal | |
| 15 | Kill-switch behavior | OK | `learning/` never imports broker, execution, risk or runtime (`test_learning_never_imports_broker_execution_or_risk`), and kill-switch paths are never mutable | |
| 16 | API rate-limit compliance | OK | No network access in `learning/` (`test_network_access_only_from_upstox_adapters`) | |
| 17 | PAPER/LIVE isolation | OK | `strategy_deployments` only accepts PAPER rows (`schema.sql:457`). `deploy_live` always refuses (`learning/deployment_controller.py:169`). The controller refuses to start outside PAPER (`:62`). LIVE_APPROVED needs a human approver and a typed phrase (`learning/promotion_gate.py:227-230`), and is recorded only. PAPER_READY needs HISTORICAL evidence plus a permitting autonomy level (`promotion_gate.py:166-174`; triggers `schema.sql:729,739`). The model path is `ml/model_registry.py:291-297` with triggers `schema.sql:625,638`. Tests: `test_live_deployment_is_impossible`, `test_live_approval_is_human_only_and_never_deploys`, `test_synthetic_evidence_never_promotes`, `test_promotion_reads_the_autonomy_level_at_decision_time`, the trigger tests in `test_learning_storage.py` | Phase 7 warning 5 is resolved. |
| 18 | Restart recovery | OK | Closed trades are backfilled at start-up; restored open trades are marked as already snapshotted. Trades created before the session started are journaled as UNKNOWN (`learning/trade_journal.py:235-237`, `:261`; `session_start=bot._now()` at `runtime/learning.py:110`). Tests: `test_trades_from_earlier_sessions_are_journaled_as_unknown`, `test_a_backtest_session_keeps_its_own_label` | Warning 4 is fixed. A trade restored from an earlier session that closes in this one is also UNKNOWN (conservative). |
| 19 | Logging and audit trail | OK | Every RL decision is persisted (append-only, `schema.sql:653-674`), and every loop stage writes `learning_events`. The per-trade risk-event lookup is limited to the trade's own window (creation → exit + 30 min; `learning/trade_journal.py:58`, `:249`; `data/storage/repositories.py:454-469`), using `ix_risk_events_time` (`schema.sql:223`). Tests: `test_every_rl_decision_in_a_backtest_is_persisted`, `test_risk_event_lookup_is_time_bounded_and_indexed` (index in the query plan; under 50 ms per lookup with 20,000 unrelated events), `test_journal_lookup_only_reads_the_trade_window` | Phase 8 warning 2 is resolved. Warning 3 is fixed. |

## Blocking issues

None.

## Warnings

1. **A management decision is written after `apply_advice`.** If that write fails, one applied action has no row; RL is then suspended for the strategy with a CRITICAL log. At most one unaudited action per strategy per session. (Was warning 2.)
2. **The loop's autonomy source is caller-supplied.** Only the CLI wires it (a fixed level). A loop running next to the bot must read the bot's live level, so that a dashboard switch to OBSERVE_ONLY takes effect immediately. The triggers check the level the writer asserts, not the runtime level. (Was warning 5.)
3. **HISTORICAL remains an operator attestation** (dataset label; carried from Phase 7 warning 1). The gate trusts the in-process `ValidationReport`. (Was warning 6.)
4. **Deployments don't change trading yet.** Nothing calls `effective_config()`; there is no launcher until Phase 10. PAPER deployments are recorded but not applied. (Was warning 7.)
5. **Log spam while RL is suspended.** HOLD decisions keep trying to write every minute; with the database down, every failure is logged. (Was warning 8.)
6. **Risk events outside the trade window are not journaled.** Only events from creation to exit + 30 minutes are attached to a trade. A much later escalation that names the trade is still in `risk_events`, just not on the journal row. (New, from the warning 3 fix.)
7. **Phase 5-8 warnings are carried unchanged.**

Resolved since the first audit:
- **Warning 1 (RL record built outside its guard) — FIXED.** `RLTradeAdvisor.record_decision` builds and writes in one guard. A failure returns False and suspends RL for the strategy, and never raises into the candle listener or the entry filter. Tests: `test_management_record_failures_never_escape_the_candle_listener` (fails against the pre-fix code), `test_an_invalid_entry_record_vetoes_without_raising`.
- **Warning 3 (unbounded risk-event scan on the event loop) — FIXED.** The lookup is time-bounded and uses the index. Tests: `test_risk_event_lookup_is_time_bounded_and_indexed`, `test_journal_lookup_only_reads_the_trade_window`.
- **Warning 4 (backfilled trades labelled with the current session's source) — FIXED.** Trades created before the session started are UNKNOWN. Tests: `test_trades_from_earlier_sessions_are_journaled_as_unknown`, `test_a_backtest_session_keeps_its_own_label`.

## Required tests missing

1. A launcher applying `effective_config()` at start-up, then trading the deployed version (Phase 10).
2. The loop's `autonomy_fn` wired to the running bot, with a dashboard autonomy change mid-run.
3. The end-to-end HISTORICAL path (verified → PAPER_READY → deployed → the bot trades it), once real data exists.

---

### First audit (2026-11-05), for the record

Verdict: PASS WITH WARNINGS, 1144 tests passed, no blocking issues. Its warnings 1, 3 and 4 are fixed above. Warnings 2 and 5-9 are carried as warnings 1-5 and 7 here. Its required tests 3 (latency bound for the recorder) and 4 (fault injection for RL record construction) are now covered.
