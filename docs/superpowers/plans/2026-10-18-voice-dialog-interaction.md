# Voice Dialog Interaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a voice-based division-of-labor layer to P2: the player speaks, P2 understands, adjusts preference weights on existing planner candidates, and answers in one short sentence.

**Architecture:** A daemon `DialogWorker` owns VAD, Volcengine ASR/TTS, and the Ark LLM. The 20 Hz main loop stays the only writer of `World`/`Blackboard`: it publishes a 2 Hz context snapshot, drains dialog commands, applies them as `Preference` weights, and returns an ack. Preferences are additive scoring terms inside the existing layer-based planner; the LLM never plans, computes ETA, or touches the control loop.

**Tech Stack:** Python 3.9, `sounddevice` + `numpy` (audio/VAD), `websockets` (Volcengine WSS), `requests` (Ark SSE), pytest 7.4.4, existing `Quartz`/PyObjC controller.

**Spec:** `docs/superpowers/specs/2026-10-18-voice-dialog-interaction-design.md`

## Global Constraints

- Python 3.9; type annotations use `# type: (...) -> ...` comments, matching the existing code.
- Main loop is 20 Hz (`TICK_INTERVAL = 0.05`); plugin frames are 5 Hz. All `*_ticks` values are 20 Hz ticks.
- `DialogWorker` runs in its own daemon thread and must not read `World` or write `Blackboard`.
- No blocking network/audio I/O in the main loop; all of it stays in the worker.
- LLM output is one `reply` + a bounded command list; no multi-turn LLM loop in v1.
- Preference weights: `role ±1.2`, `assign -4.0`, `claim -8.0`, `direct +4.0`.
- TTL: bound claim 240 ticks, branch/type claim 600, `assign_p1` 160–400, `direct_p2` 600, `abandon_order` 1200.
- Rescue gate uses `cushion = ETA.solo_slack` (remaining − solo ETA), not `remaining`; `margin = max(100, min(int(0.3*D), 200))` ticks.
- L0/L1/L2 and `RescueCooked`/`ReturnPot`/`RecoverLoosePot` ignore negative claim/assign preferences.
- Branch names are raw ingredient names: `SushiRice`, `Cucumber`, `SushiFish`, `Seaweed`.
- v1 audio is Mac built-in mic + speakers, half-duplex: VAD paused while TTS plays; no barge-in.
- All secrets come from environment variables; never commit keys.
- `pref` must be registered in every relevant `TERM_WHITELIST` layer or `filter_terms` silently drops it.

## Review Focus

1. **Continuous game audio looks like speech** — built-in speakers feed game SFX into the mic; the VAD must not treat sustained SFX as an utterance. Test: a 3 s loud sine/noise burst at the game-volume level does not open a speech segment when it stays below `noise_floor + 9 dB` or is filtered by the pause/hold logic.
2. **`solo_slack` is a cushion, not remaining time** — a builder who subtracts `D` again gets a gate that never opens. Test: remaining 21 s, solo ETA 17 s, `cushion=4 s`, margin 5.1 s → gate opens; cushion 9 s → closed; cushion −8 s → closed.
3. **`FetchToPlate` / `Prefetch` are part of chopping** — a claim on “我来切黄瓜” must also stop P2 from carrying cucumber to a plate. Test: chop-family preference with `branch=Cucumber` matches `Chop`, `FetchToBoard`, `FetchToPlate`, `Prefetch`.
4. **Multi-command partial rejection** — “别做饭，洗碗，我来切” may accept two commands and reject one. Test: valid commands apply, the rejected one does not, and the reply is suppressed/short-corrected instead of promising the rejected action.
5. **Reply is spoken before the command is applied** — the worker must wait for the main-loop ack, then speak; a `rejected` ack suppresses the reply. Test: a rejected command produces no TTS unless the correction template fires.

---

## File Structure

### New package: `ai/dialog/`

- `__init__.py` — exports `Preference`, `DialogCommand`, `LLMTurn` from `schema` only. Do **not** import `worker` here: `ai/state/blackboard.py` imports `ai.dialog.schema`, and importing the package would otherwise execute a `worker → blackboard` cycle. Consumers import `DialogWorker` from `ai.dialog.worker` directly.
- `schema.py` — dataclasses, command parsing, family/branch constants, `task_key_str`, `preference_matches_task`. Imports only stdlib; no `ai.*` imports (avoids `state ↔ dialog` cycles).
- `audio.py` — `AudioCapture` (sounddevice input), `VAD` (RMS + adaptive floor + pause), `AudioPlayer` (output stream + PCM queue), device enumeration.
- `asr.py` — `ASRClient` for Volcengine bigmodel streaming ASR; `FakeASR` lives in tests.
- `tts.py` — `TTSClient` for Volcengine streaming TTS; playback uses `AudioPlayer`.
- `llm.py` — `LLMClient` (Ark SSE), `build_prompt`, `parse_json_response`.
- `policy.py` — four LLM entries, rate limits, new-order merge, emergency/assign-expired templates.
- `worker.py` — `DialogWorker` daemon thread; queues and pipeline.

### Modified files

- `ai/query/recipes.py` — add `RecipeBook.ingredient_branch(item)`.
- `ai/core/planner.py` — `Candidate.branch`, `branch_of_task`, `_preference_score`, `_claim_assign_penalty`, rescue gate, fallthrough, abandoned/suppressed filters, commitment clearing.
- `ai/core/planner_layers.py` — add `"pref"` to `TERM_WHITELIST` for L1–L7.
- `ai/state/blackboard.py` — `preferences`, `p2_suppressed_families`, `abandoned_orders`, `dialog`, `rescue_cooldown_until`.
- `ai/agent.py` — dialog worker lifecycle, `_service_dialog`, command application, event detection, context publishing, level reset, trace.
- `run_agent.py` — `--voice` / `--no-voice` / `--voice-debug` / `--voice-log`, env checks.
- `requirements-voice.txt` — new runtime deps.
- `AGENTS.md`, `docs/agent/architecture.md` — dependency + module documentation.
- `.env.example` — key names only.

### New tests

`test_dialog_schema.py`, `test_recipes.py` (add cases), `test_preferences.py`, `test_rescue_gate.py`, `test_p2_intents.py`, `test_audio_vad.py`, `test_asr_tts.py`, `test_llm_client.py`, `test_dialog_policy.py`, `test_dialog_worker.py`, `test_dialog_integration.py`.

