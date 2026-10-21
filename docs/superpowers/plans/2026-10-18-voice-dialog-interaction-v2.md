# Voice Dialog Interaction Implementation Plan (v2 - Corrected)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add voice-based task negotiation to P2 AI companion, allowing real-time division of labor through natural language conversation.

**Architecture:** Dialog worker runs in separate daemon thread, communicates with main 20 Hz loop via command/ack queues. Preference weights (role/claim/assign/direct) are additive scoring terms within existing layer-based planning, not switches. LLM does "understand + phrase + choose from limited actions", not planning or ETA calculation.

**Tech Stack:** 
- Audio: sounddevice + numpy (VAD, resampling)
- Network: websockets (ASR/TTS), requests (LLM streaming)
- Cloud: Volcano bigmodel_async ASR/TTS, Volcano Ark LLM (OpenAI-compatible)
- Integration: command/ack queues, 2 Hz context publishing

**Spec:** `docs/superpowers/specs/2026-10-18-voice-dialog-interaction-design.md` (§4-§8 are binding authority)

**Critical:** This is v2 rewrite. v1 plan had structural defects (wrong Preference/DialogCommand fields, wrong TASK_FAMILIES logic, wrong rescue gate formula, wrong API endpoints). All structures below transcribed from spec §4.3-4.5.

## Global Constraints

- Python 2/3 compatible type annotations (`# type: (...)` comment form)
- Mac native麦克风 + 扬声器, no external headset required
- TTS plays during VAD pause to prevent echo
- Main loop remains 20 Hz, dialog worker independent daemon thread
- All network I/O in worker thread, main loop non-blocking
- End-to-end latency target P95 <2.5s (VAD 400ms + ASR 300ms + LLM 1.2s + TTS 300ms)
- Preference weights (spec §4.5): role ±1.2, assign -4, claim -8, direct +4
- Layer ordering unchanged (L0 fire > L1 irreversible > L2 rescue > L3 redeem > L4 unblock > L6 prep > L7 cleanup)
- LLM replies in 8–20 Chinese characters, uses conditional phrasing when uncertain
- Branch names are ingredient names (SushiRice, Cucumber, SushiFish, Seaweed) — spec §4.5
- Command TTLs (spec §4.5): claim 12s (bound) / 30s (branch), assign 8–20s, direct 30s, role until level end
- Evidence renewal checks every 10 ticks (500ms), triggers when TTL ≤6s remaining — spec §4.7
- Rescue gate margin (spec §4.6): `max(3s, min(0.2 × D, 10s))` where D is ETA to delivery
- All keys/tokens from environment variables (spec §6.5), never committed to repo
- Env vars: `VOLC_ASR_API_KEY`, `VOLC_ASR_RESOURCE_ID`, `VOLC_TTS_API_KEY`, `VOLC_TTS_RESOURCE_ID`, `VOLC_TTS_VOICE`, `ARK_API_KEY`, `ARK_MODEL`, `ARK_BASE_URL`

## Review Focus

1. **VAD false positive from game SFX** - RMS threshold 9 dB above adaptive noise floor should reject transient game sounds; if SFX triggers ASR, test shows spurious transcript and check threshold tuning.
2. **TTS echo loop** - P2's voice should never trigger ASR as player input; test verifies VAD paused during TTS playback and `addressed=false` for P2's own utterances in transcript.
3. **Stale preference after player abandons task** - Player claims "I'll chop" then walks away for 12s; test verifies claim expires when TTL=0 without evidence renewal, P2 resumes chopping.
4. **Rescue gate opens too early** - For D=60s task, margin=10s (capped), gate should open when `0 <= latest_start < margin`; test verifies `latest_start = slack - D`.
5. **Multi-command partial rejection consistency** - Player says "don't cook, wash dishes, I'll chop"; if wash is rejected (no sink), test verifies either reply suppressed or correction template played, not original "I'll wash"播报.

---

## File Structure

### New Directory: `ai/dialog/`

Core dialog layer modules (spec §6.1):

- **`__init__.py`** - Package marker, exports `DialogWorker` and `Preference`
- **`schema.py`** - Data classes: `Preference` (spec §4.5), `DialogCommand` (spec §4.3), `DialogContext` (spec §4.2), `DialogState`; JSON parsing with fallback
- **`audio.py`** - sounddevice capture/playback; device enumeration; RMS VAD with adaptive noise floor; audio framing (50ms chunks for VAD)
- **`asr.py`** - Volcano bigmodel_async streaming ASR client (WebSocket); handles `definite=true` finalization; reconnection on network failure
- **`tts.py`** - Volcano streaming TTS client (WebSocket); playback queue; interruption on player speech; volume control (default 0.7)
- **`llm.py`** - Volcano Ark chat client (SSE stream); prompt template (spec §7.3.1); JSON parsing with fence stripping; timeout handling (3s)
- **`policy.py`** - Trigger logic for 4 LLM entry points (`speech`/`new_order`/`rescue`/`help` per spec §5.1); frequency limiting (4s interval for broadcasts, not player speech); emergency templates; `assign_expired` low-priority templates
- **`worker.py`** - Background daemon thread: VAD→ASR→LLM→TTS pipeline; command/ack queues; context subscription (2 Hz); graceful shutdown

