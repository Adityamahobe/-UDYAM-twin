"""Train the RL maintenance policy for the VMC in simulation and evaluate it on held-out seeds.

    python -m ml.train_rl                 (≈ 1–2 min; writes models/rl_policy.json and models/rl_metrics.json)
    python -m ml.train_rl --episodes 3000 (quicker, rougher)

The learned policy is compared with two baselines on the same held-out seeds: always continue at
full speed, and a rule-of-thumb policy (derate on heat or load, maintain on zone D or a worn tool).
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from ml.maintenance_rl import (ACTIONS, DECISION_S, METRICS_PATH, POLICY_PATH, REWARD, REWARD_BASIS, SCENARIO_FAULTS,
                               evaluate, returns_curve, rule_policy, train)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--episodes", type=int, default=15000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--eval-episodes", type=int, default=100, help="held-out episodes per scenario")
    args = ap.parse_args()

    t0 = time.time()
    print(f"Training Q-learning on {args.episodes} simulated VMC shifts (12 min each, {DECISION_S:g} s decisions)…")
    pol, returns = train(args.episodes, args.seed, log=print)
    train_s = round(time.time() - t0, 1)

    print(f"Evaluating on {args.eval_episodes} held-out seeds per scenario…")
    results = {
        "always_continue": evaluate(lambda s: "continue", args.eval_episodes),
        "rule_based": evaluate(rule_policy, args.eval_episodes),
        "rl_policy": evaluate(pol.best, args.eval_episodes),
    }
    trained_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    pol.meta = {
        "algorithm": "tabular Q-learning (ε-greedy 1.0 → 0.05, learning rate 1/(1+n)^0.6, γ = 0.9)",
        "environment": "VMC plant physics from sim/plant.py; scenarios " + ", ".join(SCENARIO_FAULTS)
                       + "; random injection time, severity, ambient, load and starting tool wear",
        "episodes": args.episodes, "seed": args.seed, "trained_at": trained_at, "train_seconds": train_s,
        "decision_interval_s": DECISION_S,
    }
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(pol.to_json(), indent=1), encoding="utf-8")

    metrics = {
        "label": "Simulation results on held-out seeds (not a real plant)",
        "policy": pol.meta, "actions": list(ACTIONS), "reward": {"inr": REWARD, "basis": REWARD_BASIS},
        "eval_episodes_per_scenario": args.eval_episodes,
        "per_episode": "means per 12-minute simulated shift",
        "results": results,
        "returns_curve": returns_curve(returns),
        "states_learned": len(pol.q),
    }
    METRICS_PATH.write_text(json.dumps(metrics, indent=1), encoding="utf-8")

    print(f"\nHeld-out results (mean per 12-min shift), trained in {train_s} s, {len(pol.q)} states:")
    print(f"{'scenario':18s} " + "".join(f"{p:>18s}" for p in results))
    for sc in list(SCENARIO_FAULTS) + ["all"]:
        print(f"{sc:18s} " + "".join(f"₹{results[p][sc]['return_inr']:>14,.0f}   " for p in results))
    print(f"{'trips (all)':18s} " + "".join(f"{results[p]['all']['trips']:>17.2f} " for p in results))
    print(f"\nWrote {POLICY_PATH.relative_to(POLICY_PATH.parent.parent)} and {METRICS_PATH.relative_to(METRICS_PATH.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