---

## Task 1: Dialog Schema and Dependencies

**Files:**
- Create: `ai/dialog/__init__.py`
- Create: `ai/dialog/schema.py`
- Create: `requirements-voice.txt`
- Test: `tests/test_dialog_schema.py`

**Interfaces:**
- Produces:
  - `Preference(kind, branch=None, family=None, task_type=None, candidate_key=None, owner="P1", weight=0.0, expires_tick=0, base_ttl=0)`
  - `DialogCommand(type, branch=None, family=None, task_type=None, candidate_key=None, order_id=None, owner=None, ttl_s=None, command_id="")`
  - `LLMTurn(addressed, reply, commands, confidence)`
  - `DialogState(last_speech_tick=0, last_order_tick=0, last_rescue_tick=0, last_help_tick=0, recent_turns=None, pending_reply=None, event_seq=0)`
  - `VoiceConfig(asr_key, asr_resource_id, tts_key, tts_voice, tts_resource_id, ark_key, ark_base_url, ark_model, name="小厨", rate=1.05, volume=0.7, log_path=None, debug=False)`
  - `FAMILY_OF_TYPE: Dict[str, str]`, `RESCUE_TYPES: set`, `NO_BRANCH_TYPES: set`
  - `task_key_str(task) -> str`, `preference_matches_task(pref, task, branch) -> bool`, `parse_llm_turn(text) -> LLMTurn`

- [ ] **Step 1: Write failing schema tests**

```python
def test_preference_defaults_and_ttl():
    p = Preference(kind="claim", branch="SushiRice", family="cook",
                   weight=-8.0, expires_tick=600, base_ttl=600)
    assert p.kind == "claim" and p.branch == "SushiRice"
    assert p.expires_tick == 600 and p.base_ttl == 600

def test_parse_claim_command():
    turn = parse_llm_turn('{"addressed": true, "reply": "行，饭你来。", '
                          '"commands": [{"type": "claim_task", '
                          '"branch": "SushiRice", "family": "cook"}], '
                          '"confidence": 0.9}')
    assert turn.addressed is True and turn.reply == "行，饭你来。"
    assert turn.commands[0].type == "claim_task"
    assert turn.commands[0].branch == "SushiRice"

def test_parse_addressed_false_has_no_commands():
    turn = parse_llm_turn('{"addressed": false, "reply": null, '
                          '"commands": [], "confidence": 0.2}')
    assert turn.addressed is False and turn.reply is None
    assert turn.commands == []

def test_invalid_command_type_raises():
    with pytest.raises(ValueError):
        parse_llm_turn('{"addressed": true, "reply": "x", '
                       '"commands": [{"type": "teleport"}], '
                       '"confidence": 0.5}')
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_dialog_schema.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai.dialog'`.

- [ ] **Step 3: Implement `schema.py` and `__init__.py`**

Use `@dataclass`; `parse_llm_turn` strips markdown fences, uses `json.loads`, rejects unknown command types, and maps each command dict to `DialogCommand`. Keep all imports stdlib-only. `FAMILY_OF_TYPE` maps:

```python
chop: Chop, FetchToBoard, FetchToPlate, Prefetch
cook: FetchToCooker, FillCooker, TakeCookedToPlate, Prefetch
wash: WashPlate, PlaceCleanPlate
deliver: DeliverPlate
RESCUE_TYPES = {"RescueCooked", "ReturnPot", "RecoverLoosePot"}
NO_BRANCH_TYPES = {"WashPlate", "PlaceCleanPlate", "DeliverPlate",
                   "Discard", "ResetPlate", "StashHeld", "Extinguish"}
```

`preference_matches_task` returns `False` when all of `candidate_key/task_type/branch/family` are empty; otherwise it requires every non-empty field to match.

- [ ] **Step 4: Add `requirements-voice.txt`**

```text
sounddevice>=0.4.6
numpy>=1.21
websockets>=12.0
```

- [ ] **Step 5: Run tests, expect pass**

Run: `python3 -m pytest tests/test_dialog_schema.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ai/dialog/__init__.py ai/dialog/schema.py requirements-voice.txt tests/test_dialog_schema.py
git commit -m "feat(dialog): add schema, command parser, and voice dependencies"
```

---

## Task 2: Recipe Branch Mapping

**Files:**
- Modify: `ai/query/recipes.py`
- Test: `tests/test_recipes.py`

**Interfaces:**
- Produces: `RecipeBook.ingredient_branch(item: Optional[str]) -> Optional[str]`

- [ ] **Step 1: Write failing tests**

```python
def test_ingredient_branch_maps_cooked_rice(book):
    assert book.ingredient_branch("BoiledSushiRice") == "SushiRice"

def test_ingredient_branch_maps_chopped_cucumber(book):
    assert book.ingredient_branch("ChoppedCucumber") == "Cucumber"

def test_ingredient_branch_keeps_raw_name(book):
    assert book.ingredient_branch("Cucumber") == "Cucumber"

def test_ingredient_branch_unknown_identity(book):
    assert book.ingredient_branch("UnknownItem") == "UnknownItem"
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_recipes.py -q -k ingredient_branch`
Expected: FAIL with `AttributeError: 'RecipeBook' object has no attribute 'ingredient_branch'`.

- [ ] **Step 3: Implement `ingredient_branch`**

Build a lazy reverse map from `_by_name`/`_crates`: each raw maps to itself, `chop_target(raw)` maps to raw, and every name in `cooked_of_raw(raw)` maps to raw. Return `None` for empty input; identity fallback for unknown. Cache the map on first use.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_recipes.py -q -k ingredient_branch`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/query/recipes.py tests/test_recipes.py
git commit -m "feat(dialog): map cooked/chopped items back to raw ingredient branches"
```

---

## Task 3: Candidate Branch and Preference Scoring

**Files:**
- Modify: `ai/core/planner.py` (`Candidate`, `generate_candidates`, `_score_candidate`)
- Modify: `ai/core/planner_layers.py` (`TERM_WHITELIST`)
- Test: `tests/test_preferences.py`

