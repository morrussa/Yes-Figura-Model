-- ysm_runtime_host.lua
-- Stage 7: Figura host adapter + per-frame driver.
-- Wires the ported YSM runtime (Stages 1-5: ysm_runtime_core / ysm_molang /
-- ysm_runtime_controller / ysm_runtime_processor / ysm_runtime_binding) to a
-- live Figura avatar, consuming the Stage 6 data table (ysm_data.lua).
--
-- Coordinate mapping (proven by core.convert_rot + the legacy dynamic-bone path):
--   rotation: processor:final_rotation is RADIANS in YSM space (convert_rot has
--             already negated X,Y and kept Z). Figura setRot wants DEGREES with
--             the same signs  ->  setRot(fr.x*RAD2DEG, fr.y*RAD2DEG, fr.z*RAD2DEG)
--   position: setPos(-p.x, p.y, p.z)
--   scale:    setScale(s.x, s.y, s.z)
-- The dynamic (physics) MoLang channels were emitted by molang2lua as closures
-- over GLOBAL ysm_q/ysm_v/ysm_c/ysm_t (defined by the bootstrap), so we just
-- call them; their raw-degree result uses the same (-x,-y,z) / (-x,y,z) mapping.
--
-- Static structure checks only; visual verification requires Minecraft/Figura.

local core    = require("ysm_runtime_core")
local Ctrl    = require("ysm_runtime_controller")
local Proc    = require("ysm_runtime_processor")
local binding = require("ysm_runtime_binding")

local mm      = core.math
local RAD2DEG = mm.RAD2DEG
local v3      = core.v3

local H = { }
H.__index = H

--==================================================================
-- data normalization: core.sample_track expects frame.t + track.n,
-- but ysm_data emits frame.time and no n. Normalize once at load.
--==================================================================
local function prepare_track(tr)
	if not tr or not tr.frames then return tr end
	for _, f in ipairs(tr.frames) do
		if f.t == nil then f.t = f.time or 0.0 end
	end
	tr.n = #tr.frames
	return tr
end

local function prepare_data(D)
	for _, anim in pairs(D.animations or { }) do
		for _, chans in pairs(anim.bones or { }) do
			prepare_track(chans.rotation)
			prepare_track(chans.position)
			prepare_track(chans.scale)
		end
	end
end

--==================================================================
-- bone resolution: recursive search by part name, cached.
--==================================================================
local function resolve_part(root, name)
	local found = nil
	local function rec(p)
		if found then return end
		local ok, nm = pcall(function() return p:getName() end)
		if ok and nm == name then found = p; return end
		local ok2, kids = pcall(function() return p:getChildren() end)
		if ok2 and kids then
			for _, c in ipairs(kids) do
				rec(c)
				if found then return end
			end
		end
	end
	if root then rec(root) end
	return found
end

function H:part(name)
	local cached = self._part_cache[name]
	if cached ~= nil then
		if cached == false then return nil end
		return cached
	end
	local root = nil
	pcall(function() root = models[self.model] end)
	local p = resolve_part(root, name)
	self._part_cache[name] = p or false
	return p
end

--==================================================================
-- item classification (heuristic: id substrings, InnerClassify order).
-- YSM uses item tags / instanceof; Figura only exposes the id reliably,
-- so this is a best-effort fallback and a likely refinement point.
--==================================================================
local function classify_id(id)
	local d = { id = id }
	if not id or id == "" then d.empty = true; return d end
	local s = string.lower(id)
	local function has(sub) return string.find(s, sub, 1, true) ~= nil end
	if has("crossbow") then d.crossbow = true
	elseif has("bow") then d.bow = true end
	if has("sword") then d.sword = true end
	if has("pickaxe") then d.pickaxe = true
	elseif has("axe") then d.axe = true end
	if has("shovel") then d.shovel = true end
	if has("hoe") then d.hoe = true end
	if has("shield") then d.shield = true end
	if has("trident") then d.spear = true end
	if has("fishing_rod") then d.fishing_rod = true end
	if has("splash_potion") or has("lingering_potion") then d.throwable_potion = true end
	return d
end

local function safe(fn, dflt)
	local ok, v = pcall(fn)
	if ok and v ~= nil then return v end
	return dflt
end