### Modified Files

- **`ai/state/blackboard.py`** - Add `preferences: List[Preference]`, `p2_suppressed_families: Dict[str, int]`, `abandoned_orders: Dict[Any, int]`, `dialog: DialogState`, `rescue_cooldown_until: int`
- **`ai/core/planner.py`** - Add `Candidate.branch` field (str, ingredient name from RecipeBook); augment `_score_candidate()` with `pref` term (sum matching preferences per spec §4.5); rescue gate logic (spec §4.6) with `latest_start = slack - D`; commitment clearing (spec §4.5: clear `active_task`, not `commitment = None`)
- **`ai/query/recipes.py`** - Add `RecipeBook.ingredient_branch(item: str) -> Optional[str]` to map cooked/chopped items back to raw ingredient name using existing `cooked_of_raw` / `chop_target` reverse lookups
- **`ai/agent.py`** - Import `DialogWorker`; instantiate in `__init__`; drain command queue in `tick()` (steps 1–2 per spec §6.3); publish context every 10 ticks (step 3); trace dialog events (step 4)
- **`run_agent.py`** - Add CLI flags: `--voice`, `--no-voice`, `--voice-debug`, `--voice-log PATH`; read env vars for keys; instantiate `DialogWorker` if enabled

### New Test Files

- **`tests/test_dialog_schema.py`** - JSON parsing from LLM output per spec §4.3; `Preference` structure per spec §4.5; addressed=false suppression; ack outcome logic
- **`tests/test_preferences.py`** - Weight matching (role/claim/assign/direct per spec §4.5 table); branch/family/candidate_key scoping; TTL expiry; evidence renewal (spec §4.7); commitment clearing (clear active_task, not whole object)
- **`tests/test_rescue_gate.py`** - Layer fallthrough when all claimed; gate opens at `0 <= latest_start < margin`; `latest_start = slack - D` formula; margin = `max(3, min(0.2×D, 10))` with cap; no override when `latest_start < 0`; L0/L1 ignore claims; cooldown prevents flapping
- **`tests/test_p2_intents.py`** - `direct_p2` stays positive but doesn't cross layers; `cancel_p2` hard suppresses family 8s; `abandon_order` filters all candidates for that order_id with TTL
- **`tests/test_dialog_policy.py`** - 4 LLM entries per spec §5.1; `new_order` merging (1s window + opening batch); 4s interval for broadcasts only (not player speech); same-intent deduplication; emergency templates; `assign_expired` no LLM (only when P1 hands_free + not moving toward target for 4 ticks); TTS interruption
- **`tests/test_dialog_integration.py`** - Real frame fixtures (.ndjson.gz format): player claims delivery → P2 drops to lower layer; player claims rice branch → entire rice chain released; pending wash when no dirty plate → pending ack, executes when plate appears; ack `rejected` suppresses reply; rescue gate override with explanation

---

## Task 1: Dialog Schema and Data Types (from spec §4.3, §4.5)

**Files:**
- Create: `ai/dialog/__init__.py`
- Create: `ai/dialog/schema.py`
- Test: `tests/test_dialog_schema.py`

**Interfaces:**
- Consumes: (none, foundational)
- Produces:
  - `Preference(kind: str, branch: Optional[str], family: Optional[str], candidate_key: Optional[Any], owner: str, weight: float, expires_tick: int)` per spec §4.5
  - `DialogCommand` with fields from spec §4.3: `{type, branch, family, candidate_key, ttl_s}`
  - `DialogContext` with fields from spec §4.2: `{level, p2, preferences, p1, orders, candidates, dialog, event}`
  - `DialogState(pending_commands, last_speech_tick, last_order_tick, last_rescue_tick, last_help_tick, pending_acks)`
  - Action families: `chop` / `cook` / `wash` / `deliver` (spec §4.5) — NO ingredient names in families

**Steps:**

- [ ] Step 1: Write test for `Preference.from_dict()` parsing per spec §4.5 structure  
Test: `test_preference_from_dict_claim()` verifies kind="claim", branch="Cucumber", family="chop", owner="P1", weight=-8.0, candidate_key=None; `test_preference_invalid_kind_rejected()` expects ValueError for kind not in {role, claim, assign, direct}

