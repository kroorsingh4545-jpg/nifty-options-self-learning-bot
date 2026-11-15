# Risk & Execution Audit Report

Date: 2026-11-15
Scope (files / PR): branch `decision-d7-shadow-engine` vs `main` 8d523ac (decision-engine D7: shadow decision
engine), two commits:
- `c3dbd85` **pre-registration only**: `research/decision/{__init__,rules}.py` (`RULES_V1`, fingerprint
  `4632769f...c767`), `docs/decision-engine-d7-preregistration.md`, `tests/unit/test_decision_rules_registered.py`.
  Committed before the engine existed or ran on any signal; unchanged since (`git diff c3dbd85` on both files is empty).
- the engine commit: `research/decision/{engine,bias,shadow,evaluate,orb_direction}.py`; `research/__main__.py`
  (`shadow` stores verdicts; offline `decision-report`, `orb-direction`), `research/shadow/report.py`,
  `research/store/*` (v7: `shadow_decisions`), `tests/unit/test_decision_engine.py`, docs.
- **Untouched:** no file in `risk/`, `execution/`, `broker/`, `strategies/`, `runtime/`, `core/`, `data/`, `config/`,
  `dashboard/`, `ml/`, `learning/`, `options/` or `volatility/` changed (`git diff --name-only main`); no existing test
  changed.

Checks: **2141 passed, 0 failed** (1 existing NumPy deprecation warning); `ruff check .` and `mypy .` (strict,
190 files) clean.

Verdict: **PASS WITH WARNINGS**

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | No bot code changed; verdicts go only to `shadow_decisions` in the research database (`research/store/schema.sql:402`) | The bot never imports `research` (isolation test), so a verdict cannot reach the entry path. |
| 2 | Exit logic | N/A | unchanged | |
| 3 | Stop-loss logic | N/A | unchanged | |
| 4 | Take-profit logic | N/A | unchanged | |
| 5 | Trailing-stop logic | N/A | unchanged | |
| 6 | Position sizing | N/A | unchanged | The engine sizes nothing. |
| 7 | Daily loss limits | N/A | unchanged | |
| 8 | Global loss limits | N/A | unchanged | |
| 9 | Margin checks | N/A | unchanged | |
| 10 | Order lifecycle handling | N/A | unchanged | |
| 11 | Partial fills | N/A | unchanged | |
| 12 | Order rejection handling | N/A | unchanged | |
| 13 | Reconciliation | N/A | unchanged | |
| 14 | Square-off logic | N/A | unchanged | |
| 15 | Kill-switch behavior | N/A | unchanged | The engine cannot read or set it. |
| 16 | API rate-limit compliance | OK | `decision-report`, `orb-direction` not in `NETWORK_COMMANDS` (`research/__main__.py:112`); no network client imported | Offline. |
| 17 | PAPER/LIVE isolation | OK | `research/decision/*` imports only the standard library, numpy, `research.*` and `volatility.calibration`; `test_research_isolation.py` passes for every new file | Shadow only: promotion beyond shadow needs the owner and `/audit-risk` (pre-registration "Scope"). |
| 18 | Restart recovery | N/A | unchanged | Store v7 migrates by `CREATE TABLE IF NOT EXISTS`; `upsert_decisions` idempotent (`test_decisions_are_stored_and_refuse_unknown_columns`). |
| 19 | Logging and audit trail | OK | Every verdict stores the rules version and fingerprint, all inputs, the components (D5 / D6 slots NOT_AVAILABLE) and the reasons; reports `decision_engine` and `orb_direction` stored | No secrets read. |

**Pre-registration integrity, tested:**
- The rules match their registered fingerprint and the document records it
  (`test_the_rules_match_their_registered_fingerprint`, `test_the_registration_document_records_the_same_fingerprint_and_version`);
  any change alters the fingerprint (`test_any_change_changes_the_fingerprint`); the engine refuses to run altered rules
  (`test_the_engine_refuses_to_run_unregistered_rules`).
- The breakeven formula makes the expected P&L zero (Monte Carlo, 2 million draws:
  `test_breakeven_hit_rate_makes_expected_pnl_zero`); verdict thresholds, confidence, missing inputs
  (8 cases), inert D5 / D6 slots.
- Point in time: the variance bias uses only sessions before the signal day, the same VIX-change state, pooled below
  100 windows, 30-minute windows only (`test_bias_uses_only_earlier_sessions_and_the_same_state`).
- The evaluation counts only signals from 2026-11-16, only TRADE / NO_TRADE verdicts, and waits for the registered
  sample sizes (`test_the_evaluation_waits_for_the_registered_sample_sizes`,
  `test_signals_before_registration_are_never_counted`, `test_insufficient_evidence_verdicts_are_not_counted`).

**Results (scratch copies):** historical ORB signals moved in their own direction 51.3 % of the time after
30 minutes (95 % CI 48.3-54.2 %, n = 1,086) and 52.4 % after 60 minutes (49.4-55.4 %); the engine answered NO_TRADE
to all 10 recorded signals (p* 0.557-0.599), all before registration and not counted.

## Blocking issues
None.

## Warnings
1. **v1 may never become evaluable.** It answered NO_TRADE to every signal so far; if TRADE stays rare, its TRADE
   group will not reach the registered 100 signals. That is a property of the evidence (VIX over-prices intraday
   variance and no stable directional edge was found), not a defect to tune away. Any other comparison needs a new
   pre-registered version.
2. **The variance bias comes from India VIX, applied to the option's own IV.** A weekly ATM option's IV is not VIX;
   the bias measures how VIX-implied variance compares with realised, and is assumed to carry over.
3. **The p* model assumes** a directional view independent of the move's size, a normal 30-minute move and a
   30-minute hold; the strategies' real holds differ (D2 holds ran 13 to 180 minutes).
4. **The 0.55 threshold is a judgement** made before any result (documented reason: no stable directional evidence
   in D4), not a fitted value. It cannot change in v1.
5. **The ORB direction study assumes every historical signal was taken** and covers ORB only (VWAP+EMA needs
   futures volume history).
6. **D5 and D6 are not built.** Their slots are present and inert; adding evidence there will need a new version
   and registration.

## Required tests missing
1. None that can run offline. The registered evaluation runs forward from 2026-11-16 (`decision-report`).