**Interfaces:**
- Produces:
  - `Candidate(task, preconditions, estimated_steps, branch=None)`
  - `branch_of_task(task, book) -> Optional[str]`
  - `_preference_score(candidate, bb, layer) -> float`
  - `_claim_assign_penalty(candidate, bb, layer) -> float`
  - `terms["pref"]` in `_score_candidate`

- [ ] **Step 1: Write failing tests**

```python
def test_role_preference_matches_family(bb, book):
    bb.preferences = [Preference(kind="role", family="chop", owner="P1",
                                 weight=-1.2)]
    chop = _cand(T.Chop(order_id=1, resources=("board_1",), item="Cucumber"), book)
    fish = _cand(T.Chop(order_id=1, resources=("board_1",), item="SushiFish"), book)
    assert _preference_score(chop, bb, layer=6) == -1.2
    assert _preference_score(fish, bb, layer=6) == -1.2

def test_branch_claim_covers_fetch_to_plate(bb, book):
    bb.preferences = [Preference(kind="claim", branch="Cucumber",
                                 family="chop", weight=-8.0)]
    plate = _cand(T.FetchToPlate(order_id=1, resources=("plate_1",),
                                 item="Cucumber"), book)
    assert _preference_score(plate, bb, layer=6) == -8.0

def test_branch_claim_does_not_touch_other_branch(bb, book):
    bb.preferences = [Preference(kind="claim", branch="SushiRice",
                                 family="cook", weight=-8.0)]
    cucumber = _cand(T.FetchToPlate(order_id=1, resources=("plate_1",),
                                    item="Cucumber"), book)
    assert _preference_score(cucumber, bb, layer=6) == 0.0

def test_l2_rescue_ignores_negative_claim(bb, book):
    bb.preferences = [Preference(kind="claim", family="cook", weight=-8.0)]
    rescue = _cand(T.RescueCooked(resources=("cooker_1",)), book)
    assert _preference_score(rescue, bb, layer=2) == 0.0
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_preferences.py -q`
Expected: FAIL (`Candidate` has no `branch`, `_preference_score` missing).

- [ ] **Step 3: Add `Candidate.branch` and `branch_of_task`**

Add `branch: Optional[str] = None` as the last dataclass field. In `generate_candidates`, when wrapping each task from `recipe_graph.emit`, set `branch=branch_of_task(task, book)`. In `_gen_misc`, pass `branch=branch_of_task(...)` as well. `branch_of_task` returns `None` for `NO_BRANCH_TYPES` or missing `item`, else `book.ingredient_branch(item)`.

- [ ] **Step 4: Implement `_preference_score` and `_claim_assign_penalty`**

`_preference_score` sums matching `Preference.weight`; it skips expired prefs, skips all-empty matchers, and skips **negative** weights for `layer in (0, 1, 2)` or `task.TYPE in RESCUE_TYPES` (positive `direct` still applies). `_claim_assign_penalty` sums only negative `kind in ("claim", "assign")` weights using the same matching/immunity rules.

- [ ] **Step 5: Add `terms["pref"]` and whitelist it**

Near the end of `_score_candidate`, set `terms["pref"] = _preference_score(candidate, bb, layer)`. Add `"pref"` to `TERM_WHITELIST` for `L_IRREVERSIBLE`, `L_RESCUE`, `L_REDEEM`, `L_UNBLOCK`, `L_CAPACITY`, `L_PREP`, `L_CLEANUP`; leave `L_FIRE` empty.

- [ ] **Step 6: Run tests, expect pass**

Run: `python3 -m pytest tests/test_preferences.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ai/core/planner.py ai/core/planner_layers.py tests/test_preferences.py
git commit -m "feat(dialog): add branch metadata and preference scoring term"
```

---

## Task 4: Blackboard Fields and Preference Pruning

**Files:**
- Modify: `ai/state/blackboard.py`
- Test: `tests/test_blackboard.py`

**Interfaces:**
- Produces: `Blackboard.preferences`, `.p2_suppressed_families`, `.abandoned_orders`, `.dialog`, `.rescue_cooldown_until`; `prune_expired_preferences(bb, tick) -> int`

- [ ] **Step 1: Write failing tests**

```python
def test_blackboard_dialog_defaults():
    bb = Blackboard()
    assert bb.preferences == []
    assert bb.p2_suppressed_families == {}
    assert bb.abandoned_orders == {}
    assert bb.dialog is not None
    assert bb.rescue_cooldown_until == 0

def test_prune_expired_preferences():
    bb = Blackboard()
    bb.preferences = [
        Preference(kind="claim", expires_tick=80),
        Preference(kind="claim", expires_tick=120),
    ]
    assert prune_expired_preferences(bb, 110) == 1
    assert [p.expires_tick for p in bb.preferences] == [120]
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_blackboard.py -q -k "dialog_defaults or prune_expired"`
Expected: FAIL with missing attributes.

- [ ] **Step 3: Add fields and helper**

Import `Preference, DialogState` from `ai.dialog.schema` (schema imports stdlib only, so no cycle). Add dataclass fields with `field(default_factory=...)`. `prune_expired_preferences` keeps `expires_tick == 0` (no expiry) or `expires_tick > tick`, and returns the removed count.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_blackboard.py -q -k "dialog_defaults or prune_expired"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/state/blackboard.py tests/test_blackboard.py
git commit -m "feat(dialog): extend blackboard with preferences and dialog state"
```

---

## Task 5: Planner Integration — Abandoned Orders, Suppressed Families, Commitment

**Files:**
- Modify: `ai/core/planner.py` (`generate_candidates`, `_feasibility_of`, `plan`)
- Test: `tests/test_preferences.py`, `tests/test_p2_intents.py`

**Interfaces:**
- Consumes: `bb.abandoned_orders`, `bb.p2_suppressed_families`, `bb.preferences`, `bb.commitment`
- Produces: order-level exclusion, `"p2_suppressed"` veto, `_clear_claimed_commitment(bb)`

- [ ] **Step 1: Write failing tests**

```python
def test_abandoned_order_is_not_generated(bb, world, book):
    bb.abandoned_orders = {2: bb.tick + 1200}
    cands = generate_candidates(world, bb, None, book)
    assert all(getattr(c.task, "order_id", None) != 2 for c in cands)

