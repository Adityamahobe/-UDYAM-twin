# Reinforcement-learning maintenance policy

A Q-learning policy for the CNC mill (VMC-01) that recommends, every 20 s of machine time, one of:
**continue** at full speed, **derate 20 %**, **derate 40 %**, **inspect** (a short stop that shows whether the VMC really
has a fault) or **stop for maintenance** (a simulated repair).
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
| State (8 features, same in training and live) | temperature margin to the 42.35 °C limit (> 3, 1–3, < 1 °C, over), temperature trend over the last 20 s, vibration zone (A/B, C, D), spindle load (≥ 85 % of rated or not), tool wear (OK, > 180 min, > 200 min), current derate (0, 20, 40 %), compressed-air pressure (OK ≥ 6.5, warning, low < 6.0 bar: the interlock then derates the VMC 25 %), latest inspection result for 60 s (none, fault found, all clear) |
| Actions | continue, derate_20, derate_40, inspect, maintain |
| Reward (₹ per 20 s) | parts × ₹450 (`site.inr_per_part`) − ₹40/s over the temperature limit − ₹5/s in zone C − ₹60/s in zone D − ₹10/s overloaded − ₹15/s with a tool past 200 min − maintenance ₹1,500 and no parts that interval − inspection ₹400 and a quarter of that interval's parts − trip: 4 h × ₹2,500/h (`site.downtime_cost_inr_per_hr`), which ends the shift |

Part value and downtime cost come from `config/machines.yaml`; **the penalty rates are illustrative assumptions** for the
simulation (they are in `ml/maintenance_rl.py` `REWARD` and in every metrics file and export).

## Training (`python -m ml.train_rl`)

- Environment: the VMC and air-compressor physics from `sim/plant.py` (the live simulator's), stepped without the twin so
  it runs fast. Each 12-minute shift picks no fault, one of the four VMC fault-lab scenarios (bearing, coolant loss,
  accelerated tool wear, overload) or a compressor pressure drop, injects it at a random time, and randomises severity,
  ambient temperature, load and starting tool wear. An inspection reveals whether the VMC (not the compressor) has a fault.
- Algorithm: tabular Q-learning, ε-greedy 1.0 → 0.05, learning rate 1/(1+n)^0.6, γ = 0.9, 25,000 shifts (~4 min).
  States never seen in training fall back to the rule baseline.
- Evaluation: 100 held-out seeds per scenario, against *always continue*, the *rules* the twin uses today (derate on heat or
  load, maintain on zone D, leave low air to the interlock) and *rules + inspect* (inspect on early warning signs, maintain
  if the inspection finds a fault). Results in `models/rl_metrics.json`.

Held-out results (mean ₹ per 12-minute shift, from the run in this repository):

| Scenario | Always continue | Rules (today) | Rules + inspect | RL policy |
|---|---|---|---|---|
| none | **9,333** | 9,285 | 9,077 | 9,311 |
| vibration fault | −12,004 | **6,816** | 6,624 | 6,804 |
| over-temperature | −6,524 | 5,855 | 6,764 | **7,096** |
| tool wear | **9,327** | 9,271 | 8,895 | 9,309 |
| overload | −4,205 | 5,411 | 6,297 | **6,697** |
| air-pressure drop | **7,008** | 6,991 | 6,875 | 6,918 |
| **all** | 489 | 7,271 | 7,422 | **7,689** |
| trips per shift | 0.167 | 0 | 0 | 0 |
| inspections per shift | 0 | 0 | 0.833 | 0.007 |
| maintenance stops per shift | 0 | 0.167 | 0.535 | 0.167 |

RL is best overall: +5.7 % over today's rules and +3.6 % over rules + inspect. Its gains come from
over-temperature and overload; it is about equal on no fault, tool wear and the bearing fault, and slightly behind simply
continuing on the air-pressure drop (where it correctly does not waste maintenance on the VMC). **It learned to almost never
inspect**: in this simulator a fault shows in the sensors within one or two decisions, so a ₹400 inspection rarely tells it
something it would not see anyway. Rules + inspect inspects 0.83 times per shift and gains on overload and coolant loss by
maintaining earlier, but pays for the inspections when nothing is wrong. Two training runs of the same setup differed by about
₹120 on the overall figure, so differences of that size between policies are within run-to-run variation.

## Live feedback loop

`FeedbackAgent` (in `ml/maintenance_rl.py`) watches every broadcast batch. For each CNC mill, every 20 s it:

1. **Closes the previous decision**: works out the action actually taken (the mean derate, *maintain* if a resolution
   happened, or *inspect* if an inspection work order was created for the machine; a simulated inspection reports the
   simulator's own fault state), the outcome (parts, seconds over temperature, zone C/D, overload, worn tool, trip), the reward,
   and whether the twin's predicted risk was right (adverse outcome expected vs. happened).
2. **Updates the Q-table** for that state and action (learning rate 0.1), in **SIM mode only**: REPLAY and LIVE data are
   evaluated but never learned from.
3. **Opens a new decision** with the current state, the twin's predicted risk and the policy's recommendation.

Each evaluated decision is stored in SQLite (`rl_decisions`) and exported as the *RL decisions* sheet. Online updates
live in memory and start again from `models/rl_policy.json` when the server restarts.

## Never actuates

The recommendation is advice. *Send for operator approval* calls `POST /api/rl/{machine_id}/propose`, which creates a
pending proposal (derate / clear derate / an inspection or maintenance work order). It is executed only by
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
