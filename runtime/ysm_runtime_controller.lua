-- ysm_runtime_controller.lua
-- Stage 4: the per-controller transition / blend state machine.
--
-- Faithful port of the YSM (geckolib3) controller layer that produces the
-- *weighted, smoothly cross-faded* contributions each animation controller
-- feeds into the Stage 3 AnimationProcessor. This is what the old converter
-- lacked: it baked single clips with no transition weighting, which is why the
-- run animation looked wrong (issue 4) and motion was steppy (issue 3).
--
-- Ported 1:1 from:
--   geckolib3/core/controller/AnimationControllerInstance.java  (state machine)
--   geckolib3/core/controller/PredicateBasedController.java       (emit/blend)
--   geckolib3/core/keyframe/BoneAnimationQueue.java               (blendWeight)
--   geckolib3/core/keyframe/{AnimationPoint,KeyFramePoint,
--                            TransitionPoint,ConstantPoint}.java   (points)
--   geckolib3/core/util/TicksInterpolator.java                    (timing)
--   geckolib3/core/util/MathUtil.java                             (lerp/nlerp)
--
-- Data contract (emitted by the Stage 6 converter):
--   animation = {
--     name      = string,
--     length    = number,                  -- animationLength, in ticks
--     loop      = "loop"|"play_once"|"hold",-- EDefaultLoopTypes
--     blend_weight = number | molang-expr | nil, -- nil => 1.0
--     bones = { [boneName] = { rotation=track|nil, position=track|nil,
--                              scale=track|nil } },
--   }
-- where `track` is a core.build_track(...) result and is sampled with
-- core.sample_track(track, tick).
--
-- The controller is engine-agnostic: process(tick, ctx) advances the machine
-- and for_each_transform(fn) yields per-bone TransitionVector3f records ready
-- for processor:apply_rotation/apply_position/apply_scale.

local core = require("ysm_runtime_core")
local M = {}

local v3       = core.v3
local cmath    = core.math
local lerp     = cmath.lerp
local nlerpEuler = cmath.nlerpEulerAngles

local DEFAULT_ENDING_TICK = 3.0   -- AnimationControllerInstance.defaultTransitionTick

-- lerpValues for vec3 (MathUtil.lerpValues): a + p*(b-a)  componentwise
local function lerp3(p, a, b)
	return {
		x = a.x + p * (b.x - a.x),
		y = a.y + p * (b.y - a.y),
		z = a.z + p * (b.z - a.z),
	}
end

local function mul3(a, s) return { x = a.x * s, y = a.y * s, z = a.z * s } end
local function copy3(a) return { x = a.x, y = a.y, z = a.z } end
-- MathUtil.lerpAngle(t,k) = 1 + (t-1)*k  (scale blends toward identity 1)
local function lerp_toward_one(a, k)
	return { x = 1 + (a.x - 1) * k, y = 1 + (a.y - 1) * k, z = 1 + (a.z - 1) * k }
end

----------------------------------------------------------------------
-- TicksInterpolator -------------------------------------------------
----------------------------------------------------------------------
local function new_interpolator(transitionLengthTicks)
	local tickDuration = transitionLengthTicks * 20.0
	return {
		tickDuration = tickDuration,
		interpolate = function(t) if tickDuration ~= 0.0 then return t / tickDuration end return 1.0 end,
		get_progress = function() return tickDuration end,
	}
end

----------------------------------------------------------------------
-- Animation point evaluation (value + percentCompleted + emit) ------
-- kind: "key" | "trans" | "const"
----------------------------------------------------------------------
local function eval_blend_weight(anim, ctx)
	local bw = anim.blend_weight
	if bw == nil then return 1.0 end
	if type(bw) == "number" then return bw end
	if type(bw) == "table" and bw.eval then
		local v = bw.eval(ctx)
		return (v and v > 0.0) and v or 0.0
	end
	return 1.0
end

-- Build a point for a channel. Returns nil if the channel has no track.
local function make_key_point(track, tick)
	if not track then return nil end
	return { kind = "key", value = core.sample_track(track, tick) }