def test_suppressed_family_is_vetoed(bb, world, book):
    bb.p2_suppressed_families = {"cook": bb.tick + 160}
    cam = next(c for c in generate_candidates(world, bb, None, book)
               if c.task.TYPE == "FetchToCooker")
    ok, veto = _feasibility_of(cam, _ctx(world, bb), bb)
    assert (ok, veto) == (False, "p2_suppressed")

def test_claim_clears_matching_commitment(bb, book):
    bb.commitment.active_task = T.Chop(order_id=1, resources=("board_1",),
                                       item="Cucumber")
    bb.preferences = [Preference(kind="claim", branch="Cucumber",
                                 family="chop", weight=-8.0)]
    _clear_claimed_commitment(bb, book)
    assert bb.commitment.active_task is None
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_p2_intents.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement order/suppression filters**

In `generate_candidates`, build `orders = [o for o in world.orders if not _is_abandoned(bb, o.get("id"))]` and iterate that instead of `world.orders`. In `_feasibility_of`, after `claims_available`, check the task's family against `bb.p2_suppressed_families` (non-expired) and return `(False, "p2_suppressed")`.

- [ ] **Step 4: Implement `_clear_claimed_commitment`**

At the start of `plan()`, if `bb.commitment.active_task` is matched by a negative `claim`/`assign` preference, set `bb.commitment.active_task = None` and `bb.commitment.infeasible_streak = 0`. Do **not** set `bb.commitment` to `None`; other code reads its fields.

- [ ] **Step 5: Run tests, expect pass**

Run: `python3 -m pytest tests/test_p2_intents.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ai/core/planner.py tests/test_preferences.py tests/test_p2_intents.py
git commit -m "feat(dialog): apply abandoned orders, family suppression, and claim-driven commitment reset"
```

---

## Task 6: Rescue Gate and Layer Fallthrough

**Files:**
- Modify: `ai/core/planner.py` (`plan`, `_select_layer`)
- Test: `tests/test_rescue_gate.py`

**Interfaces:**
- Produces:
  - `_rescue_margin_ticks(task_type) -> int`
  - `_rescue_gate_open(candidate, cushion, bb) -> bool`
  - `_select_layer(feasible, world, layer_map, bb, slack_map, tick) -> Tuple[Optional[int], List[Candidate]]`

- [ ] **Step 1: Write failing tests**

```python
def test_gate_opens_with_cushion_inside_margin(bb):
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",), item="BoiledSushiRice")
    bb.tick = 1000
    # solo_eta=17s=340 ticks, remaining=21s=420 ticks -> cushion=80 ticks
    assert _rescue_gate_open(_cand(task), cushion=80, bb=bb) is True

def test_gate_closed_when_cushion_is_comfortable(bb):
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",), item="BoiledSushiRice")
    bb.tick = 1000
    assert _rescue_gate_open(_cand(task), cushion=180, bb=bb) is False

def test_gate_closed_when_already_doomed(bb):
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",), item="BoiledSushiRice")
    bb.tick = 1000
    assert _rescue_gate_open(_cand(task), cushion=-160, bb=bb) is False

def test_l2_rescue_ignores_claim(bb):
    task = T.RescueCooked(resources=("cooker_1",))
    bb.preferences = [Preference(kind="claim", family="cook", weight=-8.0)]
    assert _claim_assign_penalty(_cand(task), bb, layer=2) == 0.0
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_rescue_gate.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement margin and gate**

`_rescue_margin_ticks`: `D = ETA.task_cost_ticks(task_type) + (0 if task_type == "DeliverPlate" else ETA.COST_DELIVER)`; return `int(max(100, min(int(0.3 * D), 200)))`. `_rescue_gate_open`: `0 <= cushion < margin` and `bb.tick > bb.rescue_cooldown_until`. The caller passes `cushion = slack_map[effective_order_id]` (solo slack); do **not** subtract `D` again.

- [ ] **Step 4: Implement `_select_layer` and use it in `plan()`**

Group `feasible` by `layer_map[c.task.key]`; iterate layers ascending. For each layer, build `eligible = [c for c in layer_cands if _claim_assign_penalty(c, bb, layer) >= 0 or _rescue_gate_open(c, cushion(c), bb)]`. If `eligible` is non-empty, return `(layer, eligible)`. If not, continue to the next layer. `cushion(c)` uses the candidate's effective order id (for `DeliverPlate`, use the same `_credited_order_id` as scoring) and `slack_map`. When a gate-open candidate is chosen, set `bb.rescue_cooldown_until = bb.tick + 100`, record `rescue_override` in `decision_record`, and set `bb.dialog.pending_event = ("rescue", order_id)` with `bb.dialog.event_seq += 1` so Agent can forward it to the worker.

- [ ] **Step 5: Replace the current `layer_filter` + `in_layer` selection**

Keep `layer_filter` for its `layer_map` (trace), but score the list returned by `_select_layer`. If no layer is eligible, return `None` with the usual empty record.

- [ ] **Step 6: Run tests, expect pass**

Run: `python3 -m pytest tests/test_rescue_gate.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ai/core/planner.py tests/test_rescue_gate.py
git commit -m "feat(dialog): add cushion-based rescue gate and layer fallthrough"
```

---

## Task 7: Audio Capture, VAD, and Playback

**Files:**
- Create: `ai/dialog/audio.py`
- Test: `tests/test_audio_vad.py`

**Interfaces:**
- Produces:
  - `list_input_devices() -> List[dict]`, `list_output_devices() -> List[dict]`
  - `AudioCapture(device_index=None, sample_rate=16000, frame_ms=50).frames() -> Iterator[np.ndarray]`
  - `VAD(threshold_db=9.0, noise_floor_db=-40.0, frame_ms=50)` with `.paused`, `.feed(frame) -> Optional[str]` (`"start"`/`"end"`/`None`)
  - `AudioPlayer(sample_rate=24000).play(chunks)`, `.stop()`, `.is_playing`

- [ ] **Step 1: Write failing tests**

```python
def test_vad_detects_speech_above_floor():
    vad = VAD(threshold_db=9.0, noise_floor_db=-40.0)
    frame = np.full(800, 0.2, dtype=np.float32)  # ~-14 dB
    assert vad.feed(frame) in (None, "start")

def test_vad_paused_ignores_loud_frame():
    vad = VAD()
    vad.paused = True
    assert vad.feed(np.full(800, 0.9, dtype=np.float32)) is None

