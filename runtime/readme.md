# YSM -> Figura Lua runtime (faithful port)

Reimplements YesSteveModel's *runtime* in Lua so a Figura avatar reproduces YSM
behavior exactly, instead of baking a near-static converted model. All semantics
are ported 1:1 from the OpenYSM Java source (geckolib3 core + client/animation).
Static structure checks only -- visual verification requires loading in
Minecraft/Figura.

## Modules (load order)
1. ysm_runtime_core.lua        Stage 1: math / coord convention / easing /
                               keyframe sampler / second+first-order physics
2. ysm_molang.lua              Stage 2: MoLang lexer + Pratt parser + evaluator
                               with exact math builtins; query/variable/temp
3. ysm_runtime_controller.lua  Stage 4: per-controller transition state machine
                               (idle/begin/running/ending) + weighted blend emit
4. ysm_runtime_processor.lua   Stage 3: multi-controller layered blend + reset/
                               fade smoothing across all bones
5. ysm_runtime_binding.lua     Stage 5: item-hold resolution, first-person arm,
                               weapon/arrow visibility, query.* host adapter

## Per-frame pipeline
  q = binding.make_query(host)                 -- fill query.* from live state
  for each controller (in pipeline order):
      ctrl:process(seekTime, molangCtx)        -- advance its state machine
      ctrl:apply_to(processor, seekTime, deprecated)
  processor:finalize(seekTime)                 -- reset/fade undriven bones
  for each bone: setRot/Pos/Scale(processor:final_*())
  -- binding layer drives which animation each hold/arm controller plays:
  state,name = binding.predicate_mainhand(host, condMain)
  state,name = binding.predicate_offhand(host, condOff)
  binding.weapon_visibility(weapon_map, mainName, offName)  -> setHidden cmds
  binding.first_person_state(host)  -> arm/main visibility + is_first_person

## Stage 5 binding -- how it maps to the reported issues
* Issue 1 (bow not held / arrow not registered):
  - get_item_type + ConditionHold(addTest/doTest) resolve the held category to
    the model's `hold_mainhand:<cat>` animation (e.g. hold_mainhand:bow), exactly
    as YSM scans animation names. That hold animation poses the arm so the bow
    bone sits in-hand.
  - weapon_visibility() reveals the weapon geometry + decorative arrow bone for
    the active category and hides them when the hand is empty. The bone->category
    map is supplied by the converter (Stage 6); a host-provided heuristic map is
    the fallback when no explicit config exists (this model has none).
* Issue 2 (no first-person hands):
  - is_first_person now comes from the host (was hardcoded false). first_person_
    state() toggles the arm.json model on and the main-model arms off in first
    person, matching FirstPersonArmAnimationController's fp.arm.* pipeline.

## ConditionHold name prefixes (faithful)
  hold_mainhand$<id>     explicit item id      (preSize 14)
  hold_mainhand#<tag>    item tag
  hold_mainhand:<inner>  category (InnerClassify) or use-anim
  hold_mainhand:empty    empty hand
  (offhand: hold_offhand..., preSize 13)

## MoLang quick use
```lua
local molang = require("ysm_molang")
local binding = require("ysm_runtime_binding")
local ctx = { query = binding.make_query(host), variable = {}, temp = {} }
local value = molang.compile("query.ground_speed * 0.1", false).eval(ctx)
```
