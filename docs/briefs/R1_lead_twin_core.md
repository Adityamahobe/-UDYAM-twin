# R1: Lead + Twin Core

**You own the differentiator.** If calibration doesn't work, the project is just another dashboard.

**Also read:** MASTER_PLAN §0 (twin builder), §3.3 (line), §4.3 (hybrid core), §5 (sprint, kill rule)

## Deliverables
| Due | Output |
|---|---|
| Sun 18 | Schema (§4.2) frozen and committed as `docs/schema.json`; repo scaffold; a **fake stream** script so R2/R3/R4 can start today |
| Mon 19 | `sim/plant.py` with 3 templates (cutting, rotating CNC, compressor), seeded; machine registry; 4 instances ticking at 500 ms |
| Tue 20 | `twin/model.py` running alongside each plant; residuals (EWMA, CUSUM, z-spike); `sim/line.py` with buffers and starved/blocked states; fault injectors; PLC states |
| Wed 21 | `twin/calibrate.py`: bounded RLS on `h_cool`, `k_heat`, `k_wear`; versioned params + rollback; **freeze on spike / dq_flag** |
| Thu 22 | `twin/forecast.py` (minutes to warning); common-cause rule for the compressor; adding a machine at runtime (for the wizard) |
| Fri 23 | Tests: calibration converges for a gradual fault and freezes for an abrupt fault or stuck sensor; replay is deterministic. **Feature freeze.** |

## Done when
Gradual coolant fault → residual drifts → θ updates → curves re-converge within ~60 s of demo time. Bearing spike or stuck sensor → θ doesn't move.

## Lead duties
Merge `main` nightly (it must always run the demo) · run the stand-up · make scope cuts using the kill rule · drive the demo on stage.
