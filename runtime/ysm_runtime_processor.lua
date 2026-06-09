-- ysm_runtime_processor.lua
-- Stage 3: faithful Lua port of OpenYSM's per-frame bone blend + reset engine.
-- THIS is the smoothing/layering core that the old converter lacked.
--
-- Ported from:
--   geckolib3/core/processor/AnimationProcessor.java  (tickAnimation loop)
--   geckolib3/core/util/TransitionVector3f.java        (blend weights)
--   geckolib3/core/snapshot/BoneSnapshot.java          (delta semantics)
--
-- Model of operation, per frame, exactly like Java:
--   1) For each controller IN ORDER, for each bone it drives this frame, call
--      apply_rotation / apply_position / apply_scale with that controller's
--      evaluated value + its transition weight (percentCompleted).
--      - The FIRST controller to touch a channel resets it (rot->0, pos->0,
--        scale->1); later controllers blend on top (nlerp/lerp by weight).
--   2) finalize(seekTime): any bone NOT driven this frame eases back to rest
--      (rot->0, pos->0, scale->1) over resetSpeed ticks via nlerpEulerAngles /
--      lerpValues -- the fade that prevents snapping.
--
-- snapshot.rotation/position are DELTAS over the bone's initialRotation/0;
-- scale is absolute. Final applied rotation = initialRotation + rotation.

local core = require("ysm_runtime_core")
local m = core.math
local v3 = core.v3

local P = {}
P.__index = P

local function copy3(a) return v3(a.x, a.y, a.z) end

-- TransitionVector3f blend ------------------------------------------------
-- applyLinearBlendTo: progress==0 -> target=value; else lerp(progress,value,target)
local function applyLinearBlendTo(target, value, progress)
	if progress == 0.0 then
		target.x, target.y, target.z = value.x, value.y, value.z
	else
		-- MathUtil.lerpValues(progress, begin=value, end=target)
		target.x = m.lerp(value.x, target.x, progress)
		target.y = m.lerp(value.y, target.y, progress)
		target.z = m.lerp(value.z, target.z, progress)
	end
end

-- applyRotationBlendTo: progress==0 -> target=value; else nlerp(value->target)
local function applyRotationBlendTo(target, value, progress, offset)
	if progress == 0.0 then
		target.x, target.y, target.z = value.x, value.y, value.z
	else
		local r = m.nlerpEulerAngles(progress, value, target, offset)
		target.x, target.y, target.z = r.x, r.y, r.z
	end
end

-- Constructor: opts.resetSpeed (ticks). Default 2.0 (YSM AnimationData default).
function P.new(opts)
	opts = opts or {}
	return setmetatable({
		resetSpeed = opts.resetSpeed or 2.0,
		snaps = {},   -- name -> snapshot
		active = {},  -- name -> true (in modelRendererList)
	}, P)
end

-- Register a bone with its bind/initial rotation (radians, YSM space).
function P:register_bone(name, initialRotation)
	self.snaps[name] = {
		name = name,
		initialRotation = initialRotation and copy3(initialRotation) or v3(0, 0, 0),
		rotation = v3(0, 0, 0),
		position = v3(0, 0, 0),
		scale = v3(1, 1, 1),
		currentValue = v3(0, 0, 0),
		runRot = false, runPos = false, runScale = false,
		prevRot = nil, prevPos = nil, prevScale = nil,
		resetRotTick = 0.0, resetPosTick = 0.0, resetScaleTick = 0.0,
	}
	return self.snaps[name]
end

function P:get(name) return self.snaps[name] end

local function mark_active(self, s)
	if not self.active[s.name] then self.active[s.name] = true end
end

