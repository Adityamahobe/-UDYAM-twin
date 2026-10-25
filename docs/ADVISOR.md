# UDYAM-TWIN Advisor (decision-support chatbot)

A chat panel on the dashboard ("💬 Ask the Twin", bottom right) that answers operator questions
such as *"VMC-01 is heating up — can I finish a batch of 40 parts?"* with numbers from the live twin.
The language model is **Google Gemini**, called from the server through the official **Google Gen AI
Python SDK** (`google-genai`) with Gemini function calling.

## How it works

```
Browser chat ─► POST /api/chat ─► api/advisor.py ─► Gemini (function calling), or a local Ollama model
                                       │            automatic function calling is OFF: the server runs every tool
                                       ├─ get_line_status       live readings, vibration zone, OEE, ₹ loss
                                       ├─ get_machine_state     readings, twin residual, calibration, forecast,
                                       │                        AI4I risk, tool RUL + UDYAM-SIM fault diagnosis
                                       ├─ get_recent_events     alerts, calibration, common cause
                                       ├─ query_dataset         UDYAM-SIM, AI4I 2020 and CWRU statistics
                                       ├─ simulate_what_if      twin thermal physics run forward under derate options
                                       ├─ get_economics         editable shop assumptions
                                       ├─ propose_action        derate / work order — stored as a proposal, NOT applied
                                       └─ submit_answer         the structured finish: Measured / Inferred / Suggested

Approve button ─► POST /api/chat/proposals/{id}/approve ─► operator check ─► applied once, in the simulator only
```

One turn: the server sends the operator's message, the system rules and the function declarations to
Gemini; for each function call Gemini returns, the server validates the arguments, runs the tool, and
sends all the results back in one turn; this repeats (at most `UDYAM_CHAT_MAX_TOOL_ROUNDS`) until Gemini
calls `submit_answer`. Gemini's own content is sent back unchanged, so its thought signatures are kept.

Guardrails, built into the code (not only the prompt):

- **Every number comes from a tool.** The model explains; the twin computes. The system rules forbid
  inventing readings, statistics, predictions, costs or remaining life, and require *Measured /
  Inferred / Suggested* sections with simulation results and assumptions labelled.
- **Strict tool dispatcher.** Every call (Gemini's, Ollama's or offline) goes through a fixed tool table
  with strict argument models: unknown tools, unknown fields, out-of-range values and unknown machine IDs
  come back to the model as tool errors; nothing the model writes is executed.
- **Evidence and approval decided by the server.** `evidence` lists the tool calls the server actually ran
  (with their results); `requires_approval` comes from proposals and `machine_control` suggestions.
- **Number check.** `checks.unverified_numbers` lists any number in the reply that matches no tool result.
- **Propose, never actuate.** `propose_action` only stores a proposal (with an ID, valid 15 minutes). It is
  executed only by `POST /api/chat/proposals/{id}/approve`, a separate operator workflow:
  - if `UDYAM_OPERATOR_TOKEN` is set, the request must carry it in `X-Operator-Token` (the panel asks for it);
    if it is not set, only a browser on the server machine itself (localhost) can approve;
  - cross-origin requests are refused, and a proposal can be approved or dismissed once;
  - a derate is applied to the **simulated plant**, a work order is added to the work-order log. UDYAM-Twin
    sends no command to any physical machine.
- **No peeking at the answer.** The injected fault (`active_fault`, `active_scenario`) is stripped from
  everything the advisor sees, as on a real machine.
- **Never breaks the demo.** No key, no model, no SDK, no internet or an API error → the same tools answer in
  offline mode (rule-based templates), and `fallback_reason` says why (e.g. `provider_rate_limited`). The
  badge shows *Gemini*, *Local model* or *Offline*. Set `UDYAM_CHAT_OFFLINE_FALLBACK=0` to get HTTP errors instead.
- **The key stays on the server.** It is read from the environment (`.env` is git-ignored), passed only to
  the SDK client, and never returned by `/api/chat/status`, `/api/chat` or any error message.

## Turn on Gemini

1. Open **aistudio.google.com/apikey** with your Google account → **Create API key** → copy it.
2. In the project folder:

   ```bash
   cp .env.example .env              # git-ignored; run.sh loads it into the server process only
   # edit .env: GEMINI_API_KEY=<your key>
   python -m tools.gemini_models     # lists the model IDs your key can use
   # edit .env: GEMINI_MODEL=<one of those IDs>     (required: no model is hard-coded)
   # optional: UDYAM_OPERATOR_TOKEN=<a PIN>          (needed to approve from a phone / another computer)
   ./run.sh                          # installs google-genai if missing
   ```

3. Open the dashboard → **💬 Ask the Twin**. The badge should read **Gemini**; if it reads *Offline*,
   hover it or open `GET /api/chat/status` to see what is missing.

Things to plan around:
- **Quota:** each question uses 2–4 model requests (tool rounds). If your quota or rate limit runs out,
  the chat falls back to offline mode and says so (`provider_rate_limited`, with `retry_after_s` when Gemini
  sends one). Rehearse in offline mode (`UDYAM_LLM_PROVIDER=offline`) to save quota.
- **Data use:** on Google's free tier, prompts may be used to improve Google's products; the demo only sends
  simulated data.
- **Internet:** Gemini needs it. Carry a hotspot, or use Ollama below.

## Optional: a local model with Ollama (no internet)