def test_continuous_game_noise_does_not_open_speech():
    vad = VAD(threshold_db=9.0, noise_floor_db=-40.0)
    # Steady game audio raises the adaptive floor; a flat 3 s burst must not
    # look like the onset of a human utterance.
    for _ in range(60):
        assert vad.feed(np.full(800, 0.05, dtype=np.float32)) in (None,)

def test_vad_ends_after_silence():
    vad = VAD()
    loud = np.full(800, 0.2, dtype=np.float32)
    quiet = np.zeros(800, dtype=np.float32)
    vad.feed(loud); vad.feed(loud); vad.feed(loud)
    for _ in range(9):
        out = vad.feed(quiet)
    assert out == "end"

def test_audio_player_stop_clears_queue():
    player = AudioPlayer(sample_rate=24000)
    player.play([b"\x00\x00" * 240])
    player.stop()
    assert player.is_playing is False
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_audio_vad.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai.dialog.audio'`.

- [ ] **Step 3: Implement capture/VAD/player**

`AudioCapture` uses `sounddevice.InputStream` with a callback pushing mono float32 frames to a `queue.Queue`; `frames()` yields 50 ms blocks. `VAD` tracks a rolling noise floor (10th percentile of recent frames), opens after 3 consecutive frames above `floor + threshold_db`, ends after 400 ms below threshold, has min 300 ms / max 8 s utterance guards. `AudioPlayer` uses `sounddevice.OutputStream` with a callback consuming a `queue.Queue` of PCM chunks; `stop()` clears the queue and sets `is_playing=False`; no `sounddevice.wait()`.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_audio_vad.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/audio.py tests/test_audio_vad.py
git commit -m "feat(dialog): add audio capture, RMS VAD, and streaming playback"
```

---

## Task 8: Volcengine ASR and TTS Clients

**Files:**
- Create: `ai/dialog/asr.py`
- Create: `ai/dialog/tts.py`
- Test: `tests/test_asr_tts.py`

**Interfaces:**
- Produces:
  - `ASRClient(api_key, resource_id, sample_rate=16000).stream(pcm_chunks) -> Iterator[dict]` yielding `{"text": str, "definite": bool}`
  - `TTSClient(api_key, resource_id, voice, sample_rate=24000).synthesize(text) -> Iterator[bytes]`

- [ ] **Step 1: Write failing tests with a fake websocket**

```python
def test_asr_yields_only_definite(monkeypatch):
    fake = FakeWS([
        {"result": [{"text": "你", "definite": False}]},
        {"result": [{"text": "你好", "definite": True}]},
    ])
    monkeypatch.setattr(asr.websockets, "connect", lambda *a, **k: fake)
    client = ASRClient("key", "volc.bigasr.sauc.duration")
    out = list(client.stream([b"\x00\x00" * 3200]))
    assert out == [{"text": "你好", "definite": True}]

def test_tts_yields_audio_chunks(monkeypatch):
    fake = FakeWS([b"\x01\x02", b"\x03\x04"])
    monkeypatch.setattr(tts.websockets, "connect", lambda *a, **k: fake)
    client = TTSClient("key", "seed-tts-2.0", "zh_female_qingxin")
    assert b"".join(client.synthesize("行，我洗。")) == b"\x01\x02\x03\x04"
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_asr_tts.py -q`
Expected: FAIL with missing modules.

- [ ] **Step 3: Implement ASR/TTS wrappers**

ASR: connect `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` with `X-Api-Key`, `X-Api-Resource-Id`, `X-Api-Connect-Id`; send the full client request JSON (`audio.format=pcm`, `rate=16000`, `bits=16`, `channel=1`), then 200 ms PCM packets, then the end packet; parse the server's binary messages into JSON and yield `result[]` entries with `definite=true`. Reference the official demo for the binary header/payload layout; keep the protocol helpers private.

TTS: connect the v3 bidirectional streaming endpoint for the configured `resource_id`, send the text and end marker, yield PCM chunks. Keep `format=pcm`, `sample_rate=24000`.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_asr_tts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/asr.py ai/dialog/tts.py tests/test_asr_tts.py
git commit -m "feat(dialog): add Volcengine streaming ASR and TTS clients"
```

---

## Task 9: LLM Client and Prompt

**Files:**
- Create: `ai/dialog/llm.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Produces:
  - `LLMClient(api_key, base_url, model, timeout=3.0).query(prompt) -> str`
  - `build_prompt(context: dict, event: str, transcript: str) -> str`
  - `parse_json_response(text: str) -> dict`

- [ ] **Step 1: Write failing tests**

```python
def test_llm_query_returns_streamed_text(monkeypatch):
    resp = FakeSSE(['data: {"choices":[{"delta":{"content":"{\\"addressed\\":true"}}]}\n\n',
                    'data: {"choices":[{"delta":{"content":",\\"reply\\":\\"行\\",\\"commands\\":[],\\"confidence\\":0.8}"}}]}\n\n',
                    'data: [DONE]\n\n'])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/v3",
                       "doubao-seed-1.6-flash")
    text = client.query("prompt")
    assert '"addressed":true' in text

def test_parse_json_strips_markdown_fence():
    out = parse_json_response('```json\n{"addressed": false, "reply": null, '
                              '"commands": [], "confidence": 0.1}\n```')
    assert out["addressed"] is False

def test_build_prompt_contains_context_and_branches():
    prompt = build_prompt({"level": "Sushi_1_4_2P", "orders": []}, "speech", "我来切")
    assert "Sushi_1_4_2P" in prompt and "我来切" in prompt
    assert "SushiRice" in prompt and "addressed" in prompt
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_llm_client.py -q`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement client/prompt**

`query` uses `requests.post(..., stream=True, timeout=3.0)` with `model`, `messages`, `stream=True`, `temperature=0.3`, `max_tokens=200`; concatenate `choices[0].delta.content` from `data:` lines until `[DONE]`. `parse_json_response` strips fences, extracts the first JSON object, and raises `ValueError` on failure. `build_prompt` embeds the context JSON, the command table, family/branch vocabulary, the five reply rules, and the player transcript.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_llm_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/llm.py tests/test_llm_client.py
git commit -m "feat(dialog): add Ark SSE client and prompt builder"
```

