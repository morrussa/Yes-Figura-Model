-- ysm_runtime_core.lua
-- Stage 1: faithful Lua port of OpenYSM's geckolib3 animation CORE.
-- These are the deterministic primitives the whole interpreter rests on.
-- Every block cites the authoritative Java source it was ported from so the
-- behavior matches YSM exactly (no guessed flip rules).
--
-- Ported from com.elfmcys.yesstevemodel.geckolib3.core.* :
--   util/MathUtil.java, keyframe/bone/{EasingType,CatmullRomKeyFrame,
--   LinearKeyFrame,TransitionKeyFrame,RawBoneKeyFrame,BoneKeyFrame}.java,
--   molang/value/RotationValue.java,
--   client/animation/molang/functions/physics/{SecondOrder,FirstOrder}.java,
--   client/animation/molang/PhysicsManager.java
--
-- Pure Lua 5.2 (LuaJ). No Figura API used here on purpose: this module is
-- engine-agnostic and unit-testable; the Figura glue layer (Stage 4) maps
-- these results onto model parts.

local M = {}

----------------------------------------------------------------------
-- Vector3 (plain tables {x,y,z}) ------------------------------------
----------------------------------------------------------------------
local function v3(x, y, z) return { x = x or 0, y = y or 0, z = z or 0 } end
M.v3 = v3

----------------------------------------------------------------------
-- ysm_math  (port of MathUtil.java) --------------------------------
----------------------------------------------------------------------
local math_ = {}
M.math = math_

local PI       = math.pi
local TWO_PI   = 2.0 * math.pi
local DEG2RAD  = math.pi / 180.0
local RAD2DEG  = 180.0 / math.pi
math_.PI, math_.TWO_PI, math_.DEG2RAD, math_.RAD2DEG = PI, TWO_PI, DEG2RAD, RAD2DEG
math_.ZERO = v3(0, 0, 0)
math_.ONE  = v3(1, 1, 1)

-- RotationValue.convert(f, z): degrees -> radians, negate when z (flip).
-- RawBoneKeyFrame.init() builds rotation with flip = (true, true, false)
-- for (X, Y, Z). This is YSM's ONE-AND-ONLY coordinate convention for
-- rotation keyframes: negate X and Y, keep Z, all in radians.
function math_.convert_rot_component(deg, flip)
	local r = deg * DEG2RAD
	if flip then return -r end
	return r
end

-- Convert a degrees rotation vector into YSM bone space.
function math_.convert_rot(deg_vec)
	return v3(
		-(deg_vec.x * DEG2RAD),
		-(deg_vec.y * DEG2RAD),
		 (deg_vec.z * DEG2RAD)
	)
end

function math_.lerp(a, b, t) return a + t * (b - a) end

function math_.lerp3(t, a, b)
	return v3(a.x + t * (b.x - a.x), a.y + t * (b.y - a.y), a.z + t * (b.z - a.z))
end

-- MathUtil.catmullRom(percent,left,begin,end,right)
function math_.catmullRom(p, left, begin, e, right)
	local v0 = (e - left) * 0.5
	local v1 = (right - begin) * 0.5
	local t2 = p * p
	local t3 = p * t2
	return (2 * begin - 2 * e + v0 + v1) * t3
		+ (-3 * begin + 3 * e - 2 * v0 - v1) * t2
		+ v0 * p + begin
end

function math_.catmullRom3(p, left, begin, e, right)
	return v3(
		math_.catmullRom(p, left.x, begin.x, e.x, right.x),
		math_.catmullRom(p, left.y, begin.y, e.y, right.y),
		math_.catmullRom(p, left.z, begin.z, e.z, right.z)
	)
end

function math_.normalizeAngle(angle)
	local f = angle % TWO_PI
	if f >= PI then f = f - TWO_PI end
	if f < -PI then f = f + TWO_PI end
	return f
end

----------------------------------------------------------------------
-- Quaternions (only what nlerpEulerAngles needs) -------------------
-- JOML Quaternionf is (x,y,z,w). eulerZYXToQuaternion = rotateZYX(z,y,x)
-- i.e. q = qz * qy * qx.
----------------------------------------------------------------------
local function qmul(a, b)
	return {
		x = a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
		y = a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
		z = a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w,
		w = a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
	}
end

local function qaxis(ax, ay, az, ang)
	local h = ang * 0.5
	local s = math.sin(h)
	return { x = ax * s, y = ay * s, z = az * s, w = math.cos(h) }