```bash
brew install ollama            # or the installer from ollama.com
ollama serve &                 # keep it running
ollama pull qwen2.5:7b         # ~4.7 GB; qwen2.5:3b on an 8 GB Mac
# in .env: OLLAMA_MODEL=qwen2.5:7b      (and UDYAM_LLM_PROVIDER=ollama to prefer it over Gemini)
./run.sh
```

The badge reads **Local model**. Nothing leaves the laptop; answers are less polished than Gemini's.

## API

`POST /api/chat` `{message, machine_id?, conversation_id?}` → `{reply, conversation_id, mode (gemini | ollama | offline),
telemetry_mode (SIM | REPLAY | LIVE), measured[], inferred[], unavailable[], evidence[{tool, input, ok, kind, result}],
suggested_actions[{action, category, machine_id, rationale, requires_operator_approval, executed: false, proposal?}],
requires_approval, proposals[{proposal_id, action, machine_id, derate_pct?, title?, reason, status}],
checks{unverified_numbers}, fallback_reason, notice, model, tool_rounds, latency_s}`.

Send back the returned `conversation_id` to continue; the server keeps the history in memory (200
conversations at most, 20 messages each, cleared on restart).

| Situation | Result |
|---|---|
| empty or > 4000-character message, malformed `conversation_id` | 422 |
| unknown `machine_id` | 404 `unknown_machine` (with the valid IDs) |
| unknown or expired `conversation_id` | 404 `unknown_conversation` |
| same conversation still answering, or 20 messages reached | 409 `conversation_busy` / `conversation_too_long` |
| no key / no model / no SDK | offline answer with `fallback_reason.error = chat_not_configured` (503 if fallback is off) |
| Gemini rate limit or quota (429 / RESOURCE_EXHAUSTED) | `provider_rate_limited` (+ `retry_after_s`); 429 with fallback off |
| key rejected (401 / 403 / "API key not valid") | `provider_auth_failed`; 502 |
| model not available to the key (404) | `model_unavailable`; 502 |
| timeout / DEADLINE_EXCEEDED | `provider_timeout`; 504 |
| network failure | `provider_unreachable`; 503 |
| other 4xx / 5xx | `provider_bad_request` / `provider_error`; 502 |
| no candidates, empty answer, malformed function call, cut off (MAX_TOKENS), too many tool rounds | `malformed_provider_response`, `provider_truncated`, `tool_round_limit`; 502 |
| prompt or answer blocked by Gemini's safety filters | 200, `mode: gemini`, reply says it declined |

The SDK retries transient errors itself (`GEMINI_MAX_RETRIES`, default 2) before any of the above.

`POST /api/chat/proposals/{id}/approve` and `/dismiss` (body `{operator?}`, header `X-Operator-Token` when
required): 200 `{status: executed | dismissed, result}`; 401 `operator_token_required`; 403
`approval_requires_local_operator` or `cross_origin`; 404 `unknown_proposal`; 409 `proposal_already_decided`;
410 `proposal_expired`.

Tools: `get_line_status`, `get_machine_state(machine_id)`, `get_recent_events(n, machine_id)`,
`query_dataset(fault)` (UDYAM-SIM `overview` / fault names, AI4I 2020 `TWF`…`RNF`, CWRU `inner_race`…),
`simulate_what_if(machine_id, action, minutes, parts_target)` (action `compare`, `continue`, `derate_10`…`derate_50`,
`stop`; CNC thermal only, other machines report the limitation), `get_economics`, `propose_action`, `submit_answer`.

Settings: `GEMINI_API_KEY`, `GEMINI_MODEL` (both required for Gemini), `GEMINI_TIMEOUT_S` (60),
`GEMINI_MAX_RETRIES` (2), `GEMINI_MAX_OUTPUT_TOKENS` (8192), `UDYAM_LLM_PROVIDER` (`auto`), `OLLAMA_MODEL`,
`OLLAMA_BASE_URL`, `UDYAM_LLM_TIMEOUT_S`, `UDYAM_CHAT_OFFLINE_FALLBACK` (1), `UDYAM_CHAT_MAX_TOOL_ROUNDS` (6),
`UDYAM_OPERATOR_TOKEN`.

## Demo questions that work well

| Ask | What happens |
|---|---|
| "What's happening on the line?" | Line status, OEE, residual states |
| (after **Over-temperature (coolant loss)** scenario) "VMC-01 is heating up. Can I finish 40 parts?" | Diagnosis = coolant (UDYAM-SIM model), what-if 0/20/40 % derate, proposes 20 % → click **Approve** |
| "Why did the twin recalibrate?" | Residual drift + `h_cool` drop explained |
| "How often does coolant loss hit the limit in your sim data?" | UDYAM-SIM stats: with vs without auto-derate |
| "VMC-01 garam ho raha hai, kya karu?" | Same, answered in Hinglish |
| (bearing scenario) "Should I just slow it down?" | Zone C/D caveat: derating does not fix a bearing → work order |

## Files

- `api/advisor.py` — tools, strict dispatcher, system rules, Gemini function-calling loop, Ollama loop,
  conversations, proposals, offline fallback
- `api/app.py` — `POST /api/chat`, `GET /api/chat/status`, the proposal approve/dismiss endpoints, `GET /api/sim-metrics`
- `web/chat.js`, `web/app.css` (chat section) — the panel
- `tools/gemini_models.py` — lists the Gemini model IDs your key can use
- `tests/test_advisor_chat.py` — Gemini loop, response contract, dispatcher, validation, every error path,
  approval workflow (Gemini mocked; no network)
- `tests/test_advisor_free_providers.py` — provider selection, Ollama loop
- `tests/test_sim_dataset_and_advisor.py` — offline answers, no-actuation, UDYAM-SIM model
