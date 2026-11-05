"""PPO trade manager (Phase 8): training, evaluation, versioning, safety.

Every model here is trained on SYNTHETIC random-walk data with tiny budgets
(a few hundred PPO steps). They are research artefacts: the tests check that
they can never be validated, promoted, stored in the trading registry or
loaded as production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
import torch

from core.enums import ModelDataSource, ModelRunStatus, ModelType, TradingMode
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.repositories import Repositories
from ml.model_registry import REQUIRED_MANIFEST_KEYS, ModelRegistry, RegistryError, SyntheticModelError
from ml.rl_agent import (
    MODEL_KIND,
    PPOParams,
    PPOTradeManager,
    RLTrainingConfig,
    RLTrainingError,
    RLTrainingPipeline,
    RLTrainingResult,
    compliance,
    compliance_status,
    fit_normalizer,
    masked_choice,
    rollout,
    static_policy,
    train_policy,
    walk_forward_splits,
)
from ml.rl_environment import Action, MarketReplay, RiskEnvelope, TradeManagementEnv, action_mask, observation_vector
from tests.ml_support import config, synthetic_dataset

SID = "vwap_ema"
TINY = RLTrainingConfig(total_timesteps=256, ppo=PPOParams(n_steps=128, batch_size=64, n_epochs=2), n_folds=2)


@dataclass
class Trained:
    result: RLTrainingResult
    registry: ModelRegistry
    root: Path


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> Trained:
    root = tmp_path_factory.mktemp("rl")
    db = Database(root / "research.db")
    db.initialize()
    repos = Repositories(db, TradingMode.PAPER)
    sync_strategies(repos.strategies, config().strategies.strategies)
    registry = ModelRegistry(repos.model_runs, root / "models", allow_synthetic=True)
    result = RLTrainingPipeline(config(), registry).run(synthetic_dataset(24), SID, TINY)
    return Trained(result, registry, root)


# --------------------------------------------------------------------------- #
# Validation design
# --------------------------------------------------------------------------- #


def test_walk_forward_is_chronological_with_embargo_and_holdout() -> None:
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
    folds, dev, hold = walk_forward_splits(days, 3, 1, 0.2)
    assert hold == days[-4:]
    assert max(dev) < min(hold) and (min(hold) - max(dev)).days >= 2  # one embargo day in between
    for train, test in folds:
        assert max(train) < min(test)
        assert (min(test) - max(train)).days >= 2  # embargo
        assert not set(test) & set(hold) and not set(train) & set(hold)
    with pytest.raises(RLTrainingError):
        walk_forward_splits(days[:5], 3, 1, 0.2)


def test_training_config_requires_multiple_seeds() -> None:
    with pytest.raises(RLTrainingError):
        RLTrainingConfig(seeds=(1, 2, 3))
    with pytest.raises(RLTrainingError):
        RLTrainingConfig(total_timesteps=10)


def test_masked_choice_never_picks_an_invalid_action() -> None:
    rng = np.random.default_rng(0)
    for _ in range(500):
        probs = rng.dirichlet(np.ones(len(Action)))
        for mask in (action_mask(True, False), action_mask(False, True), action_mask(False, False)):
            assert mask[masked_choice(probs, mask)]
    flat = np.full(len(Action), 1 / len(Action))
    assert masked_choice(flat, action_mask(True, False)) == Action.HOLD  # ties -> the safest action
    assert masked_choice(flat, action_mask(False, True)) == Action.SKIP_SETUP
    with pytest.raises(ValueError):
        masked_choice(flat, np.zeros(len(Action), dtype=bool))


def test_training_is_reproducible_per_seed() -> None:
    cfg = config()
    replay = MarketReplay(cfg, synthetic_dataset(12), SID, setups="grid", grid_every_minutes=15)
    days = TradeManagementEnv(replay, replay.dataset.days()).days[:3]
    norm = fit_normalizer(replay, days, TINY, 11)
    a = train_policy(replay, days, norm, 11, TINY).state_dict()
    b = train_policy(replay, days, norm, 11, TINY).state_dict()
    c = train_policy(replay, days, norm, 12, TINY).state_dict()
    assert all(torch.equal(a[k], b[k]) for k in a)
    assert not all(torch.equal(a[k], c[k]) for k in a)


# --------------------------------------------------------------------------- #
# Evaluation report
# --------------------------------------------------------------------------- #


def test_evaluation_reports_every_required_metric(trained: Trained) -> None:
    ev = trained.result.evaluation
    assert ev["data_source"] == "SYNTHETIC" and ev["synthetic"] is True
    h = ev["holdout"]
    assert len(h["seeds"]) >= 5 and {"ensemble", "baseline_static", "baseline_skip_all"} <= set(h)
    for m in (h["ensemble"], h["baseline_static"], *h["seeds"].values()):
        for key in ("net_pnl", "max_drawdown", "win_rate_pct", "profit_factor", "trades", "avg_holding_minutes",
                    "expectancy", "charges", "slippage", "compliance", "avg_r_multiple", "invalid_action_rate"):
            assert key in m, key
    assert h["seed_spread_net_pnl"]["n"] == 5
    assert len(ev["walk_forward"]) == TINY.n_folds
    for f in ev["walk_forward"]:
        assert len(f["seeds"]) == 5 and "baseline_static" in f
    assert "reward is reported only" in ev["judged_on"]
    assert ev["holdout"]["baseline_skip_all"]["trades"] == 0
    md = trained.result.report_md
    assert md.startswith("> **SYNTHETIC DATA") and md.rstrip().endswith("used for trading.")
    assert "baseline: static stop/target" in md


def test_gates_judge_equity_drawdown_and_compliance(trained: Trained) -> None:
    gates = trained.result.evaluation["gates"]
    names = " | ".join(gates)
    for word in ("expectancy", "baseline", "drawdown", "compliance", "seeds", "invalid"):
        assert word in names, word
    assert not any("reward" in k.lower() for k in gates)  # never judged on reward alone
    # status follows the gates (SYNTHETIC: TRAINED or REJECTED only)
    status = trained.result.status
    assert status is (ModelRunStatus.TRAINED if trained.result.gates_passed else ModelRunStatus.REJECTED)


def test_trained_agent_respects_the_risk_envelope(trained: Trained) -> None:
    ev = trained.result.evaluation
    for f in ev["walk_forward"]:
        for m in f["seeds"].values():
            assert compliance_status(m["compliance"]) != "FAIL"
    assert compliance_status(ev["holdout"]["ensemble"]["compliance"]) != "FAIL"


def test_compliance_detects_breaches() -> None:
    e = RiskEnvelope.from_config(config(), SID)
    ok = {"day": "2026-09-01", "signal_time": "2026-09-01T10:00:00+05:30", "entry_time": "2026-09-01T10:00:00+05:30",
          "exit_time": "2026-09-01T10:30:00+05:30", "entry": 100.0, "initial_stop": 75.0, "final_stop": 80.0,
          "stop_history": [("a", 75.0), ("b", 80.0)], "quantity": 65, "net_pnl": 300.0}
    assert compliance_status(compliance([ok], e)) == "PASS"
    late = {**ok, "entry_time": "2026-09-01T14:50:00+05:30", "signal_time": "2026-09-01T14:50:00+05:30"}
    widened = {**ok, "stop_history": [("a", 80.0), ("b", 75.0)], "final_stop": 75.0}
    held = {**ok, "exit_time": "2026-09-01T15:15:00+05:30"}
    big = {**ok, "quantity": 65 * 5}
    for bad in (late, widened, held, big):
        assert compliance_status(compliance([bad], e)) == "FAIL", bad
    loss = {**ok, "net_pnl": -1500.0}
    cooled = {**ok, "signal_time": "2026-09-01T10:35:00+05:30", "entry_time": "2026-09-01T10:35:00+05:30",
              "exit_time": "2026-09-01T10:50:00+05:30"}
    assert compliance_status(compliance([loss, cooled], e)) == "FAIL"  # entered inside the loss cooldown


# --------------------------------------------------------------------------- #
# Versioning and the synthetic-model firewall
# --------------------------------------------------------------------------- #


def test_manifest_records_everything_needed(trained: Trained) -> None:
    reg = trained.result.registered
    assert reg is not None
    rec, man = reg.record, reg.manifest
    assert rec.model_type is ModelType.RL_PPO and man["model_kind"] == MODEL_KIND
    assert rec.data_source is ModelDataSource.SYNTHETIC and rec.model_version.endswith("-synthetic")
    for k in REQUIRED_MANIFEST_KEYS:
        assert man.get(k), k
    assert man["feature_schema"]["names"] and len(man["feature_schema"]["hash"]) == 64  # observation schema
    assert len(man["hyperparameters"]["seeds"]) >= 5 and len(rec.seeds_json) >= 5
    assert {"torch", "stable_baselines3", "gymnasium", "rl_env", "obs_schema", "reward"} <= set(man["environment"])
    assert rec.environment_version.startswith("rl-pipeline-")
    assert man["environment_spec"]["reward"]["components"]
    assert "square-off timing" in man["environment_spec"]["agent_never_controls"]
    files = set(man["files"])
    assert "observation_normalizer.json" in files and sum(f.endswith(".pt") for f in files) == 5
    assert (reg.directory / "evaluation_report.md").read_text(encoding="utf-8").startswith("> **SYNTHETIC")


def test_synthetic_rl_model_can_never_become_production(trained: Trained, tmp_path: Path) -> None:
    reg = trained.result.registered
    assert reg is not None
    run_id = reg.record.run_id
    with pytest.raises(SyntheticModelError):
        trained.registry.mark_validated(run_id)
    with pytest.raises(SyntheticModelError):
        trained.registry.mark_paper_ready(run_id, approved_by="someone")
    with pytest.raises(RegistryError):
        trained.registry.set_status(run_id, ModelRunStatus.LIVE_APPROVED)
    for mode in TradingMode:
        assert trained.registry.load_production(ModelType.RL_PPO, SID, mode) is None
    # the trading registry refuses SYNTHETIC models outright
    db = Database(tmp_path / "trading.db")
    db.initialize()
    repos = Repositories(db, TradingMode.PAPER)
    sync_strategies(repos.strategies, config().strategies.strategies)
    trading = ModelRegistry(repos.model_runs, tmp_path / "models")
    with pytest.raises(SyntheticModelError):
        trading.register(trained.result.model, kind=MODEL_KIND, strategy_id=SID, data_source="SYNTHETIC",
                         manifest=reg.manifest, report_md="x")
    # and the database itself refuses a promoted status for non-HISTORICAL rows
    with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError from the schema trigger
        trained.registry.runs.set_status(run_id, ModelRunStatus.PAPER_READY, approved_by="x")


def test_saved_model_reloads_and_acts_identically(trained: Trained) -> None:
    reg = trained.result.registered
    assert reg is not None
    loaded = trained.registry.load(reg.record.run_id)
    m1, m2 = trained.result.model, loaded.model
    assert isinstance(m2, PPOTradeManager) and m2.seeds == m1.seeds
    replay = MarketReplay(config(), synthetic_dataset(24), SID, setups="grid", grid_every_minutes=15)
    env = TradeManagementEnv(replay, replay.dataset.days())
    for d in env.days[-3:]:
        _, info = env.reset(options={"day": d})
        done = False
        while not done:
            raw = observation_vector(env.raw_observation())
            a1, i1 = m1.act(raw, info["action_mask"])
            a2, i2 = m2.act(raw, info["action_mask"])
            assert a1 == a2 and i1["probabilities"] == i2["probabilities"]
            assert info["action_mask"][a1]
            _, _, done, _, info = env.step(a1)


def test_tampered_artifacts_are_refused(trained: Trained) -> None:
    reg = trained.result.registered
    assert reg is not None
    pt = next(p for p in reg.directory.glob("*.pt"))
    original = pt.read_bytes()
    try:
        pt.write_bytes(original + b"x")
        with pytest.raises(RegistryError):
            trained.registry.load(reg.record.run_id)
    finally:
        pt.write_bytes(original)
    norm = reg.directory / "observation_normalizer.json"
    text = norm.read_text(encoding="utf-8")
    try:
        d = json.loads(text)
        d["mean"][0] += 1
        norm.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(RegistryError):
            trained.registry.load(reg.record.run_id)
    finally:
        norm.write_text(text, encoding="utf-8")
    assert trained.registry.load(reg.record.run_id).model is not None


def test_policy_files_are_plain_tensors(trained: Trained) -> None:
    """No pickled code: every policy file loads with weights_only=True."""
    reg = trained.result.registered
    assert reg is not None
    for pt in reg.directory.glob("*.pt"):
        state = torch.load(pt, map_location="cpu", weights_only=True)
        assert all(isinstance(v, torch.Tensor) for v in state.values())


def test_static_baseline_matches_the_strategy_rules() -> None:
    replay = MarketReplay(config(), synthetic_dataset(12), SID, setups="grid", grid_every_minutes=15)
    r = rollout(replay, replay.dataset.days(), static_policy, normalizer=None, rewards=TINY.rewards)
    assert r.trades and r.invalid == 0
    assert {t["exit_reason"] for t in r.trades} <= {"STOP_LOSS", "TARGET", "SQUARE_OFF", "DATA_END",
                                                     "DAILY_LOSS_LIMIT"}
    assert all(t["final_stop"] == t["initial_stop"] for t in r.trades)


def test_cli_synthetic_rl_run_never_touches_the_trading_database(  # type: ignore[no-untyped-def]
        config_dir: Path, edit_yaml, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ml.__main__ import main

    def point(d: dict) -> None:  # type: ignore[type-arg]
        d["ml"]["research_dir"] = str(tmp_path / "research")
        d["ml"]["artifact_dir"] = str(tmp_path / "models")
        d["database"]["path"] = str(tmp_path / "trading.db")

    edit_yaml("settings.yaml", point)
    rc = main(["train-rl", "--config-dir", str(config_dir), "--synthetic", "--lot-units", "65", "--days", "24",
               "--start", "2026-09-01", "--strategy", SID, "--timesteps", "128", "--n-steps", "128", "--folds", "2"])
    out = capsys.readouterr().out
    assert rc == 0 and "SYNTHETIC dataset" in out and "-synthetic" in out and "never be validated" in out
    assert not (tmp_path / "trading.db").exists() and not (tmp_path / "models").exists()
    manifests = list((tmp_path / "research" / "models" / "RL_PPO").rglob("manifest.json"))
    assert len(manifests) == 1
    man = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert man["data_source"] == "SYNTHETIC" and man["synthetic"] is True


def test_cli_refuses_an_unlabelled_dataset_for_rl(config_dir: Path, edit_yaml, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from ml.__main__ import main

    def point(d: dict) -> None:  # type: ignore[type-arg]
        d["ml"]["research_dir"] = str(tmp_path / "research")
        d["ml"]["artifact_dir"] = str(tmp_path / "models")
        d["database"]["path"] = str(tmp_path / "trading.db")

    edit_yaml("settings.yaml", point)
    (tmp_path / "ds").mkdir()
    with pytest.raises(SystemExit):
        main(["train-rl", "--config-dir", str(config_dir), "--data", str(tmp_path / "ds"), "--strategy", SID])
    assert not (tmp_path / "trading.db").exists() and not (tmp_path / "research").exists()
