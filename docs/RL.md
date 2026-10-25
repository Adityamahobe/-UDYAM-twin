# Reinforcement-learning maintenance policy

A Q-learning policy for the CNC mill (VMC-01) that recommends, every 20 s of machine time, one of:
**continue** at full speed, **derate 20 %**, **derate 40 %**, or **stop for maintenance** (a simulated repair).
It is trained in simulation, learns online from the virtual factory, and **only recommends**.

```
            ┌──────────────── feedback to improve future decisions ────────────────┐
            ▼                                                                       │
 Predicted risk / selected action ─► Simulated outcome ─► Evaluate ─────────► Update RL policy
 (twin rules + AI4I risk, Q-table)    (next 20 s in the   (prediction correct?   (Q-learning, SIM
                                       virtual factory)    reward ₹)              mode only)
```

## State, actions, reward

| | |
|---|---|
| State (6 features, same in training and live) | temperature margin to the 42.35 °C limit (> 3, 1–3, < 1 °C, over), temperature trend over the last 20 s, vibration zone (A/B, C, D), spindle load (≥ 85 % of rated or not), tool wear (OK, > 180 min, > 200 min), current derate (0, 20, 40 %) |
| Actions | continue, derate_20, derate_40, maintain |
| Reward (₹ per 20 s) | parts × ₹450 (`site.inr_per_part`) − ₹40/s over the temperature limit − ₹5/s in zone C − ₹60/s in zone D − ₹10/s overloaded − ₹15/s with a tool past 200 min − maintenance ₹1,500 and no parts that interval − trip: 4 h × ₹2,500/h (`site.downtime_cost_inr_per_hr`), which ends the shift |

Part value and downtime cost come from `config/machines.yaml`; **the penalty rates are illustrative assumptions** for the
simulation (they are in `ml/maintenance_rl.py` `REWARD` and in every metrics file and export).

## Training (`python -m ml.train_rl`)

- Environment: the VMC plant physics from `sim/plant.py` (the live simulator's), stepped without the twin so it runs fast.
  Each 12-minute shift picks no fault or one of the four fault-lab scenarios (bearing, coolant loss, accelerated tool
  wear, overload), injects it at a random time, and randomises severity, ambient temperature, load and starting tool wear.
- Algorithm: tabular Q-learning, ε-greedy 1.0 → 0.05, learning rate 1/(1+n)^0.6, γ = 0.9, 15,000 shifts (~2 min).
  States never seen in training fall back to the rule baseline.
- Evaluation: 100 held-out seeds per scenario, against *always continue* and a *rule-based* policy (the twin's behaviour
  today as rules: derate on heat or load, maintain on zone D). Results in `models/rl_metrics.json`.

Held-out results (mean ₹ per 12-minute shift, from the run in this repository):

| Scenario | Always continue | Rule-based | RL policy |
|---|---|---|---|
| none | 9,333 | 9,275 | 9,322 |
| vibration fault | −12,005 | **6,829** | 6,777 |
| over-temperature | −6,534 | 5,878 | **7,086** |
| tool wear | 9,327 | 9,263 | 9,326 |
| overload | −4,230 | 5,476 | **6,417** |
| **all** | −822 | 7,344 | **7,785** |
| trips per shift | 0.20 | 0 | 0 |

RL gains over the rules come from over-temperature and overload (it derates earlier and by the right amount); it is
about equal elsewhere and slightly behind on the bearing fault.

## Live feedback loop

`FeedbackAgent` (in `ml/maintenance_rl.py`) watches every broadcast batch. For each CNC mill, every 20 s it:

1. **Closes the previous decision**: works out the action actually taken (the mean derate, or *maintain* if a
   resolution happened), the outcome (parts, seconds over temperature, zone C/D, overload, worn tool, trip), the reward,
   and whether the twin's predicted risk was right (adverse outcome expected vs. happened).
2. **Updates the Q-table** for that state and action (learning rate 0.1), in **SIM mode only**: REPLAY and LIVE data are
   evaluated but never learned from.
3. **Opens a new decision** with the current state, the twin's predicted risk and the policy's recommendation.

Each evaluated decision is stored in SQLite (`rl_decisions`) and exported as the *RL decisions* sheet. Online updates
live in memory and start again from `models/rl_policy.json` when the server restarts.

## Never actuates

The recommendation is advice. *Send for operator approval* calls `POST /api/rl/{machine_id}/propose`, which creates a
pending proposal (derate / clear derate / work order). It is executed only by
`POST /api/chat/proposals/{id}/approve` (operator token, or localhost; single use; 15-minute expiry), in the simulator or
the work-order log. No command is sent to a physical machine.

## API

- `GET /api/rl?recent=20`: recommendation per CNC mill (state, Q-values, predicted risk), loop statistics
  (decisions, prediction accuracy, advice followed, mean reward), recent evaluated decisions, reward assumptions.
- `GET /api/rl/metrics`: training and held-out evaluation results.
- `POST /api/rl/{machine_id}/propose`: 200 `{proposal, recommendation}`; 404 no recommendation yet; 409 nothing to change.

## Limitations

Trained and evaluated in our own simulator with illustrative costs: the numbers show the policy learned this simulator,
not that it would run a real plant well. Tabular states are coarse; the live state uses the twin's readings, which can
differ slightly from the training plant (the line can starve or block the VMC).