---

## Task 10: Dialog Policy and Event Triggers

**Files:**
- Create: `ai/dialog/policy.py`
- Test: `tests/test_dialog_policy.py`

**Interfaces:**
- Produces:
  - `should_trigger(event: str, state: DialogState, tick: int, transcript: str = "") -> bool`
  - `merge_new_orders(order_ids, state, tick) -> Tuple[bool, List]`
  - `emergency_template(kind: str, **kwargs) -> str`
  - `assign_expired_template(family: str) -> str`

- [ ] **Step 1: Write failing tests**

```python
def test_speech_is_not_rate_limited():
    state = DialogState(last_speech_tick=100)
    assert should_trigger("speech", state, tick=110, transcript="我来切") is True

def test_proactive_4s_interval():
    state = DialogState(last_rescue_tick=100)
    assert should_trigger("rescue", state, tick=150) is False
    assert should_trigger("rescue", state, tick=180) is True

def test_new_order_merges_within_one_second():
    state = DialogState()
    first, batch = merge_new_orders([1], state, tick=100)
    assert first is False and batch == [1]
    second, batch = merge_new_orders([2], state, tick=110)
    assert second is False and batch == [1, 2]
    third, batch = merge_new_orders([3], state, tick=180)
    assert third is True and batch == [3]

def test_assign_expired_never_calls_llm():
    assert should_trigger("assign_expired", DialogState(), tick=100) is False
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_dialog_policy.py -q`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement policy**

`speech` always triggers (dedupe identical transcript within 4 s). `new_order` uses a 1 s batch window and triggers once the window closes. `rescue`/`help` use a 4 s minimum interval. `assign_expired` returns `False`; the caller uses `assign_expired_template`. Emergency templates are short Chinese strings with no invented pot/order numbers.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_dialog_policy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/policy.py tests/test_dialog_policy.py
git commit -m "feat(dialog): add event triggers, rate limits, and templates"
```

---

## Task 11: Dialog Worker Thread

**Files:**
- Create: `ai/dialog/worker.py`
- Test: `tests/test_dialog_worker.py`

**Interfaces:**
- Consumes: `VoiceConfig`, `Preference`, `DialogCommand`, `LLMTurn`; `ASRClient`, `TTSClient`, `LLMClient`, `AudioCapture`, `VAD`, `AudioPlayer`.
- Produces: `DialogWorker(config, cmd_q, ack_q, ctx_q, event_q, clients=None)` with `.start()`, `.stop()`, `.publish_context(ctx)`, `.put_event(event)`, and `._handle_transcript(text, context)` (the ASR→LLM→command→ack→TTS path, exposed as the deterministic test seam).

- [ ] **Step 1: Write failing worker tests with fakes**

```python
def test_speech_produces_command_then_speaks_after_ack():
    turn = LLMTurn(True, "行，你切。", [
        DialogCommand("claim_task", family="chop")], 0.9)
    worker, q = make_worker(llm=FakeLLM(turn))
    worker.start()
    worker._handle_transcript("我来切", {"level": "Sushi_1_4_2P"})
    cmd = q.cmd_q.get(timeout=2)
    assert cmd.type == "claim_task" and cmd.family == "chop"
    q.ack_q.put((cmd.command_id, "accepted", ""))
    assert q.tts.spoken == ["行，你切。"]
    worker.stop()

def test_rejected_ack_suppresses_reply():
    turn = LLMTurn(True, "行，我洗。", [
        DialogCommand("direct_p2", family="wash")], 0.9)
    worker, q = make_worker(llm=FakeLLM(turn))
    worker.start()
    worker._handle_transcript("去洗碗", {"level": "Sushi_1_4_2P"})
    cmd = q.cmd_q.get(timeout=2)
    q.ack_q.put((cmd.command_id, "rejected", "no_sink"))
    assert q.tts.spoken == []
    worker.stop()

def test_tts_pauses_vad():
    worker, q = make_worker(llm=FakeLLM(LLMTurn(True, "行。", [], 0.9)))
    q.tts.block = True
    worker.start()
    worker._speak("行。")
    assert q.vad.paused is True
    q.tts.release()
    assert q.vad.paused is False
    worker.stop()
```

`make_worker` (test helper) returns `(DialogWorker, FakeBundle)` where `FakeBundle` holds `cmd_q`, `ack_q`, `ctx_q`, `event_q`, `tts`, `vad`, and `FakeLLM(turn)` serializes the `LLMTurn` to JSON.


- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_dialog_worker.py -q`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement worker lifecycle and pipeline**

Start a daemon thread in `start()`; set a stop event in `stop()` and join with timeout. The loop reads the latest context and event, runs VAD/ASR on speech, calls `should_trigger`, builds the prompt, calls `LLMClient.query`, parses `LLMTurn`, puts commands on `cmd_q`, waits for `ack_q` with a 2 s timeout, then calls `_speak(reply)` only if the ack is not `rejected`. `_speak` sets `vad.paused=True`, plays through `AudioPlayer`, then sets it back to `False`. `assign_expired` uses the template and never calls the LLM. All network/audio errors are caught, logged, and do not kill the thread.

- [ ] **Step 4: Run tests, expect pass**

Run: `python3 -m pytest tests/test_dialog_worker.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/worker.py tests/test_dialog_worker.py
git commit -m "feat(dialog): add daemon worker with VAD/ASR/LLM/TTS and ack-gated speech"
```

---

## Task 12: Agent Integration

**Files:**
- Modify: `ai/agent.py`
- Test: `tests/test_dialog_worker.py` (agent section), `tests/test_p2_intents.py`

**Interfaces:**
- Consumes: `DialogWorker`, `DialogCommand`, `Preference`, `prune_expired_preferences`, `DialogState`.
- Produces: `Agent(ctrl=None, voice=None)`, `_service_dialog(world)`, `_apply_dialog_command(world, cmd)`, `_build_dialog_context(world)`.

- [ ] **Step 1: Write failing agent tests**