end

-- TransitionPoint: fade-IN from offset pose toward the track's first frame.
local function make_trans_point(track, lerpFactor, offset)
	if not track then return nil end
	local dst = core.sample_track(track, 0.0)      -- dstKeyframe.evaluate()
	return { kind = "trans", lerpFactor = lerpFactor, offset = copy3(offset), dst = dst,
		-- getLerpPoint() = lerpValues(lerpFactor, offset, dst)
		value = lerp3(lerpFactor, offset, dst) }
end

-- ConstantPoint: fade-OUT, holds `value`; percentCompleted ramps over totalTick.
local function make_const_point(currentTick, totalTick, value)
	local pct
	if totalTick == 0.0 then pct = (currentTick == 0.0) and 0.0 or 1.0
	else pct = currentTick / totalTick end
	return { kind = "const", value = copy3(value), pct = pct }
end

----------------------------------------------------------------------
-- Emit: port of PredicateBasedController.TransformProviderRecord ----
-- Each returns { value = vec3, pct = number } or nil.
-- pct is the TransitionVector3f.percentCompleted handed to the processor
-- (processor: progress==0 => take new fully; progress==1 => keep accumulator).
----------------------------------------------------------------------
local function emit_rotation(point, blendWeight, initialRotation)
	if not point then return nil end
	if point.kind == "const" then
		local r = copy3(point.value)
		if blendWeight ~= 1.0 then r = mul3(r, blendWeight) end
		return { value = r, pct = point.pct }
	elseif point.kind == "trans" then
		local vec = mul3(point.dst, blendWeight)            -- evaluateRaw * blendWeight
		vec = nlerpEuler(point.lerpFactor, point.offset, vec, initialRotation)
		return { value = vec, pct = 0.0 }
	else -- key
		local r = copy3(point.value)
		if blendWeight ~= 1.0 then r = mul3(r, blendWeight) end
		return { value = r, pct = 0.0 }
	end
end

local function emit_position(point, blendWeight)
	if not point then return nil end
	local r = copy3(point.value)
	local bw = blendWeight
	local pct
	if point.kind == "const" then
		pct = point.pct
	else
		if point.kind == "trans" then
			bw = lerp(1.0, blendWeight, point.lerpFactor)   -- lerpValues(lerpFactor,1,bw)
		end
		pct = 0.0
	end
	if bw ~= 1.0 then r = mul3(r, bw) end
	return { value = r, pct = pct }
end

local function emit_scale(point, blendWeight)
	if not point then return nil end
	local r = copy3(point.value)
	local bw = blendWeight
	local pct
	if point.kind == "const" then
		pct = point.pct
	else
		if point.kind == "trans" then
			bw = lerp(1.0, blendWeight, point.lerpFactor)
		end
		pct = 0.0
	end
	if bw ~= 1.0 then r = lerp_toward_one(r, bw) end   -- lerpAnglesInPlace (toward 1)
	return { value = r, pct = pct }
end

----------------------------------------------------------------------
-- Controller instance ----------------------------------------------
----------------------------------------------------------------------
local Ctrl = {}
Ctrl.__index = Ctrl

-- opts: { transition = 0.1, scale_special = false,
--         get_animation = function(name) -> animation,
--         pose_provider = function(boneName) -> {rot,pos,scale} (current pose),
--         initial_rotation = function(boneName) -> vec3 }
function M.new(opts)
	opts = opts or {}
	return setmetatable({
		transition = opts.transition or 0.0,
		scale_special = opts.scale_special or false,
		get_animation = opts.get_animation,
		pose_provider = opts.pose_provider,
		initial_rotation = opts.initial_rotation,
		interp = new_interpolator(opts.transition or 0.0),
		state = "idle",       -- idle|begin|running|ending
		tickOffset = 0.0,
		savedEndingTick = 0.0,
		lastRequested = nil,
		pending = nil,        -- {loop, animation}
		current = nil,        -- animation
		currentLoop = nil,
		active = {},          -- list of {bone, rotTrack,posTrack,scaleTrack, snapshot, output}
		queues = {},          -- per-bone: {rot=point,pos=point,scale=point}
		finished = true,
	}, Ctrl)