-- value: v3 (rotation delta in radians); progress: percentCompleted weight;
-- deprecated: additive legacy mode.
function P:apply_rotation(name, value, progress, seekTime, deprecated)
	local s = self.snaps[name]; if not s then return end
	mark_active(self, s)
	if not s.runRot then
		s.runRot = true
		s.rotation.x, s.rotation.y, s.rotation.z = 0, 0, 0
	end
	s.resetRotTick = seekTime
	if deprecated then
		s.currentValue.x = s.currentValue.x + value.x
		s.currentValue.y = s.currentValue.y + value.y
		s.currentValue.z = s.currentValue.z + value.z
		s.rotation.x, s.rotation.y, s.rotation.z = s.currentValue.x, s.currentValue.y, s.currentValue.z
	else
		applyRotationBlendTo(s.rotation, value, progress or 0.0, s.initialRotation)
		s.currentValue.x, s.currentValue.y, s.currentValue.z = s.rotation.x, s.rotation.y, s.rotation.z
	end
end

function P:apply_position(name, value, progress, seekTime)
	local s = self.snaps[name]; if not s then return end
	mark_active(self, s)
	if not s.runPos then
		s.runPos = true
		s.position.x, s.position.y, s.position.z = 0, 0, 0
	end
	s.resetPosTick = seekTime
	applyLinearBlendTo(s.position, value, progress or 0.0)
end

function P:apply_scale(name, value, progress, seekTime)
	local s = self.snaps[name]; if not s then return end
	mark_active(self, s)
	if not s.runScale then
		s.runScale = true
		s.scale.x, s.scale.y, s.scale.z = 1, 1, 1
	end
	s.resetScaleTick = seekTime
	applyLinearBlendTo(s.scale, value, progress or 0.0)
end

-- Reset/fade pass. Call once after all controllers applied this frame.
function P:finalize(seekTime)
	local rs = self.resetSpeed
	for name in pairs(self.active) do
		local s = self.snaps[name]
		local running = false

		-- rotation
		if s.runRot then
			running = true; s.runRot = false; s.prevRot = nil
		else
			if s.prevRot == nil then s.prevRot = copy3(s.rotation) end
			local pr = (seekTime - s.resetRotTick) / rs
			if pr < 1.0 then
				running = true
				local r = m.nlerpEulerAngles(pr, s.prevRot, m.ZERO, s.initialRotation)
				s.rotation.x, s.rotation.y, s.rotation.z = r.x, r.y, r.z
			else
				s.rotation.x, s.rotation.y, s.rotation.z = 0, 0, 0
			end
		end

		-- position
		if s.runPos then
			running = true; s.runPos = false; s.prevPos = nil
		else
			if s.prevPos == nil then s.prevPos = copy3(s.position) end
			local pr = (seekTime - s.resetPosTick) / rs
			if pr < 1.0 then
				running = true
				s.position.x = m.lerp(s.prevPos.x, 0, pr)
				s.position.y = m.lerp(s.prevPos.y, 0, pr)
				s.position.z = m.lerp(s.prevPos.z, 0, pr)
			else
				s.position.x, s.position.y, s.position.z = 0, 0, 0
			end
		end

		-- scale
		if s.runScale then
			running = true; s.runScale = false; s.prevScale = nil
		else
			if s.prevScale == nil then s.prevScale = copy3(s.scale) end
			local pr = (seekTime - s.resetScaleTick) / rs
			if pr < 1.0 then
				running = true
				s.scale.x = m.lerp(s.prevScale.x, 1, pr)
				s.scale.y = m.lerp(s.prevScale.y, 1, pr)
				s.scale.z = m.lerp(s.prevScale.z, 1, pr)
			else
				s.scale.x, s.scale.y, s.scale.z = 1, 1, 1
			end
		end

		-- BoneTopLevelSnapshot.reset(): keep currentValue in sync (modern mode).
		s.currentValue.x, s.currentValue.y, s.currentValue.z = s.rotation.x, s.rotation.y, s.rotation.z

		if not running then self.active[name] = nil end
	end
end

-- Final applied rotation for a bone = initialRotation + rotation delta.
function P:final_rotation(name)
	local s = self.snaps[name]; if not s then return v3(0, 0, 0) end
	return v3(
		s.initialRotation.x + s.rotation.x,
		s.initialRotation.y + s.rotation.y,
		s.initialRotation.z + s.rotation.z
	)
end

return P