--==================================================================
-- host adapter: getters consumed by binding.make_query (called as
-- getter(host, ctx, args)) and by the hold predicates. Missing values
-- resolve to 0/false. All Figura calls are pcall-guarded.
--==================================================================
local function build_host(self)
	local h = { }
	h.is_preview = false

	-- numeric queries
	h.ground_speed = function()
		return safe(function()
			local vx, _, vz = player:getVelocity():unpack()
			return math.sqrt((vx or 0) * (vx or 0) + (vz or 0) * (vz or 0)) * 20
		end, 0)
	end
	h.vertical_speed = function()
		return safe(function()
			local _, vy = player:getVelocity():unpack()
			return (vy or 0) * 20
		end, 0)
	end
	h.life_time = function() return self.seek / 20.0 end
	-- query.anim_time = per-animation LOCAL time in seconds. YSM defines it as
	-- adjustedTick/20.0 (AnimationControllerContext.animTime): it starts at 0 when
	-- an animation (re)starts, wraps on loop, and freezes on hold/ending. It is NOT
	-- the global clock -- that is query.life_time. Returning self.seek/20.0 here made
	-- anim_time identical to life_time, breaking time-based keyframe expressions.
	h.anim_time = function()
		local c = self._active_ctrl or self.main_ctrl
		if c and c.current and c.state ~= "idle" then
			local a = c:adjust_tick(self.seek)
			local len = c.current.length
			if len and len > 0.0 then
				if c.currentLoop == "loop" then a = a % len
				elseif a > len then a = len end
			end
			return a / 20.0
		end
		return 0.0
	end
	h.delta_time = function() return self.dt end
	h.health = function() return safe(function() return player:getHealth() end, 0) end
	h.max_health = function() return safe(function() return player:getMaxHealth() end, 0) end
	h.player_level = function() return safe(function() return player:getExperienceLevel() end, 0) end
	h.head_x_rotation = function() return safe(function() return vanilla_model.HEAD:getRot().x end, 0) end
	h.head_y_rotation = function() return safe(function() return vanilla_model.HEAD:getRot().y end, 0) end
	h.body_x_rotation = function() return safe(function() return vanilla_model.BODY:getRot().x end, 0) end
	h.body_y_rotation = function() return safe(function() return vanilla_model.BODY:getRot().y end, 0) end
	h.time_of_day = function() return safe(function() return (world.getTimeOfDay() % 24000) / 24000 end, 0) end
	h.moon_phase = function() return safe(function() return world.getMoonPhase() end, 0) end
	h.item_in_use_duration = function() return safe(function() return player:getActiveItemTime() / 20.0 end, 0) end

	-- boolean queries
	h.is_on_ground = function() return safe(function() return player:isOnGround() end, false) end
	h.is_sneaking = function() return safe(function() return player:isSneaking() end, false) end
	h.is_sprinting = function() return safe(function() return player:isSprinting() end, false) end
	h.is_swimming = function() return safe(function() return player:isVisuallySwimming() end, false) end
	h.is_in_water = function() return safe(function() return player:isInWater() end, false) end
	h.is_on_fire = function() return safe(function() return player:isOnFire() end, false) end
	h.is_using_item = function() return safe(function() return player:isUsingItem() end, false) end
	h.is_eating = function() return safe(function() return player:isUsingItem() end, false) end
	h.is_riding = function() return safe(function() return player:getVehicle() ~= nil end, false) end
	h.is_sleeping = function() return safe(function() return player:getPose() == "SLEEPING" end, false) end
	h.is_spectator = function() return safe(function() return player:getGamemode() == "SPECTATOR" end, false) end
	h.is_jumping = function() return safe(function() return host and host.isJumping and host:isJumping() end, false) end
	h.is_first_person = function() return self._first_person == true end

	-- hold-predicate helpers
	h.item_desc = function(_, hand)
		local off = (hand == "offhand")
		local id = safe(function()
			local stack = player:getHeldItem(off)
			if not stack or stack:isEmpty() then return "" end
			return stack:getID()
		end, "")
		local d = classify_id(id)
		-- charged crossbow detection
		if id == "minecraft:crossbow" then
			d.charged_crossbow = safe(function()
				local stack = player:getHeldItem(off)
				local nbt = stack and stack:getTag()
				return nbt ~= nil and nbt.ChargedProjectiles ~= nil and #nbt.ChargedProjectiles > 0
			end, false)
		end
		return d
	end
	h.is_swinging = function(_, hand)
		return safe(function()
			if player.isSwingingArm and not player:isSwingingArm() then return false end
			local sw = player.getActiveHand and player:getActiveHand()
			if sw == nil then return (player.isSwingingArm and player:isSwingingArm()) or false end
			local want = off_hand_name(sw)
			return (player:isSwingingArm()) and (want == hand)
		end, false)
	end
	h.is_using_item_fn = nil
	h.used_item_hand = function()
		return safe(function()
			local ah = player:getActiveHand()
			if ah == "OFF_HAND" then return "offhand" end
			return "mainhand"
		end, "mainhand")
	end
	h.use_anim = function(_, _) return "none" end
	h.is_fishing = function() return false end
	h.is_maid_sitting = function() return false end

	return h
