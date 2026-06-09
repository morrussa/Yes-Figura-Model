-- ysm_molang.lua
-- Stage 2: a faithful MoLang runtime for the YSM Lua interpreter.
--
-- YSM's Java side compiles MoLang to bytecode via a separate engine; porting
-- that verbatim is unnecessary for behavioral fidelity. Instead this is a clean
-- MoLang evaluator whose OPERATOR and BUILTIN semantics are ported 1:1 from:
--   geckolib3/core/molang/MolangParser.java        (comment stripping, grammar)
--   geckolib3/core/molang/builtin/MathBinding.java  (function catalog)
--   geckolib3/core/molang/builtin/math/*.java        (exact per-function math)
--   geckolib3/core/molang/builtin/QueryBinding.java  (query.* variable names)
--
-- Numeric results are identical to YSM. The host-state variables (query.*) are
-- supplied by the Figura glue layer through the evaluation context.
--
-- Supported grammar (standard MoLang):
--   numbers, 'strings', math operators + - * /, comparisons == != < <= > >=,
--   logical && || !, ternary cond ? a : b (and cond ? a), null-coalesce a ?? b,
--   member access a.b.c, function calls f(x, y), assignment v.x = expr / t.x =,
--   statements separated by ';', and 'return expr'.
--   Namespaces: math/query(q)/variable(v)/temp(t)/this/context(c).

local M = {}

----------------------------------------------------------------------
-- Lexer -------------------------------------------------------------
----------------------------------------------------------------------
local TWO_CHAR = { ["&&"]=true, ["||"]=true, ["=="]=true, ["!="]=true,
	["<="]=true, [">="]=true, ["??"]=true }

local function lex(src)
	local toks, i, n = {}, 1, #src
	local function push(t, v) toks[#toks + 1] = { t = t, v = v } end
	while i <= n do
		local c = src:sub(i, i)
		if c:match("%s") then
			i = i + 1
		elseif c == "'" then
			local j = src:find("'", i + 1, true) or (n + 1)
			push("str", src:sub(i + 1, j - 1))
			i = j + 1
		elseif c:match("[%d]") or (c == "." and src:sub(i + 1, i + 1):match("%d")) then
			local num = src:match("^%d*%.?%d+[eE]?[%+%-]?%d*", i)
			if not num then num = src:match("^%d+", i) end
			push("num", tonumber(num))
			i = i + #num
		elseif c:match("[%a_]") then
			local id = src:match("^[%a_][%w_]*", i)
			push("id", id)
			i = i + #id
		else
			local two = src:sub(i, i + 1)
			if TWO_CHAR[two] then push("op", two); i = i + 2
			else push("op", c); i = i + 1 end
		end
	end
	push("eof")
	return toks
end

----------------------------------------------------------------------
-- Parser (Pratt) ---------------------------------------------------
-- AST nodes: {k="num",v} {k="str",v} {k="name",path={...}}
--   {k="call",path,args} {k="un",op,e} {k="bin",op,a,b}
--   {k="tern",c,a,b} {k="assign",path,e} {k="block",stmts}
--   {k="return",e}
----------------------------------------------------------------------
local Parser = {}
Parser.__index = Parser

local function new_parser(toks)
	return setmetatable({ toks = toks, p = 1 }, Parser)
end
function Parser:peek() return self.toks[self.p] end
function Parser:next() local t = self.toks[self.p]; self.p = self.p + 1; return t end
function Parser:is(t, v)
	local tk = self.toks[self.p]
	return tk.t == t and (v == nil or tk.v == v)
end
function Parser:eat(t, v)
	if self:is(t, v) then return self:next() end
	error("molang parse: expected " .. tostring(v or t))
end

-- binary precedence
local BINPREC = {
	["||"]=1, ["&&"]=2,
	["=="]=3, ["!="]=3,
	["<"]=4, ["<="]=4, [">"]=4, [">="]=4,
	["+"]=5, ["-"]=5,
	["*"]=6, ["/"]=6,
}

function Parser:parse_program()
	local stmts = {}
	while not self:is("eof") do
		if self:is("op", ";") then self:next()
		else
			stmts[#stmts + 1] = self:parse_stmt()
			if self:is("op", ";") then self:next() end
		end
	end
	if #stmts == 1 then return stmts[1] end
	return { k = "block", stmts = stmts }
end

function Parser:parse_stmt()
	if self:is("id", "return") then
		self:next()
		return { k = "return", e = self:parse_expr() }
	end
	return self:parse_expr()
end

-- assignment / ternary / null-coalesce at the top of expression precedence
function Parser:parse_expr()
	local left = self:parse_ternary()
	if self:is("op", "=") and left.k == "name" then
		self:next()
		return { k = "assign", path = left.path, e = self:parse_expr() }
	end
	return left
end

function Parser:parse_ternary()
	local cond = self:parse_coalesce()
	if self:is("op", "?") then
		self:next()
		local a = self:parse_expr()
		local b = nil
		if self:is("op", ":") then self:next(); b = self:parse_expr() end
		return { k = "tern", c = cond, a = a, b = b }
	end
	return cond
end

function Parser:parse_coalesce()
	local left = self:parse_binary(0)
	while self:is("op", "??") do
		self:next()
		local right = self:parse_binary(0)
		left = { k = "bin", op = "??", a = left, b = right }
	end
	return left
end

function Parser:parse_binary(minp)
	local left = self:parse_unary()
	while true do
		local tk = self:peek()
		if tk.t ~= "op" then break end
		local prec = BINPREC[tk.v]
		if not prec or prec < minp then break end
		self:next()
		local right = self:parse_binary(prec + 1)
		left = { k = "bin", op = tk.v, a = left, b = right }
	end
	return left
end

function Parser:parse_unary()
	if self:is("op", "-") then self:next(); return { k = "un", op = "-", e = self:parse_unary() } end
	if self:is("op", "!") then self:next(); return { k = "un", op = "!", e = self:parse_unary() } end
	return self:parse_postfix()
end

function Parser:parse_postfix()
	local e = self:parse_primary()
	return e
end

function Parser:parse_primary()
	local tk = self:peek()
	if tk.t == "num" then self:next(); return { k = "num", v = tk.v } end
	if tk.t == "str" then self:next(); return { k = "str", v = tk.v } end
	if tk.t == "op" and tk.v == "(" then
		self:next()
		local e = self:parse_expr()
		self:eat("op", ")")
		return e
	end
	if tk.t == "id" then
		-- member path: id ('.' id)*
		local path = { self:next().v }
		while self:is("op", ".") do
			self:next()
			path[#path + 1] = self:eat("id").v
		end
		if self:is("op", "(") then
			self:next()
			local args = {}
			if not self:is("op", ")") then
				args[#args + 1] = self:parse_expr()
				while self:is("op", ",") do self:next(); args[#args + 1] = self:parse_expr() end
			end
			self:eat("op", ")")
			return { k = "call", path = path, args = args }
		end
		return { k = "name", path = path }
	end
	error("molang parse: unexpected token " .. tostring(tk.t) .. " " .. tostring(tk.v))
end

----------------------------------------------------------------------
-- Math builtins (exact ports of builtin/math/*.java) ---------------
----------------------------------------------------------------------
local FPI = 3.1415927        -- the float PI YSM uses in Sin/Cos
local function num(x) if type(x) == "number" then return x elseif x == true then return 1 elseif x then return 0 else return 0 end end
local function wrapDeg(a)
	a = a % 360.0
	if a >= 180.0 then a = a - 360.0 end
	if a < -180.0 then a = a + 360.0 end
	return a
end

local MATH = {}
MATH.pi = math.pi
MATH.e  = math.exp(1)
MATH["floor"]  = function(a) return math.floor(a[1]) end
MATH["ceil"]   = function(a) return math.ceil(a[1]) end
MATH["round"]  = function(a) return math.floor(a[1] + 0.5) end       -- Math.round
MATH["trunc"]  = function(a) local v=a[1]; return v < 0 and math.ceil(v) or math.floor(v) end
MATH["abs"]    = function(a) return math.abs(a[1]) end
MATH["sqrt"]   = function(a) return math.sqrt(a[1]) end
MATH["exp"]    = function(a) return math.exp(a[1]) end
MATH["ln"]     = function(a) return math.log(a[1]) end
MATH["pow"]    = function(a) return a[1] ^ a[2] end
MATH["mod"]    = function(a) return a[1] % a[2] end
MATH["max"]    = function(a) return math.max(a[1], a[2]) end
MATH["min"]    = function(a) return math.min(a[1], a[2]) end
MATH["clamp"]  = function(a) local v,lo,hi=a[1],a[2],a[3]; if v<lo then return lo elseif v>hi then return hi else return v end end
MATH["sin"]    = function(a) return math.sin(a[1] / 180.0 * FPI) end   -- degrees
MATH["cos"]    = function(a) return math.cos(a[1] / 180.0 * FPI) end   -- degrees
MATH["asin"]   = function(a) return math.asin(a[1]) end
MATH["acos"]   = function(a) return math.acos(a[1]) end
MATH["atan"]   = function(a) return math.atan(a[1]) end
MATH["atan2"]  = function(a) return math.atan(a[1], a[2]) end
MATH["lerp"]   = function(a) return a[1] + (a[2] - a[1]) * a[3] end
MATH["lerprotate"] = function(a) return a[1] + wrapDeg(a[2] - a[1]) * a[3] end  -- lerpYaw
MATH["min_angle"]  = function(a) return wrapDeg(a[1]) end
-- hermite_blend(x): min=ceil(x); floor(3*min^2 - 2*min^3)  (faithful to YSM)
MATH["hermite_blend"] = function(a) local mn = math.ceil(a[1]); return math.floor(3.0*mn*mn - 2.0*mn*mn*mn) end
local function rand01() return math.random() end
MATH["random"] = function(a)
	local mn, rng = a[1], a[2]
	if mn > rng then mn, rng = rng, a[1] - rng else rng = rng - mn end
	return mn + rand01() * rng
end
MATH["random_integer"] = function(a) return math.floor(MATH["random"](a)) end
MATH["die_roll"] = function(a)
	local i, mn, rng = math.floor(a[1]), a[2], a[3]
	if mn > rng then mn, rng = rng, a[2] - rng else rng = rng - mn end
	local total = 0
	while i > 0 do total = total + mn + rand01() * rng; i = i - 1 end
	return total
end
MATH["die_roll_integer"] = function(a) return math.floor(MATH["die_roll"](a)) end
-- geckolib-compat aliases
MATH["randomi"] = MATH["random_integer"]
MATH["roll"]    = MATH["die_roll"]
MATH["rolli"]   = MATH["die_roll_integer"]
MATH["hermite"] = MATH["hermite_blend"]

----------------------------------------------------------------------
-- Evaluator ---------------------------------------------------------
----------------------------------------------------------------------
local NS_VAR  = { variable = true, v = true }
local NS_TEMP = { temp = true, t = true }
local NS_QRY  = { query = true, q = true }

local function get_store(ctx, head)
	if NS_VAR[head]  then ctx.variable = ctx.variable or {}; return ctx.variable end
	if NS_TEMP[head] then ctx.temp = ctx.temp or {}; return ctx.temp end
	return nil
end

-- Resolve a dotted store table (for v.roaming.exp style nested access).
local function resolve_store_path(store, path, from)
	local node = store
	for i = from, #path - 1 do
		node[path[i]] = node[path[i]] or {}
		node = node[path[i]]
		if type(node) ~= "table" then return nil end
	end
	return node, path[#path]
end

local eval  -- fwd decl

local function eval_name(node, ctx)
	local path = node.path
	local head = path[1]
	if NS_VAR[head] or NS_TEMP[head] then
		local store = get_store(ctx, head)
		local tbl, key = resolve_store_path(store, path, 2)
		local val = tbl and tbl[key]
		if type(val) == "table" then return 0 end
		return val or 0
	elseif NS_QRY[head] then
		local q = ctx.query or {}
		local entry = q[path[2]]
		if type(entry) == "function" then return num(entry(ctx)) end
		if entry == nil then return 0 end
		return num(entry)
	elseif head == "math" then
		local c = MATH[path[2]]
		if type(c) == "number" then return c end
		return 0
	elseif head == "this" then
		return ctx.this or 0
	else
		-- context bindings c.* or unknown -> 0
		local c = ctx[head]
		if type(c) == "table" then
			local v = c[path[2]]
			if type(v) == "function" then return num(v(ctx)) end
			return num(v or 0)
		end
		return 0
	end
end

local function eval_call(node, ctx)
	local path, head = node.path, node.path[1]
	local args = {}
	for i = 1, #node.args do args[i] = eval(node.args[i], ctx) end
	if head == "math" then
		local fn = MATH[path[2]]
		if type(fn) == "function" then return num(fn(args, ctx)) end
		return 0
	elseif NS_QRY[head] then
		local q = ctx.query or {}
		local fn = q[path[2]]
		if type(fn) == "function" then return num(fn(ctx, args)) end
		return 0
	else
		local c = ctx[head]
		if type(c) == "table" and type(c[path[2]]) == "function" then
			return num(c[path[2]](ctx, args))
		end
		return 0
	end
end

eval = function(node, ctx)
	local k = node.k
	if k == "num" then return node.v end
	if k == "str" then return node.v end
	if k == "name" then return eval_name(node, ctx) end
	if k == "call" then return eval_call(node, ctx) end
	if k == "un" then
		local v = eval(node.e, ctx)
		if node.op == "-" then return -num(v) end
		return (num(v) == 0) and 1 or 0   -- !
	end
	if k == "bin" then
		local op = node.op
		if op == "&&" then
			if num(eval(node.a, ctx)) == 0 then return 0 end
			return (num(eval(node.b, ctx)) ~= 0) and 1 or 0
		elseif op == "||" then
			if num(eval(node.a, ctx)) ~= 0 then return 1 end
			return (num(eval(node.b, ctx)) ~= 0) and 1 or 0
		elseif op == "??" then
			local a = eval(node.a, ctx)
			if a == nil then return eval(node.b, ctx) end
			return a
		end
		local a = eval(node.a, ctx)
		local b = eval(node.b, ctx)
		if op == "+" then return num(a) + num(b)
		elseif op == "-" then return num(a) - num(b)
		elseif op == "*" then return num(a) * num(b)
		elseif op == "/" then local d = num(b); if d == 0 then return 0 end; return num(a) / d
		elseif op == "==" then return (a == b) and 1 or 0
		elseif op == "!=" then return (a ~= b) and 1 or 0
		elseif op == "<"  then return (num(a) <  num(b)) and 1 or 0
		elseif op == "<=" then return (num(a) <= num(b)) and 1 or 0
		elseif op == ">"  then return (num(a) >  num(b)) and 1 or 0
		elseif op == ">=" then return (num(a) >= num(b)) and 1 or 0
		end
		return 0
	end
	if k == "tern" then
		if num(eval(node.c, ctx)) ~= 0 then return eval(node.a, ctx) end
		if node.b then return eval(node.b, ctx) end
		return 0
	end
	if k == "assign" then
		local head = node.path[1]
		local store = get_store(ctx, head)
		local val = eval(node.e, ctx)
		if store then
			local tbl, key = resolve_store_path(store, node.path, 2)
			if tbl then tbl[key] = val end
		end
		return val
	end
	if k == "return" then
		return eval(node.e, ctx), true
	end
	if k == "block" then
		local last = 0
		for i = 1, #node.stmts do
			local v, ret = eval(node.stmts[i], ctx)
			last = v
			if ret then return v end
		end
		return last
	end
	return 0
end

----------------------------------------------------------------------
-- Public API --------------------------------------------------------
----------------------------------------------------------------------
-- strip // line and /* */ block comments, respecting ' string literals
function M.strip_comments(input)
	local out, i, n = {}, 1, #input
	local inStr, inLine, inBlock = false, false, false
	while i <= n do
		local c = input:sub(i, i)
		if inStr then
			if c == "'" then inStr = false end
			out[#out + 1] = c
		elseif inLine then
			if c == "\r" or c == "\n" then inLine = false; out[#out + 1] = "\n" end
		elseif inBlock then
			if c == "*" and input:sub(i + 1, i + 1) == "/" then inBlock = false; i = i + 1 end
		elseif c == "'" then inStr = true; out[#out + 1] = "'"
		else
			local nx = input:sub(i + 1, i + 1)
			if c == "/" and nx == "/" then inLine = true; i = i + 1
			elseif c == "/" and nx == "*" then inBlock = true; i = i + 1
			else out[#out + 1] = c end
		end
		i = i + 1
	end
	return table.concat(out)
end

-- Compile an expression/script string into { eval = function(ctx) -> number } .
-- Parse failures fall back to a constant 0, mirroring MolangParser's behavior.
function M.compile(src, isScript)
	if type(src) == "number" then return { ast = { k = "num", v = src }, eval = function() return src end } end
	local ok, ast = pcall(function()
		local text = isScript and M.strip_comments(src) or src
		return new_parser(lex(text)):parse_program()
	end)
	if not ok then
		return { ast = { k = "num", v = 0 }, error = ast, eval = function() return 0 end }
	end
	return {
		ast = ast,
		eval = function(ctx)
			ctx = ctx or {}
			local v = eval(ast, ctx)
			return num(v)
		end,
	}
end

M.MATH = MATH
return M
