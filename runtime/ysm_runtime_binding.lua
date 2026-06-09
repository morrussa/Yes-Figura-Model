-- ysm_runtime_binding.lua
-- Stage 5: binding layer (item-hold resolution, first-person arm, projectile/
-- weapon-bone visibility, and the query.* host adapter for MoLang/controllers).
--
-- Faithful port of OpenYSM sources:
--   client/animation/condition/InnerClassify.java   (getItemType / doClassifyTest)
--   client/animation/condition/ConditionHold.java    (addTest / doTest, id/tag/extra)
--   client/animation/predicate/MainHandHoldPredicate  & OffHandHoldPredicate
--   geckolib3/core/controller/controllers/FirstPersonArmAnimationController.java
--   client/renderer/layer/CustomPlayerItemInHandLayer.java (locator-based render)
--   geckolib3/core/processor/IBone.java setHidden(selfHidden, skipChildRendering)
--   geckolib3/core/molang/builtin/QueryBinding.java   (query.* names)
--
-- Engine-agnostic: a `host` adapter object supplies live state. The Figura glue
-- (main.lua) implements host; this module contains no Figura API calls.

local M = {}

-- ============================================================
-- 1) Item classifier  (InnerClassify.getItemType, EXACT priority order)
-- ============================================================
-- `desc` is a per-hand item descriptor the host fills, e.g.
--   { empty=false, id="minecraft:bow", sword=false, axe=false, bow=true, ... }
-- Booleans mirror the instanceof / tag checks done engine-side in Java.
function M.get_item_type(desc)
	if not desc or desc.empty then return "" end
	if desc.slashblade then return "slashblade" end
	if desc.sword then return "sword" end
	if desc.gohei then return "gohei" end
	if desc.axe then return "axe" end
	if desc.pickaxe then return "pickaxe" end
	if desc.shovel then return "shovel" end
	if desc.hoe then return "hoe" end
	if desc.shield then return "shield" end
	if desc.crossbow then return "crossbow" end
	if desc.bow then return "bow" end
	if desc.fishing_rod then return "fishing_rod" end
	if desc.spear then return "spear" end
	if desc.throwable_potion then return "throwable_potion" end
	return ""
end

-- InnerClassify.doClassifyTest(prefix, entity, hand)
function M.classify_test(prefix, desc)
	local t = M.get_item_type(desc)
	if t ~= "" then return prefix .. t end
	return ""
end

-- ============================================================
-- 2) ConditionHold  (addTest scan + doTest resolution)
-- ============================================================
-- Built once per hand from the list of hold animation names present in the
-- converted model (the same names addTest() scans on the Java side).
local ConditionHold = {}
ConditionHold.__index = ConditionHold

local function valid_resource_location(s)
	-- minecraft RL: optional "ns:" then path; chars [a-z0-9_./-], ns [a-z0-9_.-]
	if s == nil or s == "" then return false end
	local ns, path = s:match("^([a-z0-9_%.%-]+):([a-z0-9_%./%-]+)$")
	if ns then return true end
	return s:match("^[a-z0-9_%./%-]+$") ~= nil
end

-- hand = "mainhand" | "offhand"
function M.build_condition_hold(hand, anim_names)
	local self = setmetatable({}, ConditionHold)
	if hand == "mainhand" then
		self.idPre, self.tagPre, self.extraPre, self.preSize = "hold_mainhand$", "hold_mainhand#", "hold_mainhand:", 14
		self.emptyName = "hold_mainhand:empty"
	else
		self.idPre, self.tagPre, self.extraPre, self.preSize = "hold_offhand$", "hold_offhand#", "hold_offhand:", 13
		self.emptyName = "hold_offhand:empty"
	end
	self.idTest = {}    -- set: resource location string -> true
	self.tagTest = {}   -- list of tag location strings (order preserved)
	self.innerTest = {} -- set: full name -> true
	self.extraTes = {}  -- set: use-anim name -> true
	for _, name in ipairs(anim_names or {}) do
		self:add_test(name)
	end
	return self
end