- [ ] Step 2: Run test to verify it fails  
Run: `pytest tests/test_dialog_schema.py::test_preference_from_dict_claim -xvs`  
Expected: FAIL with `ModuleNotFoundError: No module named 'ai.dialog'`

- [ ] Step 3: Implement `ai/dialog/schema.py` with `Preference`, `DialogCommand`, `DialogContext`, `DialogState` dataclasses per spec  
`Preference` per spec §4.5 table: kind in {role, claim, assign, direct}, branch is Optional[str] ingredient name, family in {chop, cook, wash, deliver, None}, candidate_key for specific binding; `DialogCommand` per spec §4.3: type field (not command_type), branch/family/candidate_key/ttl_s; validate kind/family at parse time

- [ ] Step 4: Run tests to verify they pass  
Run: `pytest tests/test_dialog_schema.py -xvs`  
Expected: PASS (all schema parsing tests)

- [ ] Step 5: Write test for `DialogCommand.from_dict()` per spec §4.3 LLM output format  
Test: `test_command_from_llm_json()` with `{"type": "claim_task", "branch": "Cucumber", "family": "chop", "ttl_s": 30}` expects type="claim_task", branch="Cucumber", family="chop", candidate_key=None, ttl_s=30

- [ ] Step 6: Implement `DialogCommand.from_dict()` parsing LLM JSON  
Logic: read type/branch/family/candidate_key/ttl_s from dict, validate type in {claim_task, set_role, direct_p2, cancel_p2, abandon_order, noop}

- [ ] Step 7: Run command parsing tests  
Run: `pytest tests/test_dialog_schema.py::test_command_from_llm_json -xvs`  
Expected: PASS

- [ ] Step 8: Write test for `addressed=false` in LLM JSON suppressing output  
Test: `test_addressed_false_no_commands()` with `{"addressed": false, "reply": "", "commands": []}` expects empty commands list

- [ ] Step 9: Run test  
Run: `pytest tests/test_dialog_schema.py::test_addressed_false_no_commands -xvs`  
Expected: PASS

- [ ] Step 10: Write `ai/dialog/__init__.py` exports  
`from ai.dialog.schema import Preference, DialogCommand, DialogContext, DialogState`

- [ ] Step 11: Commit  
`git add ai/dialog/ tests/test_dialog_schema.py && git commit -m "feat(dialog): add schema per spec §4.3-4.5

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`

---

## Task 2: Blackboard Extensions for Dialog State

**Files:**
- Modify: `ai/state/blackboard.py`
- Test: `tests/test_blackboard.py`

**Interfaces:**
- Consumes: `Preference`, `DialogState` from Task 1
- Produces: extended `Blackboard` with `preferences: List[Preference]`, `p2_suppressed_families: Dict[str, int]`, `abandoned_orders: Dict[Any, int]`, `dialog: Optional[DialogState]`, `rescue_cooldown_until: int`

**Steps:**

- [ ] Step 1: Write test for preference storage in `tests/test_blackboard.py`  
Test: `test_blackboard_preferences_init()` verifies `bb.preferences == []`; `test_preferences_ttl_expiry()` advances tick, checks preferences with `expires_tick <= current_tick` removed

- [ ] Step 2: Run test  
Run: `pytest tests/test_blackboard.py::test_blackboard_preferences_init -xvs`  
Expected: FAIL with AttributeError

- [ ] Step 3: Add fields to `Blackboard` dataclass in `ai/state/blackboard.py`  
Add: `preferences: List[Preference] = field(default_factory=list)`, `p2_suppressed_families: Dict[str, int] = field(default_factory=dict)`, `abandoned_orders: Dict[Any, int] = field(default_factory=dict)`, `dialog: Optional[DialogState] = None`, `rescue_cooldown_until: int = 0`

- [ ] Step 4: Run test  
Run: `pytest tests/test_blackboard.py::test_blackboard_preferences_init -xvs`  
Expected: PASS

- [ ] Step 5: Write test for preference expiry pruning  
Test: `test_prune_expired_preferences()` creates bb with preferences at tick 100, expires_tick in [80, 120], current_tick=110, expects only expires_tick>110 remain

- [ ] Step 6: Add expiry pruning helper to blackboard.py  
Function: `prune_expired_preferences(bb, current_tick)` filters `bb.preferences = [p for p in bb.preferences if p.expires_tick > current_tick]`

- [ ] Step 7: Run expiry test  
Run: `pytest tests/test_blackboard.py::test_prune_expired_preferences -xvs`  
Expected: PASS

- [ ] Step 8: Commit  
`git add ai/state/blackboard.py tests/test_blackboard.py && git commit -m "feat(dialog): extend blackboard for preference storage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`

---