end

function off_hand_name(sw)
	if sw == "OFF_HAND" then return "offhand" end
	return "mainhand"
end

--==================================================================
-- construction
--==================================================================
function H.new(D, opts)
	opts = opts or { }
	prepare_data(D)
	local self = setmetatable({
		data = D,
		model = D.model,
		processor = Proc.new({ resetSpeed = opts.resetSpeed or 2.0 }),
		controllers = { },
		variable = { },
		temp = { },
		seek = 0.0,
		dt = 0.05,
		_part_cache = { },
		_first_person = false,
		_main_hold_name = nil,
		_off_hold_name = nil,
	}, H)

	self.host = build_host(self)

	local function get_anim(name) return self.data.animations[name] end
	local function init_rot(bone)
		local b = self.data.bones[bone]
		if b and b.default_rot then return mm.convert_rot(b.default_rot) end
		return v3(0, 0, 0)
	end

	local function make_ctrl(transition)
		local c = Ctrl.new({
			transition = transition or 0.0,
			get_animation = get_anim,
			initial_rotation = init_rot,
		})
		self.controllers[#self.controllers + 1] = { ctrl = c }
		return c
	end

	-- pipeline order: base locomotion first, then hold poses layer on top.
	self.main_ctrl = make_ctrl(opts.transition or 4.0)
	self.hold_main = make_ctrl(2.0)
	self.hold_off  = make_ctrl(2.0)

	-- ConditionHold built from the hold animation names present in the model.
	local names = { }
	for n in pairs(self.data.animations) do names[#names + 1] = n end
	self.cond_main = binding.build_condition_hold("mainhand", names)
	self.cond_off  = binding.build_condition_hold("offhand", names)

	return self
end

--==================================================================
-- animation selection
--==================================================================
-- Heuristic locomotion selector (NOT controller.json fidelity yet). YSM's real
-- selection lives in the animation_controllers state machine; porting that is
-- the remaining sub-task. This covers the common name conventions.
function H:select_main()
	local A = self.data.animations
	local function has(n) return A[n] ~= nil end
	local hst = self.host
	local speed = hst.ground_speed() or 0
	local moving = speed > 0.1
	if not hst.is_on_ground() then
		if hst.is_swimming() and has("swim") then return "swim" end
		if has("float") then return "float" end
		if has("fall") then return "fall" end
	end
	if hst.is_swimming() and has("swim") then return "swim" end
	if hst.is_sneaking() then
		if moving and has("sneak_walk") then return "sneak_walk" end
		if has("sneak") then return "sneak" end
	end
	if moving then
		if hst.is_sprinting() and has("run") then return "run" end
		if has("walk") then return "walk" end
		if has("move") then return "move" end
	end
	if has("idle") then return "idle" end
	return nil
end

function H:select_holds()
	local sm, nm = binding.predicate_mainhand(self.host, self.cond_main)
	local so, no = binding.predicate_offhand(self.host, self.cond_off)
	if sm == "PLAY" then
		self.hold_main:set_animation(nm)
		self._main_hold_name = nm
	elseif sm == "STOP" then
		self.hold_main:set_animation(nil)
		self._main_hold_name = nil
	end
	if so == "PLAY" then
		self.hold_off:set_animation(no)
		self._off_hold_name = no
	elseif so == "STOP" then
		self.hold_off:set_animation(nil)
		self._off_hold_name = nil
	end
end

--==================================================================
-- pose write-back
--==================================================================
function H:write_pose()
	for name, s in pairs(self.processor.snaps) do
		local p = self:part(name)
		if p then
			local fr = self.processor:final_rotation(name)
			pcall(function()
				p:setRot(fr.x * RAD2DEG, fr.y * RAD2DEG, fr.z * RAD2DEG)
				p:setPos(-s.position.x, s.position.y, s.position.z)
				p:setScale(s.scale.x, s.scale.y, s.scale.z)
			end)
		end
	end
end

function H:_num(val)
	if type(val) == "function" then
		local ok, r = pcall(val, self.ctx)
		if ok then return tonumber(r) or 0 end
		return 0
	end
	return tonumber(val) or 0
end

-- physics / MoLang dynamic channels override the keyframe pose for their bone.
function H:apply_dynamics()
	for _, c in ipairs(self.controllers) do
		local anim = c.ctrl.current
		if anim and anim.bones then
			for bone, chans in pairs(anim.bones) do
				if chans.rotation_dynamic or chans.position_dynamic or chans.scale_dynamic then
					local p = self:part(bone)
					if p then
						local rd = chans.rotation_dynamic
						if rd then
							local x, y, z = self:_num(rd.x), self:_num(rd.y), self:_num(rd.z)
							pcall(function() p:setRot(-x, -y, z) end)
						end
						local pd = chans.position_dynamic
						if pd then
							local x, y, z = self:_num(pd.x), self:_num(pd.y), self:_num(pd.z)
							pcall(function() p:setPos(-x, y, z) end)
						end
						local sd = chans.scale_dynamic
						if sd then
							local x, y, z = self:_num(sd.x), self:_num(sd.y), self:_num(sd.z)
							pcall(function() p:setScale(x, y, z) end)
						end
					end
				end
			end
		end
	end
end

--==================================================================
-- visibility: weapon reveal (issue 1), base-hidden bones, first-person
-- arm toggle (issue 2), vanilla item hiding while a hold pose is active.
--==================================================================
function H:apply_visibility()
	local cmds = binding.weapon_visibility(self.data.weapon_map, self._main_hold_name, self._off_hold_name)
	for _, cmd in ipairs(cmds) do
		local p = self:part(cmd.bone)
		if p then pcall(function() p:setVisible(not cmd.hidden) end) end
	end
	for _, name in ipairs(self.data.hidden_default or { }) do
		local p = self:part(name)
		if p then pcall(function() p:setVisible(false) end) end
	end
	local fp = binding.first_person_state(self.host)
	self._fp_state = fp
	-- hide the vanilla held-item model only while a custom hold pose is active
	pcall(function()
		if vanilla_model and vanilla_model.RIGHT_ITEM then
			vanilla_model.RIGHT_ITEM:setVisible(self._main_hold_name == nil)
		end
		if vanilla_model and vanilla_model.LEFT_ITEM then
			vanilla_model.LEFT_ITEM:setVisible(self._off_hold_name == nil)
		end
	end)
end

--==================================================================
-- per-frame driver. delta is the Figura render tick-delta; ctx is the
-- render-context string ("FIRST_PERSON" / "OTHER" / etc.) when available.
--==================================================================
function H:render(delta, ctx)
	if ctx ~= nil then self._first_person = (ctx == "FIRST_PERSON") end
	local wt = safe(function() return world.getTime() end, 0)
	local newSeek = wt + (delta or 0.0)
	self.dt = math.max(0.0, newSeek - self.seek)
	if self.dt <= 0.0 then self.dt = 0.05 end
	self.seek = newSeek
	local seekTime = self.seek

	local q = binding.make_query(self.host)
	self.ctx = { query = q, variable = self.variable, temp = self.temp, this = 0 }

	self._active_ctrl = nil
	pcall(function() self.main_ctrl:set_animation(self:select_main()) end)
	pcall(function() self:select_holds() end)

	for _, c in ipairs(self.controllers) do
		self._active_ctrl = c.ctrl
		pcall(function() c.ctrl:process(seekTime, self.ctx) end)
		pcall(function() c.ctrl:apply_to(self.processor, seekTime, false) end)
	end
	pcall(function() self.processor:finalize(seekTime) end)

	self:write_pose()
	self:apply_dynamics()
	self:apply_visibility()
end

return H