-- Mirrors ConditionHold.addTest
function ConditionHold:add_test(name)
	if #name <= self.preSize then return end
	local sub = name:sub(self.preSize + 1)
	if name:sub(1, #self.idPre) == self.idPre and valid_resource_location(sub) then
		self.idTest[sub] = true
	end
	if name:sub(1, #self.tagPre) == self.tagPre and valid_resource_location(sub) then
		self.tagTest[#self.tagTest + 1] = sub
	end
	if name:sub(1, #self.extraPre) ~= self.extraPre or sub == "none" then return end
	-- Java also checks getUseAnimByName(sub) present; we record the suffix and
	-- the full name so both extra (use-anim) and inner (category) tests resolve.
	self.extraTes[sub] = true
	self.innerTest[name] = true
end

-- Mirrors ConditionHold.doTest. `desc` is the hand item descriptor; `use_anim`
-- is the item's use-animation name (host supplies, e.g. "bow","block","none").
function ConditionHold:do_test(desc, use_anim)
	if not desc or desc.empty then return self.emptyName end
	-- doIdTest
	if desc.id and next(self.idTest) ~= nil and self.idTest[desc.id] then
		return self.idPre .. desc.id
	end
	-- doTagTest (first matching tag, registration order)
	if #self.tagTest > 0 and desc.tags then
		for _, tag in ipairs(self.tagTest) do
			if desc.tags[tag] then return self.tagPre .. tag end
		end
	end
	-- doExtraTest
	if next(self.extraTes) == nil and next(self.innerTest) == nil then return "" end
	local inner = M.classify_test(self.extraPre, desc)
	if inner ~= "" and self.innerTest[inner] then return inner end
	if use_anim and use_anim ~= "none" and self.extraTes[use_anim] then
		return self.extraPre .. use_anim
	end
	return ""
end

-- ============================================================
-- 3) Hold predicate  (Main/OffHandHoldPredicate)
-- Returns: state, anim_name
--   state = "PLAY" | "PAUSE" | "STOP"
-- The caller drives the hold controller with this (PLAY anim / PAUSE / STOP).
-- ============================================================
-- checkSwingAndUse: swinging that arm -> false ; using item that hand -> false
local function check_swing_and_use(host, hand)
	if host.is_swinging and host.is_swinging(hand) then return false end
	if host.is_using_item and host.is_using_item() and host.used_item_hand and host.used_item_hand() == hand then
		return false
	end
	return true
end

-- cond: ConditionHold for this hand ; desc/use_anim from host
function M.predicate_mainhand(host, cond)
	if host.is_preview then return "STOP", nil end
	if not check_swing_and_use(host, "mainhand") then return "PAUSE", nil end
	local desc = host.item_desc and host.item_desc("mainhand") or { empty = true }
	-- charged crossbow special-case
	if desc.id == "minecraft:crossbow" and desc.charged_crossbow then
		return "PLAY", "hold_mainhand:charged_crossbow"
	end
	-- fishing / maid-sitting special-case (mainhand only)
	if (host.is_fishing and host.is_fishing()) or (host.is_maid_sitting and host.is_maid_sitting()) then
		return "PLAY", "hold_mainhand:fishing"
	end
	local name = cond:do_test(desc, host.use_anim and host.use_anim("mainhand") or "none")
	if name ~= "" then return "PLAY", name end
	return "STOP", nil
end

function M.predicate_offhand(host, cond)
	if host.is_preview then return "STOP", nil end
	if not check_swing_and_use(host, "offhand") then return "PAUSE", nil end
	local desc = host.item_desc and host.item_desc("offhand") or { empty = true }
	if desc.id == "minecraft:crossbow" and desc.charged_crossbow then
		return "PLAY", "hold_offhand:charged_crossbow"
	end
	local name = cond:do_test(desc, host.use_anim and host.use_anim("offhand") or "none")
	if name ~= "" then return "PLAY", name end
	return "STOP", nil
end

-- ============================================================
-- 4) Visibility binder  (issue 1: reveal held weapon / decorative arrow)
-- ============================================================
-- `weapon_map` is supplied by the converter (Stage 6) when explicit config
-- exists; otherwise the host may pass a heuristic map. Shape:
--   weapon_map = { bow = {"Bow","arrow","bone2",...}, crossbow = {...}, ... }
-- category "" (empty hand) hides every bone that appears in any entry.
-- Returns a list of { bone = name, hidden = bool } commands; the host applies
-- them via setHidden(hidden, hidden) (IBone.setHidden self+children).
local function category_of(name)
	if not name then return "" end
	local c = name:match("^hold_%a+:(.+)$")
	return c or ""
end

function M.weapon_visibility(weapon_map, mainhand_anim, offhand_anim)
	local cmds = {}
	if not weapon_map then return cmds end
	-- collect the union of all weapon bones, default hidden
	local all = {}
	for _, bones in pairs(weapon_map) do
		for _, b in ipairs(bones) do all[b] = true end
	end
	for b in pairs(all) do cmds[b] = true end -- hidden by default
	-- reveal bones for the active mainhand + offhand categories
	local function reveal(anim)
		local cat = category_of(anim)
		local bones = weapon_map[cat]
		if bones then
			for _, b in ipairs(bones) do cmds[b] = false end
		end
	end
	reveal(mainhand_anim)
	reveal(offhand_anim)
	local out = {}
	for b, hidden in pairs(cmds) do
		out[#out + 1] = { bone = b, hidden = hidden }
	end
	return out
end

-- ============================================================
-- 5) First-person arm  (issue 2)
-- ============================================================
-- FirstPersonArmAnimationController runs the same controller engine over the
-- arm model's animation namespace (fp.arm.*). The practical Figura binding:
--   * is_first_person comes from the host (NOT hardcoded false)
--   * the arm model is visible only in first person; the main model hides its
--     arms in first person (handled by host setVisible)
function M.first_person_state(host)
	local fp = host.is_first_person and host.is_first_person() or false
	return {
		is_first_person = fp,
		show_arm_model = fp,        -- arm.json model visible
		show_main_arms = not fp,    -- main model arm bones visible
		anim_prefix = "fp.arm",     -- arm controllers register under this prefix
	}
end

-- ============================================================
-- 6) query.* adapter for MoLang / controllers  (QueryBinding names)
-- ============================================================
-- Builds the `query` table expected by ysm_molang ctx. Each entry is either a
-- number or a function(ctx[,args]) -> number. Missing host getters resolve to 0
-- (matching MoLang missing-variable semantics). `host` provides getters by name.
local QUERY_NAMES = {
	"actor_count","anim_time","life_time","head_x_rotation","head_y_rotation",
	"moon_phase","time_of_day","time_stamp","delta_time","yaw_speed",
	"cardinal_facing_2d","distance_from_camera","eye_target_x_rotation",
	"eye_target_y_rotation","ground_speed","modified_distance_moved",
	"vertical_speed","walk_distance","body_x_rotation","body_y_rotation",
	"health","max_health","hurt_time","item_in_use_duration",
	"item_max_use_duration","item_remaining_use_duration","equipment_count",
	"cape_flap_amount","player_level",
}
local QUERY_BOOLS = {
	"all_animation_finished","any_animation_finished","has_rider",
	"is_first_person","is_in_water","is_in_water_or_rain","is_on_fire",
	"is_on_ground","is_riding","is_sneaking","is_spectator","is_sprinting",
	"is_swimming","is_eating","is_playing_dead","is_sleeping","is_using_item",
	"is_jumping","has_cape",
}

function M.make_query(host)
	local q = {}
	local function bind(name)
		local getter = host[name]
		if type(getter) == "function" then
			q[name] = function(ctx, args) return getter(host, ctx, args) or 0 end
		else
			q[name] = 0
		end
	end
	for _, n in ipairs(QUERY_NAMES) do bind(n) end
	for _, n in ipairs(QUERY_BOOLS) do
		local getter = host[n]
		if type(getter) == "function" then
			q[n] = function(ctx, args) return getter(host, ctx, args) and 1 or 0 end
		else
			q[n] = 0
		end
	end
	return q
end

M.QUERY_NAMES = QUERY_NAMES
M.QUERY_BOOLS = QUERY_BOOLS

return M