end

-- eulerZYXToQuaternion(angles): rotateZYX(z,y,x) = qz*qy*qx
function math_.eulerZYXToQuaternion(e)
	local qx = qaxis(1, 0, 0, e.x)
	local qy = qaxis(0, 1, 0, e.y)
	local qz = qaxis(0, 0, 1, e.z)
	return qmul(qmul(qz, qy), qx)
end

-- getEulerAnglesZYX(q) -> euler (MathUtil formula, safeAsin clamped)
function math_.getEulerAnglesZYX(q)
	local sy = -2.0 * (q.x * q.z - q.w * q.y)
	if sy < -1 then sy = -1 elseif sy > 1 then sy = 1 end
	return v3(
		math.atan(q.y * q.z + q.w * q.x, 0.5 - q.x * q.x - q.y * q.y),
		math.asin(sy),
		math.atan(q.x * q.y + q.w * q.z, 0.5 - q.y * q.y - q.z * q.z)
	)
end

local function qnlerp(a, b, t)
	local dot = a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w
	local s = 1.0
	if dot < 0 then s = -1.0 end
	local x = a.x + t * (b.x * s - a.x)
	local y = a.y + t * (b.y * s - a.y)
	local z = a.z + t * (b.z * s - a.z)
	local w = a.w + t * (b.w * s - a.w)
	local inv = 1.0 / math.sqrt(x * x + y * y + z * z + w * w)
	return { x = x * inv, y = y * inv, z = z * inv, w = w * inv }
end

-- MathUtil.nlerpEulerAngles(percent, startEuler, endEuler, offsetEuler) -> out
-- Used by AnimationProcessor to fade a bone back to its rest/initial rotation.
function math_.nlerpEulerAngles(percent, startE, endE, offsetE)
	local sTemp = v3(startE.x + offsetE.x, startE.y + offsetE.y, startE.z + offsetE.z)
	local eTemp = v3(endE.x + offsetE.x, endE.y + offsetE.y, endE.z + offsetE.z)
	local qs = math_.eulerZYXToQuaternion(sTemp)
	local qe = math_.eulerZYXToQuaternion(eTemp)
	local qr = qnlerp(qs, qe, percent)
	local e = math_.getEulerAnglesZYX(qr)
	return v3(e.x - offsetE.x, e.y - offsetE.y, e.z - offsetE.z)
end

----------------------------------------------------------------------
-- Keyframe track sampler -------------------------------------------
-- Port of EasingType + {Linear,CatmullRom,Transition}KeyFrame.
--
-- A "track" is the keyframe list for ONE channel (rotation/position/scale)
-- of ONE bone. Build it once from raw frames, then sample at any tick.
--
-- raw frame shape (already in target space; for rotation use convert_rot):
--   { t = <startTick>, easing = "linear"|"catmullrom", pre = v3, post = v3 }
-- If a frame is "contiguous" (single value), pass pre == post.
----------------------------------------------------------------------
local EPS_BEGIN = 0.00001   -- BoneKeyFrame.isBegin
local EPS_END   = 0.99999   -- BoneKeyFrame.isEnd