```python
def test_agent_applies_claim_command():
    agent = Agent(voice=None)
    agent.bb.tick = 100
    agent._apply_dialog_command(world, DialogCommand(
        "claim_task", branch="SushiRice", family="cook", command_id="c1"))
    assert any(p.branch == "SushiRice" and p.weight == -8.0
               for p in agent.bb.preferences)

def test_agent_cancel_p2_then_direct_clears_suppression():
    agent = Agent(voice=None)
    agent.bb.tick = 100
    agent._apply_dialog_command(world, DialogCommand("cancel_p2", family="cook"))
    assert agent.bb.p2_suppressed_families["cook"] > 100
    agent._apply_dialog_command(world, DialogCommand("direct_p2", family="cook"))
    assert "cook" not in agent.bb.p2_suppressed_families

def test_agent_resets_roles_on_new_level():
    agent = Agent(voice=None)
    agent.bb.preferences = [Preference(kind="role", family="cook", weight=1.2)]
    agent._maybe_reset_dialog_level(World({"level": "Sushi_1_4_2P", "phase": "level"}))
    agent._maybe_reset_dialog_level(World({"level": "Sushi_1_5_2P", "phase": "level"}))
    assert [p for p in agent.bb.preferences if p.kind == "role"] == []
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_dialog_worker.py -q -k agent`
Expected: FAIL with missing methods.

- [ ] **Step 3: Add dialog lifecycle to `Agent`**

`Agent.__init__(ctrl=None, voice=None)` creates the queues and `DialogWorker` when `voice` is not `None`; `run()` starts it and `finally` stops it. `_tick_once` calls `_service_dialog(world)` after `self.book` is rebuilt and `bb.tick` is set, before `arbiter.select_mode`/`planner.plan`.

- [ ] **Step 4: Implement `_service_dialog`**

Drain `cmd_q`; for each command call `_apply_dialog_command`; put `(command_id, outcome, reason)` on `ack_q`. Prune expired preferences and abandoned orders. Every 10 ticks publish `_build_dialog_context`. Detect new order ids vs `_known_order_ids` and put a `new_order` event. Drain `bb.dialog.pending_event` to the event queue. Track `chosen_task is None` for 40 ticks + P1 `hands_free` to put a `help` event. Trace `dialog` events.

- [ ] **Step 5: Implement command application**

- `claim_task`: append `Preference(kind="claim", weight=-8, base_ttl=600)`; bound candidate key uses `base_ttl=240`.
- `release_claim`: remove matching claim prefs.
- `set_role` / `clear_role`: append/remove `kind="role"` with `weight=+1.2` for P2 and `-1.2` for P1.
- `assign_p1`: append `kind="assign"`, `weight=-4`, `expires_tick=tick+ttl`, `base_ttl=ttl`.
- `cancel_assign`: remove matching assign prefs.
- `direct_p2`: append `kind="direct"`, `weight=+4`, `expires_tick=tick+ttl`, `base_ttl=ttl`; also pop `p2_suppressed_families[family]`.
- `cancel_p2`: set `p2_suppressed_families[family] = tick + 160`.
- `abandon_order`: set `abandoned_orders[order_id] = tick + 1200`; `resume_order`: pop it.
- `noop`: no state change.
- After a `claim_task`/`assign_p1`, clear `bb.commitment.active_task` if it matches (use `preference_matches_task`).

- [ ] **Step 6: Run tests, expect pass**

Run: `python3 -m pytest tests/test_dialog_worker.py tests/test_p2_intents.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ai/agent.py tests/test_dialog_worker.py tests/test_p2_intents.py
git commit -m "feat(dialog): integrate worker, command application, events, and context publishing"
```

---

## Task 13: CLI, Config, and Documentation

**Files:**
- Modify: `run_agent.py`
- Create: `.env.example`
- Modify: `AGENTS.md`, `docs/agent/architecture.md`
- Test: `tests/test_run_agent.py`

**Interfaces:**
- Produces: `--voice`, `--no-voice`, `--voice-debug`, `--voice-log PATH`; `VoiceConfig` built from env.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_voice_flag_parses():
    args = _parse_args(["--voice", "--voice-debug"])
    assert args.voice is True and args.voice_debug is True

def test_no_voice_overrides_env(monkeypatch):
    monkeypatch.setenv("VOLC_ASR_API_KEY", "x")
    args = _parse_args(["--no-voice"])
    assert args.voice is False
```

- [ ] **Step 2: Run tests, expect failure**

Run: `python3 -m pytest tests/test_run_agent.py -q`
Expected: FAIL with unrecognized arguments.

- [ ] **Step 3: Implement flags and env**

Add the four flags. Build `VoiceConfig` from `VOLC_ASR_API_KEY`, `VOLC_ASR_RESOURCE_ID`, `VOLC_TTS_API_KEY`, `VOLC_TTS_VOICE`, `VOLC_TTS_RESOURCE_ID`, `ARK_API_KEY`, `ARK_MODEL`, `ARK_BASE_URL`, `OC2_VOICE_NAME`, `OC2_VOICE_RATE`, `OC2_VOICE_VOLUME`. If `--no-voice`, pass `voice=None`. If `--voice` and any required key is missing, print a clear error and return 2. If neither flag is given, enable voice only when all required keys are present; otherwise print a warning and continue without voice.

- [ ] **Step 4: Write docs**

`.env.example` lists key names with placeholders. `AGENTS.md` adds the voice flags and `requirements-voice.txt`; `docs/agent/architecture.md` adds `ai/dialog/` to the module table and notes the 20 Hz/2 Hz boundary. Keep the existing style.

- [ ] **Step 5: Run tests, expect pass**

Run: `python3 -m pytest tests/test_run_agent.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add run_agent.py .env.example AGENTS.md docs/agent/architecture.md tests/test_run_agent.py
git commit -m "feat(dialog): add voice CLI, env config, and docs"
```

---

## Task 14: Integration Tests with Real Frames

**Files:**
- Create: `tests/test_dialog_integration.py`
- Fixtures: use `tests/fixtures/full_level.ndjson`; add scripts under `tests/fixtures/dialog_scenarios/` only if a state is missing.

**Interfaces:**
- Consumes: all prior tasks; fake ASR/TTS/LLM; `World`.
- Produces: end-to-end scenarios.

- [ ] **Step 1: Write the frame loader and first failing test**

```python
def load_last_world(path="tests/fixtures/full_level.ndjson"):
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            last = json.loads(line)
    return World(last)