end

function Ctrl:adjust_tick(tick) local a = tick - self.tickOffset; return a > 0.0 and a or 0.0 end

function Ctrl:set_animation(name, loop)
	if name == nil then self:cancel(); return end
	if self.lastRequested and self.lastRequested.name == name and self.lastRequested.loop == loop then return end
	self:clear_animation()
	self.lastRequested = { name = name, loop = loop }
	local anim = self.get_animation and self.get_animation(name) or nil
	if not anim then return end
	self.pending = { loop = loop or anim.loop or "play_once", animation = anim }
end

function Ctrl:apply_pending()
	local p = self.pending
	if not p then return false end
	self.pending = nil
	self.current = p.animation
	self.currentLoop = p.loop
	self.finished = false
	self.active = {}
	self.queues = {}
	for boneName, chans in pairs(self.current.bones or {}) do
		local snapshot
		if self.pose_provider then snapshot = self.pose_provider(boneName) end
		snapshot = snapshot or {}
		local entry = {
			bone = boneName,
			rotTrack = chans.rotation, posTrack = chans.position, scaleTrack = chans.scale,
			-- offset poses captured at transition start (controllerSnapshot)
			rotOffset = snapshot.rot or v3(0, 0, 0),
			posOffset = snapshot.pos or v3(0, 0, 0),
			scaleOffset = snapshot.scale or v3(1, 1, 1),
			outRot = nil, outPos = nil, outScale = nil,   -- saved on ending
		}
		self.active[#self.active + 1] = entry
		self.queues[boneName] = {}
	end
	return true
end

function Ctrl:reset_queues()
	for _, q in pairs(self.queues) do q.rot = nil; q.pos = nil; q.scale = nil end
end

function Ctrl:clear_animation()
	if self.state ~= "idle" then
		self.state = "idle"
		self.active = {}
		self.queues = {}
		self.current = nil
		self.finished = true
	end
end

function Ctrl:cancel() self.lastRequested = nil; self.pending = nil; self:clear_animation() end

-- startEndingTransition: snapshot current outputs to hold during fade-out
function Ctrl:start_ending(tick)
	if self.state == "running" or self.state == "begin" then
		local adjusted = self:adjust_tick(tick)
		for _, e in ipairs(self.active) do
			local q = self.queues[e.bone]
			if q.rot and q.rot.value then e.outRot = copy3(q.rot.value) end
			if q.pos and q.pos.value then e.outPos = copy3(q.pos.value) end
			if q.scale and q.scale.value then e.outScale = copy3(q.scale.value) end
		end
		self.tickOffset = tick
		if self.state == "running" then
			if adjusted > self.current.length then adjusted = self.current.length end
			self.savedEndingTick = adjusted
		else
			self.savedEndingTick = 0.0
		end
		self.finished = true
		self.state = "ending"
	end
end

function Ctrl:begin_transition(ctx, tick)
	local bw = eval_blend_weight(self.current, ctx)
	local lerpFactor = self.interp.interpolate(tick)
	for _, e in ipairs(self.active) do
		e.blendWeight = bw
		local q = self.queues[e.bone]
		if e.rotTrack then q.rot = make_trans_point(e.rotTrack, lerpFactor, e.rotOffset) end
		if e.posTrack then q.pos = make_trans_point(e.posTrack, lerpFactor, e.posOffset) end
		if e.scaleTrack then
			local lf = self.scale_special and 1.0 or lerpFactor
			q.scale = make_trans_point(e.scaleTrack, lf, e.scaleOffset)
		end
	end
end

function Ctrl:run_animation(ctx, tick)
	local bw = eval_blend_weight(self.current, ctx)
	for _, e in ipairs(self.active) do
		e.blendWeight = bw
		local q = self.queues[e.bone]
		q.rot = make_key_point(e.rotTrack, tick)
		q.pos = make_key_point(e.posTrack, tick)
		q.scale = make_key_point(e.scaleTrack, tick)
	end
end

function Ctrl:end_transition(ctx, f)
	local bw = eval_blend_weight(self.current, ctx)
	for _, e in ipairs(self.active) do
		e.blendWeight = bw
		local q = self.queues[e.bone]
		if e.outRot then q.rot = make_const_point(f, DEFAULT_ENDING_TICK, e.outRot) end
		if e.outPos then q.pos = make_const_point(f, DEFAULT_ENDING_TICK, e.outPos) end
		if e.outScale then q.scale = make_const_point(f, DEFAULT_ENDING_TICK, e.outScale) end
	end
end

-- The main per-frame advance (AnimationControllerInstance.process).
function Ctrl:process(tick, ctx)
	local adjusted = self:adjust_tick(tick)
	if self.state == "ending" and adjusted >= DEFAULT_ENDING_TICK then self:clear_animation() end
	if self.state == "running" and self.currentLoop == "play_once" and adjusted >= self.current.length then
		self:start_ending(tick)
		adjusted = self:adjust_tick(tick)
	end
	if self.state == "idle" then
		if not self:apply_pending() then return end
		self.tickOffset = tick
		adjusted = 0.0
		if self.interp.get_progress() > 0.0 then self.state = "begin" else self.state = "running" end
	end
	self:reset_queues()
	if self.state == "begin" then
		if adjusted < self.interp.get_progress() then
			self:begin_transition(ctx, adjusted)
			return
		else
			adjusted = adjusted - self.interp.get_progress()
			self.tickOffset = tick - adjusted
			self.state = "running"
		end
	end
	if self.state == "running" then
		if adjusted > self.current.length then
			self.finished = true
			if self.currentLoop == "loop" then
				if self.current.length > 0.0 then adjusted = adjusted % self.current.length else adjusted = 0.0 end
				self.tickOffset = tick - adjusted
			elseif self.currentLoop == "hold" then
				adjusted = self.current.length
			end
		end
		self:run_animation(ctx, adjusted)
		return
	end
	if self.state == "ending" then
		if adjusted > DEFAULT_ENDING_TICK then adjusted = DEFAULT_ENDING_TICK end
		self:end_transition(ctx, adjusted)
	end
end

-- Push this controller's contributions into a Stage 3 processor for one frame.
-- processor must expose register_bone/apply_rotation/apply_position/apply_scale.
-- `deprecated` selects additive vs blend mode in the processor.
function Ctrl:apply_to(processor, seekTime, deprecated)
	for _, e in ipairs(self.active) do
		local q = self.queues[e.bone]
		local bw = e.blendWeight or 1.0
		local initRot = (self.initial_rotation and self.initial_rotation(e.bone)) or v3(0, 0, 0)
		processor:register_bone(e.bone, initRot)
		local r = emit_rotation(q.rot, bw, initRot)
		if r then processor:apply_rotation(e.bone, r.value, r.pct, seekTime, deprecated) end
		local p = emit_position(q.pos, bw)
		if p then processor:apply_position(e.bone, p.value, p.pct, seekTime, deprecated) end
		local s = emit_scale(q.scale, bw)
		if s then processor:apply_scale(e.bone, s.value, s.pct, seekTime, deprecated) end
	end
end

function Ctrl:for_each_transform(fn)
	for _, e in ipairs(self.active) do
		local q = self.queues[e.bone]
		local bw = e.blendWeight or 1.0
		local initRot = (self.initial_rotation and self.initial_rotation(e.bone)) or v3(0, 0, 0)
		fn(e.bone, {
			rotation = emit_rotation(q.rot, bw, initRot),
			position = emit_position(q.pos, bw),
			scale = emit_scale(q.scale, bw),
		})
	end
end

function Ctrl:get_state() return self.state end
function Ctrl:is_finished() return self.finished end

return M