-- frames: array sorted by .t ascending. Returns a track table.
function M.build_track(frames)
	return { frames = frames, n = #frames }
end

local function frame_post(f) return f.post or f.pre end
local function frame_pre(f)  return f.pre or f.post end

-- Sample a track at time `tick` (already wrapped into [0, length] by caller).
-- Mirrors the segment model: index 0 = TransitionKeyFrame [0, f1.t]; segment i
-- (i>=2 in 1-based) spans [f(i-1).t, f(i).t] using easing of the END frame.
function M.sample_track(track, tick)
	local frames, n = track.frames, track.n
	if n == 0 then return nil end
	local f1 = frames[1]
	-- Transition / lead-in: [0, f1.t] holds the first frame's value.
	if n == 1 or tick <= f1.t then
		local total = f1.t
		local p = (total == 0) and 1.0 or (tick / total)
		if p > EPS_END then return frame_post(f1) end
		return frame_pre(f1)
	end
	-- Find segment [f(i-1).t, f(i).t] containing tick (i is 2..n, 1-based).
	local i = n
	for k = 2, n do
		if tick <= frames[k].t then i = k break end
	end
	local beginF = frames[i - 1]
	local endF   = frames[i]
	local total  = endF.t - beginF.t
	local p = (total == 0) and 1.0 or ((tick - beginF.t) / total)
	if p < EPS_BEGIN then return frame_post(beginF) end
	if p > EPS_END  then return frame_post(endF) end
	local easing = endF.easing or "linear"
	local bp = frame_post(beginF)   -- begin.postValue
	local ep = frame_pre(endF)      -- end.preValue
	if easing == "catmullrom" then
		local leftF  = frames[math.max(1, i - 2)]
		local rightF = frames[math.min(n, i + 1)]
		return math_.catmullRom3(p, frame_post(leftF), bp, ep, frame_pre(rightF))
	end
	return math_.lerp3(p, bp, ep)
end

----------------------------------------------------------------------
-- Physics: SecondOrder + FirstOrder + PhysicsManager ----------------
-- These ARE YSM's "physics nodes" (jiggle bones: skirt, hair, tails).
-- Exact port of the Java solvers.
----------------------------------------------------------------------
local function clamp(x, lo, hi)
	if x < lo then return lo elseif x > hi then return hi else return x end
end

local SecondOrder = {}
SecondOrder.__index = SecondOrder
M.SecondOrder = SecondOrder

-- new(input, frequency, coefficient, response)
function SecondOrder.new(input, frequency, coefficient, response)
	return setmetatable({
		inputFunction = 0.0,
		lastSimulation = 0.0,
		lastSimulationDot = 0.0,
		input = input,
		frequency = clamp(frequency, 0, 5),
		coefficient = clamp(coefficient, 0, 1),
		response = response,
	}, SecondOrder)
end

function SecondOrder:setArgs(input, frequency, coefficient, response)
	self.input = input
	self.frequency = frequency
	self.coefficient = coefficient
	self.response = response
end

function SecondOrder:update(timeStep)
	local input = self.input
	local frequency = clamp(self.frequency, 0, 5)
	local coefficient = clamp(self.coefficient, 0, 1)
	local response = self.response

	local k1 = coefficient / PI / frequency
	local k2 = 1 / (2 * PI * frequency) / (2 * PI * frequency)
	local k3 = response * coefficient / 2 / PI / frequency

	local inputFunctionDot = (input - self.inputFunction) / timeStep
	self.inputFunction = input

	local maxTimeStep = math.sqrt(4 * k2 + k1 * k1) - k1
	local cycleTime = math.ceil(timeStep / maxTimeStep)
	if cycleTime < 1 then cycleTime = 1 end
	local ts = timeStep / cycleTime

	local lastSim = self.lastSimulation
	local lastDot = self.lastSimulationDot
	while cycleTime > 0 do
		lastSim = lastSim + ts * lastDot
		lastDot = lastDot + ts * (k3 * inputFunctionDot + input - lastSim - k1 * lastDot) / k2
		cycleTime = cycleTime - 1
	end
	self.lastSimulation = lastSim
	self.lastSimulationDot = lastDot
end

function SecondOrder:getValue() return self.lastSimulation end

local FirstOrder = {}
FirstOrder.__index = FirstOrder
M.FirstOrder = FirstOrder

function FirstOrder.new(input, response)
	return setmetatable({ input = input, response = response, lastSimulation = 0.0 }, FirstOrder)
end

function FirstOrder:setArgs(input, response) self.input = input; self.response = response end

function FirstOrder:update(timeStep)
	self.lastSimulation = ((1 - (timeStep / self.response)) * self.lastSimulation)
		+ ((timeStep / self.response) * self.input)
end

function FirstOrder:getValue() return self.lastSimulation end

-- PhysicsManager: drives all solvers once per render frame.
-- interval seconds = (renderTicks - lastRenderTicks) / 20  (20 tps)
local PhysicsManager = {}
PhysicsManager.__index = PhysicsManager
M.PhysicsManager = PhysicsManager

function PhysicsManager.new()
	return setmetatable({ values = {}, lastRenderTicks = 0.0 }, PhysicsManager)
end

function PhysicsManager:put(key, physics) self.values[key] = physics end
function PhysicsManager:get(key) return self.values[key] end
function PhysicsManager:clear() self.lastRenderTicks = 0.0; self.values = {} end

function PhysicsManager:update(renderTicks)
	if self.lastRenderTicks > 0 then
		if renderTicks > self.lastRenderTicks then
			local interval = (renderTicks - self.lastRenderTicks) / 20.0
			self.lastRenderTicks = renderTicks
			for _, p in pairs(self.values) do p:update(interval) end
		end
	else
		self.lastRenderTicks = renderTicks
	end
end

return M
