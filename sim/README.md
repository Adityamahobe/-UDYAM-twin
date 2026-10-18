# sim/ (R1)

Plant simulators for each machine template (the hidden "true" machine with its own parameters), the virtual PLC (IDLE → RUN → DERATE → TRIP), fault injectors and the line model (buffers, starved/blocked).

Must emit `observed` and `line` messages per `docs/schema.json`. See MASTER_PLAN §4.3 and §3.3. `tools/fake_factory.py` shows the expected behaviour.