def test_player_claims_delivery_p2_drops():
    world = load_last_world()
    agent = Agent(voice=None)
    agent.book = RecipeBook(world.recipes, world.stations)
    agent.bb.tick = 100
    chosen, _ = planner.plan(world, agent.bb, None, agent.book)
    # Find a frame where the chosen task is DeliverPlate; if the fixture's
    # last frame has none, skip with a clear message and record a fixture.
    if chosen is None or chosen.TYPE != "DeliverPlate":
        pytest.skip("fixture has no delivery candidate; record a new frame")
    agent._apply_dialog_command(world, DialogCommand(
        "claim_task", family="deliver", command_id="c1"))
    new_choice, _ = planner.plan(world, agent.bb, None, agent.book)
    assert new_choice is None or new_choice.TYPE != "DeliverPlate"
```

- [ ] **Step 2: Run test, expect failure or skip**

Run: `python3 -m pytest tests/test_dialog_integration.py -q`
Expected: FAIL until the helper/imports exist; if the fixture has no delivery candidate, skip and record a frame with `tools/record_frames.py` (document the command in the test docstring).

- [ ] **Step 3: Add the remaining scenarios**

- `test_player_claims_rice_branch`: claim `branch=SushiRice`, `family=cook`; assert rice candidates get the penalty and cucumber candidates do not.
- `test_pending_wash_executes_when_plate_appears`: apply `direct_p2 family=wash`; assert `pending`; then mutate the world to add a dirty plate and assert a `WashPlate` candidate scores with +4.
- `test_l2_rescue_not_blocked_by_cook_claim`: claim cook; assert `RescueCooked` keeps its score.
- `test_rescue_gate_override_with_explanation`: construct a claimed cook candidate with `cushion=4s`, assert `rescue_override` and `overridden` reply path.
- `test_multi_command_partial_rejection`: one valid `claim_task`, one invalid `direct_p2`; assert the valid preference exists, the invalid one does not, and the worker suppresses the original reply.

- [ ] **Step 4: Run all tests, expect pass**

Run: `python3 -m pytest tests/test_dialog_integration.py -q`
Expected: PASS (or documented skips for missing fixture states).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_dialog_integration.py tests/fixtures/dialog_scenarios/
git commit -m "test(dialog): add real-frame integration scenarios"
```

---

## Self-Review

### 1. Spec Coverage

- §2 decisions: Task 1 (schema/config), Task 12 (commands), Task 13 (flags).
- §3 experience: Task 14 (claim/drop, pending wash, rescue), Task 12 (command application).
- §4.1–4.4 (input/output/validation): Tasks 1, 9, 11.
- §4.5 (preferences/branches/families): Tasks 1, 2, 3, 4.
- §4.6 (layers/gate/fallthrough/L0–L2 immunity): Task 6.
- §4.7 (TTL/base_ttl/evidence): Tasks 4, 6, 12.
- §4.8 (P1 evidence): Task 12 context uses `p1_cell`/holding; Task 6 reuses `solo_slack`.
- §4.9 (scope): Tasks 1, 10 (unsupported commands rejected/ignored), Task 13.
- §5.1–5.5 (LLM triggers, cadence, emergency, half-duplex, missed): Tasks 10, 11, 12.
- §6.1–6.5 (modules/threads/planner/config): Tasks 1–13.
- §7.1–7.4 (ASR/TTS/LLM/fallback): Tasks 8, 9, 11.
- §8 (audio/VAD/playback): Task 7, Task 11.
- §9 (deps/permissions): Task 1, Task 13.
- §10 (tests/acceptance): Task 14 and the per-task tests.
- §11 (non-goals): enforced by command validation and tests.
- §12 (calls): Task 13 documents config; Task 6 pins the gate formula.

### 2. Step Scan

Every step names a file, a test, a command, or a signature. No TBDs. The ASR/TTS protocol step references the official binary layout instead of transcribing it; the wrapper and tests pin the observable behavior. The integration test explicitly skips if the single fixture lacks the required state and documents how to record one.

### 3. Type Consistency

- `Preference` fields match across Tasks 1, 3, 4, 5, 12.
- `DialogCommand` fields match across Tasks 1, 9, 11, 12.
- `Candidate.branch` is defined in Task 3 and consumed in Tasks 3, 5, 6.
- `_preference_score(candidate, bb, layer)` is defined in Task 3 and used in Task 6.
- `task_key_str` is defined in Task 1 and used in Task 3.
- `_claim_assign_penalty` is defined in Task 3 and used in Task 6.
- `prune_expired_preferences` is defined in Task 4 and used in Task 12.
- `FAMILY_OF_TYPE`/`RESCUE_TYPES` are defined in Task 1 and used in Tasks 3, 5, 6, 12.
- `DialogWorker` constructor in Task 11 matches Task 12's instantiation.

### 4. Review Focus

Each of the five review-focus lines has a test:
1. Game-audio false positives → `test_vad_...` in Task 7 (threshold/hold behavior).
2. `solo_slack` cushion → three gate tests in Task 6.
3. `FetchToPlate`/`Prefetch` coverage → `test_branch_claim_covers_fetch_to_plate` in Task 3.
4. Multi-command partial rejection → `test_multi_command_partial_rejection` in Task 14 and `test_rejected_ack_suppresses_reply` in Task 11.
5. Speak-after-ack → `test_speech_produces_command_then_speaks_after_ack` and `test_rejected_ack_suppresses_reply` in Task 11.

### 5. Proportion

The plan is longer than the spec in step count but not a code transcript: test names/assertions and signatures carry the decisions; only the family/branch tables and the gate formula are repeated from the spec because they are exact values. No task contains a full implementation body.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-18-voice-dialog-interaction.md`.

**Please review the plan. Which execution approach would you prefer?**

- **Subagent-driven** — a fresh subagent implements each task and a fresh reviewer checks it before the next one starts; most thorough, but costs a fresh context per task.
- **Native** — I implement every task myself in this session, then one fresh reviewer checks the whole branch; cheapest and fastest.

**For this plan I recommend Subagent-driven**, because the 14 tasks have tightly coupled interfaces (`Preference`, `DialogCommand`, `Candidate.branch`, ack/reply ordering) and a mistake in the rescue gate or half-duplex pipeline would be hard to see in a single end-of-branch review. Does the plan capture what you want, and which approach should we use?
