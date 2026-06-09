"""YSM 2.5.0 custom-function / script support (transpile route).

Reads a model's `functions/` directory (`.molang` files), transpiles each into a
standalone Lua function, and wires them into the YSM runtime:

  - plain functions            -> ysm_fn[<name>]   (callable as fn.<name>)
  - <name>@player_init.molang  -> run once on load
  - <name>@player_update.molang-> run every render frame (before anim)
  - <name>@sync.molang         -> ysm.sync(...) (LOCAL/HOST approximation)
  - <fn>@player_ctrl_<ctrl>    -> per-frame animation controller script

This is a *statement-level* compiler (the legacy molang2lua in lua_gen.py is
expression-only). It emits native Lua control flow so that `return`, `break`,
`continue`, `loop`, `for_each` and recursion behave like YSM.

Known intentional divergences from native YSM (documented for the user):
  - ysm.sync(...) cannot do YSM's server-authoritative cross-player networked
    sync inside Figura. It is routed through a Figura ping (host -> all viewers);
    true mutual P2P needs a dedicated shared module installed by every player.
  - Animation-controller `ctrl.state_bypass` has no YSM built-in controller to
    fall back to in Figura; it relinquishes control to the converter's own
    locomotion/base layers instead.
"""

import re
from pathlib import Path

from .lua_gen import _lua_str, _MATH


# ---------------------------------------------------------------------------
# Comment stripping (C-style // and /* */), string-aware
# ---------------------------------------------------------------------------

def _strip_comments(s):
    out = []
    i, n = 0, len(s)
    instr = False
    while i < n:
        c = s[i]
        if instr:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(s[i + 1]); i += 2; continue
            if c == "'":
                instr = False
            i += 1; continue
        if c == "'":
            instr = True; out.append(c); i += 1; continue
        if c == '/' and i + 1 < n and s[i + 1] == '/':
            while i < n and s[i] != '\n':
                i += 1
            continue
        if c == '/' and i + 1 < n and s[i + 1] == '*':
            i += 2
            while i + 1 < n and not (s[i] == '*' and s[i + 1] == '/'):
                i += 1
            i += 2; continue
        out.append(c); i += 1
    return ''.join(out)


def _split_top(src, sep=';'):
    """Split on a top-level separator, respecting (), [], {} and strings."""
    out, buf, depth, i, n = [], [], 0, 0, len(src)
    instr = False
    while i < n:
        c = src[i]
        if instr:
            buf.append(c)
            if c == '\\' and i + 1 < n:
                buf.append(src[i + 1]); i += 2; continue
            if c == "'":
                instr = False
            i += 1; continue
        if c == "'":
            instr = True; buf.append(c); i += 1; continue
        if c in '([{':
            depth += 1; buf.append(c); i += 1; continue
        if c in ')]}':
            depth -= 1; buf.append(c); i += 1; continue
        if depth == 0 and c == sep:
            s = ''.join(buf).strip()
            if s:
                out.append(s)
            buf = []; i += 1; continue
        buf.append(c); i += 1
    s = ''.join(buf).strip()
    if s:
        out.append(s)
    return out


def _find_top(src, ch, skip_double=False):
    """Index of first top-level occurrence of single char `ch`, else -1.
    If skip_double, ignore a doubled token (e.g. '??')."""
    depth, i, n = 0, 0, len(src)
    instr = False
    while i < n:
        c = src[i]
        if instr:
            if c == '\\':
                i += 2; continue
            if c == "'":
                instr = False
            i += 1; continue
        if c == "'":
            instr = True; i += 1; continue
        if c in '([{':
            depth += 1; i += 1; continue
        if c in ')]}':
            depth -= 1; i += 1; continue
        if depth == 0 and c == ch:
            if skip_double and ((i + 1 < n and src[i + 1] == ch) or (i > 0 and src[i - 1] == ch)):
                i += 1; continue
            return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# Expression compiler (Pratt). Emits a Lua *value* expression.
# ---------------------------------------------------------------------------

# Precedence mirrors YSM's BinaryExpression.Op ordering (rescaled to small ints):
#   NULL_COALESCE(1200) < CONDITIONAL/ternary(1400) < OR(1600) < AND(1800)
#   < EQ/NEQ(2000) < LT/LTE/GT/GTE(2200) < ADD/SUB(2400) < MUL/DIV(2600).
# Ternary '?' is handled inline with literal precedence 2; ASSIGN(1, lowest)
# is handled at statement level.
_PREC = {'??': 1, '||': 3, '&&': 4,
         '==': 5, '!=': 5, '<': 6, '<=': 6, '>': 6, '>=': 6,
         '+': 7, '-': 7, '*': 8, '/': 8, '%': 8}

_CTRL_CONST = {
    'ctrl.loop': "'__loop'", 'ctrl.play_once': "'__once'",
    'ctrl.hold_on_last_frame': "'__hold'",
    'ctrl.state_continue': "'__continue'", 'ctrl.state_pause': "'__pause'",
    'ctrl.state_stop': "'__stop'", 'ctrl.state_bypass': "'__bypass'",
}
_CTRL_FN_VALUE = {
    'ctrl.set_animation': 'ysm_ctrl_api.set_animation',
    'ctrl.set_beginning_transition_length': 'ysm_ctrl_api.set_trans',
    'ctrl.set_blend_progress': 'ysm_ctrl_api.set_trans',
}
_CTRL_FN_CALL = {
    'ctrl.indicate_reload': 'ysm_ctrl_api.reload()',
    'ctrl.reset': 'ysm_ctrl_api.reset()',
}
# ctrl.<pred> -> state predicate (1/0). Anything not listed -> ysm_ctrl_pred(name)


def _tok(s):
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in ' \t\r\n':
            i += 1; continue
        if c.isdigit() or (c == '.' and i + 1 < n and s[i + 1].isdigit()):
            j = i
            while j < n and (s[j].isdigit() or s[j] == '.'):
                j += 1
            toks.append(('num', s[i:j])); i = j; continue
        if c.isalpha() or c == '_':
            j = i
            while j < n and (s[j].isalnum() or s[j] == '_'):
                j += 1
            toks.append(('id', s[i:j])); i = j; continue
        if c == "'":
            j = i + 1
            while j < n and s[j] != "'":
                if s[j] == '\\':
                    j += 1
                j += 1
            j += 1
            toks.append(('str', s[i:j])); i = j; continue
        matched = False
        for op in ('->', '??', '<=', '>=', '==', '!=', '&&', '||'):
            if s[i:i + len(op)] == op:
                toks.append((op, op)); i += len(op); matched = True; break
        if matched:
            continue
        if c in '(){}[],;?:.+-*/%=!<>&|':
            toks.append((c, c)); i += 1; continue
        i += 1
    toks.append(('eof', ''))
    return toks


class _ExprParser:
    def __init__(self, toks):
        self.t = toks
        self.p = 0

    def peek(self):
        return self.t[self.p] if self.p < len(self.t) else ('eof', '')

    def next(self):
        tk = self.t[self.p]; self.p += 1; return tk

    def parse(self, minp=0):
        left = self.prefix()
        while True:
            tk = self.peek()
            k = tk[0]
            if k in ('eof', ';', ')', ']', '}', ',', ':'):
                break
            if k in ('.', '(', '['):
                left = self.postfix(left, k)
                continue
            if k == '?':
                # Ternary / conditional. YSM: CONDITIONAL(1400) is above
                # NULL_COALESCE(1200) and below OR(1600); right-associative.
                if 2 < minp:
                    break
                self.next()
                te = self.parse(0)
                if self.peek()[0] == ':':
                    self.next(); fe = self.parse(2)
                    left = "((function() if _truth(%s) then return %s else return %s end end)())" % (left, te, fe)
                else:
                    # `cond ? x` with no else: YSM returns x if truthy else null.
                    left = "((function() if _truth(%s) then return %s end end)())" % (left, te)
                continue
            prec = _PREC.get(k, 0)
            if prec == 0 or prec < minp:
                break
            self.next()
            right = self.parse(prec + 1)
            if k == '&&':
                left = "((_truth(%s) and _truth(%s)) and 1 or 0)" % (left, right)
            elif k == '||':
                left = "((_truth(%s) or _truth(%s)) and 1 or 0)" % (left, right)
            elif k == '??':
                left = "(function() local _a=%s; if _a==nil then return %s end return _a end)()" % (left, right)
            elif k == '==':
                # YSM eq: identity, else float-compare if either is a Number,
                # else false. Returns 1/0 (a Number, matching asFloat(bool)).
                left = "(_eq(%s, %s) and 1 or 0)" % (left, right)
            elif k == '!=':
                left = "((not _eq(%s, %s)) and 1 or 0)" % (left, right)
            elif k in ('<', '<=', '>', '>='):
                # YSM compares via asFloat on both operands.
                left = "((_num(%s) %s _num(%s)) and 1 or 0)" % (left, k, right)
            elif k == '/':
                # YSM: division by zero is defined to be 0.
                left = "_div(%s, %s)" % (left, right)
            else:
                left = "(_num(%s) %s _num(%s))" % (left, k, right)
        return left

    def prefix(self):
        tk = self.peek()
        k = tk[0]
        if k == 'num':
            self.next(); return tk[1]
        if k == 'str':
            self.next(); return _lua_str(tk[1].strip("'"))
        if k == '(':
            self.next(); e = self.parse(0)
            if self.peek()[0] == ')':
                self.next()
            return '(' + e + ')'
        if k == '-':
            self.next(); return '(-_num(' + self.parse(8) + '))'
        if k == '!':
            self.next(); return '((not _truth(' + self.parse(8) + ')) and 1 or 0)'
        if k == '{':
            # expression-position block: IIFE returning last value
            self.next()
            exprs = []
            while self.peek()[0] not in ('}', 'eof'):
                exprs.append(self.parse(0))
                if self.peek()[0] == ';':
                    self.next()
            if self.peek()[0] == '}':
                self.next()
            return "(function() return %s end)()" % (exprs[-1] if exprs else 'nil')
        if k == 'id':
            return self.ident()
        # unknown
        self.next(); return 'nil'

    def ident(self):
        parts = [self.next()[1]]
        while self.peek()[0] == '.':
            self.next()
            if self.peek()[0] == 'id':
                parts.append(self.next()[1])
            else:
                break
        full = '.'.join(parts)
        low = full.lower()
        # fn.<name> : consume optional call args inline -> ysm_fn_call
        if low.startswith('fn.'):
            name = full.split('.', 1)[1].lower()
            args = []
            if self.peek()[0] == '(':
                self.next()
                while self.peek()[0] != ')':
                    args.append(self.parse(0))
                    if self.peek()[0] == ',':
                        self.next()
                    elif self.peek()[0] != ')':
                        break
                if self.peek()[0] == ')':
                    self.next()
            joined = ('' if not args else (',' + ','.join(args)))
            return "ysm_fn_call(%s%s)" % (_lua_str(name), joined)
        return self.map_id(full)

    def map_id(self, full):
        low = full.lower()
        if full in ('true', 'false'):
            return full
        if full == 'args':
            return 'args'
        if full in _MATH:
            return _MATH[full]
        if full in _CTRL_CONST:
            return _CTRL_CONST[full]
        if full in _CTRL_FN_VALUE:
            return _CTRL_FN_VALUE[full]
        if full in _CTRL_FN_CALL:
            return _CTRL_FN_CALL[full]
        if low.startswith('ctrl.'):
            return "ysm_ctrl_pred(%s)" % _lua_str(low.split('.', 1)[1])
        if full.startswith('query.') or full.startswith('q.'):
            qn = full.split('.', 1)[1].replace('.', '_')
            return 'ysm_q.' + qn + '()'
        if full.startswith('variable.') or full.startswith('v.'):
            return "ysm_v['" + full.split('.', 1)[1] + "']"
        if full.startswith('context.') or full.startswith('c.'):
            return "ysm_c['" + full.split('.', 1)[1] + "']"
        if full.startswith('temp.') or full.startswith('t.'):
            return "_T['" + full.split('.', 1)[1] + "']"
        return full

    def postfix(self, left, k):
        if k == '.':
            self.next(); name = self.next()[1]
            return left + '.' + name
        if k == '(':
            self.next(); args = []
            while self.peek()[0] != ')':
                args.append(self.parse(0))
                if self.peek()[0] == ',':
                    self.next()
                elif self.peek()[0] != ')':
                    break
            if self.peek()[0] == ')':
                self.next()
            return left + '(' + ','.join(args) + ')'
        if k == '[':
            self.next(); idx = self.parse(0)
            if self.peek()[0] == ']':
                self.next()
            return '(((%s) or {})[%s])' % (left, idx)
        return left


def compile_value(expr):
    expr = expr.strip()
    if not expr:
        return 'nil'
    try:
        p = _ExprParser(_tok(expr))
        return p.parse(0)
    except Exception:
        return '0'


# ---------------------------------------------------------------------------
# Statement compiler. Emits native Lua statements.
# ---------------------------------------------------------------------------

class _StmtCompiler:
    def __init__(self):
        self.loop_id = 0
        self.loop_stack = []

    def compile_seq(self, src):
        out = []
        for st in _split_top(src, ';'):
            out.append(self.compile_stmt(st))
        return '\n'.join(x for x in out if x)

    def compile_block(self, part):
        part = part.strip()
        if part.startswith('{') and part.endswith('}'):
            return self.compile_seq(part[1:-1])
        return self.compile_stmt(part)

    def compile_lhs(self, lhs):
        lhs = lhs.strip()
        low = lhs.lower()
        if low == 'args' or low.startswith('args'):
            return None  # not assignable
        if lhs.startswith('temp.') or lhs.startswith('t.'):
            return "_T['" + lhs.split('.', 1)[1] + "']"
        if lhs.startswith('variable.') or lhs.startswith('v.'):
            return "ysm_v['" + lhs.split('.', 1)[1] + "']"
        if lhs.startswith('context.') or lhs.startswith('c.'):
            return "ysm_c['" + lhs.split('.', 1)[1] + "']"
        if lhs.startswith('ysm.'):
            return lhs
        return None

    def compile_stmt(self, s):
        s = s.strip()
        if not s:
            return ''
        if s == 'break':
            return 'break'
        if s == 'continue':
            return ('goto %s' % self.loop_stack[-1]) if self.loop_stack else ''
        if s == 'return':
            return 'do return nil end'
        if re.match(r'^return\b', s):
            e = s[len('return'):].strip()
            return 'do return %s end' % (compile_value(e) if e else 'nil')
        # loop(count, {body})
        m = re.match(r'^loop\s*\((.*)\)\s*$', s, re.S)
        if m:
            args = _split_top(m.group(1), ',')
            if len(args) >= 2:
                cnt = compile_value(args[0])
                body = ','.join(args[1:]).strip()
                return self._emit_loop("for __i=1,math.min(math.floor(_num(%s)),1024) do" % cnt, body)
        # for_each(var, array, {body})
        m = re.match(r'^for_each\s*\((.*)\)\s*$', s, re.S)
        if m:
            args = _split_top(m.group(1), ',')
            if len(args) >= 3:
                var = args[0].strip()
                arr = compile_value(args[1])
                body = ','.join(args[2:]).strip()
                lhs = self.compile_lhs(var) or "_T['_fe']"
                lid = self._new_loop()
                inner = self._wrap_body(body, lid)
                return ("do local __fa=%s if type(__fa)=='table' then "
                        "local __lo=(__fa[0]~=nil) and 0 or 1 "
                        "local __hi=__fa.n and (__fa.n-1) or #__fa "
                        "for __k=__lo,__hi do %s=__fa[__k]\n%s\n::%s:: end end end"
                        % (arr, lhs, inner, lid))
        # Assignment has YSM's lowest precedence (ASSIGN=1), so a top-level '='
        # binds looser than a ternary on its right: `v.x = a ? b : c` parses as
        # `v.x = (a ? b : c)`. Detect assignment first whenever its '=' precedes
        # any top-level '?'.
        ai = self._find_assign(s)
        qi = _find_top(s, '?', skip_double=True)
        if ai >= 0 and (qi < 0 or ai < qi):
            lhs = self.compile_lhs(s[:ai])
            rhs = compile_value(s[ai + 1:])
            if lhs:
                return '%s = %s' % (lhs, rhs)
            return 'local _ = %s' % rhs
        # top-level ternary as control:  cond ? then [: else]
        # Kept at statement level (not an expression IIFE) so that `return`,
        # `break`, and `continue` inside {…} branches propagate like YSM scopes.
        if qi >= 0:
            cond = s[:qi].strip()
            rest = s[qi + 1:].strip()
            ci = _find_top(rest, ':')
            if ci >= 0:
                then_p = rest[:ci].strip()
                else_p = rest[ci + 1:].strip()
                out = 'if _truth(%s) then\n%s\n' % (compile_value(cond), self.compile_block(then_p))
                out += 'else\n%s\nend' % self.compile_block(else_p)
                return out
            return 'if _truth(%s) then\n%s\nend' % (compile_value(cond), self.compile_block(rest))
        # bare expression (call, fn.x, q.debug_output, ctrl.* etc.)
        return 'local _ = %s' % compile_value(s)

    def _find_assign(self, s):
        depth, i, n = 0, 0, len(s)
        instr = False
        while i < n:
            c = s[i]
            if instr:
                if c == '\\':
                    i += 2; continue
                if c == "'":
                    instr = False
                i += 1; continue
            if c == "'":
                instr = True; i += 1; continue
            if c in '([{':
                depth += 1; i += 1; continue
            if c in ')]}':
                depth -= 1; i += 1; continue
            if depth == 0 and c == '=':
                nxt = s[i + 1] if i + 1 < n else ''
                prv = s[i - 1] if i > 0 else ''
                if nxt != '=' and prv not in '=<>!':
                    return i
            i += 1
        return -1

    def _new_loop(self):
        self.loop_id += 1
        return '__cont%d' % self.loop_id

    def _wrap_body(self, body, lid):
        self.loop_stack.append(lid)
        inner = self.compile_block(body)
        self.loop_stack.pop()
        return 'do\n%s\nend' % inner

    def _emit_loop(self, header, body):
        lid = self._new_loop()
        inner = self._wrap_body(body, lid)
        return '%s\n%s\n::%s::\nend' % (header, inner, lid)


def compile_function_body(src):
    src = _strip_comments(src)
    c = _StmtCompiler()
    return c.compile_seq(src)


# ---------------------------------------------------------------------------
# Loader + Lua emission
# ---------------------------------------------------------------------------

SCRIPT_RUNTIME = r"""
-- ===== YSM custom-function (script) runtime =====
-- Value coercion mirrors YSM ValueConversions:
--   asFloat: nil->0, NaN->0, bool->1/0, number->itself, (non-numeric string->0)
--   asBoolean: nil/false->false, NaN->false, 0->false, else true
function _num(x) if type(x)=='number' then if x~=x then return 0 end return x end if x==true then return 1 end if x==nil or x==false then return 0 end local n=tonumber(x); return n or 0 end
function _truth(x) if x==nil or x==false or x==0 then return false end if x==ysm_safezero then return false end if type(x)=='number' and x~=x then return false end return true end
-- YSM: division by zero == 0.
function _div(a,b) a=_num(a) b=_num(b) if b==0 then return 0 end return a/b end
-- YSM eq: identity, else float-compare when either side is a Number, else false.
function _eq(a,b) if a==b then return true end if type(a)=='number' or type(b)=='number' then return _num(a)==_num(b) end return false end
ysm_fn={}
ysm_fn_depth=0
function ysm_fn_call(name,...)
  local f=ysm_fn[name]
  if not f then return nil end
  if ysm_fn_depth>=32 then return nil end
  ysm_fn_depth=ysm_fn_depth+1
  local nn=select('#',...)
  local a={n=nn}
  for i=1,nn do a[i-1]=select(i,...) end
  local ok,rv=pcall(f,a)
  ysm_fn_depth=ysm_fn_depth-1
  if ok then return rv end
  return nil
end
ysm_q.debug_output=function() return function(...) local s='' for i=1,select('#',...) do s=s..tostring((select(i,...))) end pcall(function() if host:isHost() then print(s) end end) end end
ysm_q.all_animations_finished=function() local c=ysm_ctrl_cur; if c and c._cur then local a=ysm_anim_get(c._cur); if a then local ok,pl=pcall(function() return ysm_anim_playing(a) end); return (ok and pl) and 0 or 1 end end return 1 end

-- ysm.sync : YSM does server-authoritative cross-player sync, which Figura has
-- no direct equivalent for. The closest native mechanism is a Figura ping,
-- which propagates from the avatar's host to every client viewing that avatar.
-- We route @sync dispatch through a ping so it reaches all viewers (host ->
-- viewers). True mutual P2P between arbitrary players would require a dedicated
-- shared Lua module installed by every participant, which is out of scope here.
ysm._sync_fns={}
local function _ysm_sync_dispatch(a)
  for _,f in ipairs(ysm._sync_fns) do pcall(f,a) end
end
if pings then
  function pings.ysm_sync(a) _ysm_sync_dispatch(a) end
end
ysm.sync=function(...)
  local nn=math.min(select('#',...),16)
  local a={n=nn}
  for i=1,nn do a[i-1]=select(i,...) end
  local oh,ish=pcall(function() return host:isHost() end)
  if oh and ish and pings and pings.ysm_sync then
    pcall(function() pings.ysm_sync(a) end)  -- broadcast host -> all viewers
  else
    _ysm_sync_dispatch(a)                     -- non-host / no ping: local best-effort
  end
  return nil
end

-- ===== script-driven animation controllers =====
ysm_script_ctrls={}
ysm_ctrl_cur=nil
ysm_ctrl_api={}
function ysm_ctrl_api.set_animation(a,lt) local c=ysm_ctrl_cur; if c then c._set=a; c._lt=lt end end
function ysm_ctrl_api.set_trans(t) local c=ysm_ctrl_cur; if c then c._trans=t end end
function ysm_ctrl_api.reload() local c=ysm_ctrl_cur; if c then c._reload=true end end
function ysm_ctrl_api.reset() local c=ysm_ctrl_cur; if c then c._reload=true; c._reset=true; c._stopnow=true end end
local function _qb(fn) local ok,v=pcall(fn); return (ok and v) and 1 or 0 end
function ysm_ctrl_pred(k)
  if k=='idle' then return _qb(function() return not player:isMoving() and player:isOnGround() end) end
  if k=='walk' then return _qb(function() return player:isMoving() and player:isOnGround() and not player:isSprinting() end) end
  if k=='run' or k=='sprint' then return _qb(function() return player:isSprinting() end) end
  if k=='sneak' or k=='sneaking' then return _qb(function() return player:isSneaking() end) end
  if k=='swim' or k=='swimming' then return _qb(function() return player:isVisuallySwimming() end) end
  if k=='fly' or k=='flying' or k=='gliding' then return _qb(function() return player:isGliding() end) end
  if k=='jump' or k=='jumping' then return _qb(function() return host:isJumping() end) end
  if k=='fall' or k=='falling' then return _qb(function() local v=player:getVelocity(); return (not player:isOnGround()) and v.y<-0.1 end) end
  if k=='on_ground' then return _qb(function() return player:isOnGround() end) end
  if k=='in_water' then return _qb(function() return player:isInWater() end) end
  if k=='climb' or k=='climbing' then return _qb(function() return player:isClimbing() end) end
  if k=='ride' or k=='riding' then return _qb(function() return player:getVehicle()~=nil end) end
  if k=='use' or k=='using_item' then return _qb(function() return player:isUsingItem() end) end
  if k=='sleep' or k=='sleeping' then return _qb(function() return player:getPose()=='SLEEPING' end) end
  return 0
end
function ysm_script_ctrl_tick(c)
  -- Faithful to YSM PredicateBasedController: ctrl.set_animation() is a SIDE EFFECT
  -- that sets a persistent requested animation (independent of the returned state),
  -- which YSM applies/advances via process(). The returned state only governs
  -- continue/stop/pause/bypass of playback -- it does NOT gate whether the
  -- requested animation plays. Critically the request PERSISTS across ticks
  -- (YSM lastRequestedAnimation), so a controller may set_animation on one tick
  -- and simply return state_continue on later ticks (e.g. the random-blink).
  ysm_ctrl_cur=c
  -- Per-tick transient flags only. c._set / c._cur PERSIST across ticks.
  c._set=nil; c._lt=nil; c._trans=nil; c._reload=false; c._reset=false; c._stopnow=false
  local ok,state=pcall(c.fn, {n=0})
  ysm_ctrl_cur=nil
  if not ok then return end
  local function stopcur()
    if c._cur then local a=ysm_anim_get(c._cur); if a then pcall(function() a:stop() end) end; c._cur=nil end
  end
  if state=='__pause' then
    if c._cur then local a=ysm_anim_get(c._cur); if a then pcall(function() a:pause() end) end end
    return
  end
  local want=c._set
  -- A set_animation() call this tick => apply it as a persistent request (YSM side effect).
  if want then
    local a=ysm_anim_get(want)
    if a then
      local playing=false
      if want==c._cur then local ok2,pl=pcall(function() return ysm_anim_playing(a) end); playing=ok2 and pl end
      -- (Re)start when the requested animation changed, finished, or a reload was requested.
      if want~=c._cur or (not playing) or c._reload then
        if c._cur and c._cur~=want then local pa=ysm_anim_get(c._cur); if pa then pcall(function() pa:stop() end) end end
        if c._trans then pcall(function() a:setBlendTime((c._trans or 0)*20,(c._trans or 0)*20) end) end
        local lt=c._lt
        if lt=='__once' then pcall(function() a:setLoop('ONCE') end)
        elseif lt=='__hold' then pcall(function() a:setLoop('HOLD') end)
        elseif lt=='__loop' then pcall(function() a:setLoop('LOOP') end) end
        pcall(function() a:stop():setPriority(2):play() end)
        c._cur=want
      end
    end
    return
  end
  -- No set_animation() this tick: the previously requested animation persists.
  if state==nil or state=='__bypass' then stopcur(); return end
  if state=='__stop' then
    -- Let the current animation stop; the persistent request (if any) was already
    -- handled above when set this tick. With no new request, relinquish.
    stopcur(); return
  end
  -- __continue with no new request: keep the current animation playing per its own
  -- loop mode (Figura advances it). Do not force-replay finished play-once anims.
  return
end
"""

_EVENTS = ('player_init', 'player_update', 'sync')


def _safe_key(name):
    return name.lower()


def load_functions(ysm_path, files_section):
    """Return list of dicts: {name, event, ctrl, body_src}."""
    fpath = (files_section or {}).get('function_path', 'functions')
    root = Path(ysm_path) / fpath
    if not root.exists():
        return []
    out = []
    for f in sorted(root.rglob('*.molang')):
        if f.name.endswith('.molang.txt'):
            continue
        stem = f.name[:-len('.molang')]
        event = None
        ctrl = None
        if '@' in stem:
            fname, ev = stem.split('@', 1)
            event = ev.strip()
            if event.lower().startswith('player_ctrl_'):
                ctrl = event[len('player_ctrl_'):]
                event = 'player_ctrl'
        else:
            fname = stem
        try:
            src = f.read_bytes().decode('utf-8', errors='replace')
        except Exception:
            continue
        out.append({
            'name': _safe_key(fname) if fname else _safe_key(stem),
            'raw': stem,
            'event': event,
            'ctrl': ctrl,
            'src': src,
        })
    return out


def gen_scripts(ysm_path, files_section):
    """Return (lua_block_str, summary_dict)."""
    fns = load_functions(ysm_path, files_section)
    if not fns:
        return '', {'total': 0}

    lines = [SCRIPT_RUNTIME]
    init_keys, update_keys, sync_keys, ctrls = [], [], [], []
    n_plain = n_ctrl = 0
    compiled_ok = 0

    for i, fn in enumerate(fns):
        key = 'f%d' % i
        try:
            body = compile_function_body(fn['src'])
            compiled_ok += 1
        except Exception as e:
            body = '-- compile error: %s' % str(e).replace('\n', ' ')
        # Each function: fresh per-call temp scope _T (recursion-safe via Lua locals)
        lua_fn = ("ysm_fn[%s]=function(args)\n"
                  "  args = args or {n=0}\n"
                  "  local _T=setmetatable({},{__index=function() return 0 end})\n"
                  "%s\n  return nil\nend\n") % (_lua_str(fn['name']), body)
        lines.append("-- %s" % fn['raw'])
        lines.append(lua_fn)
        ev = fn['event']
        if ev == 'player_init':
            init_keys.append(fn['name'])
        elif ev == 'player_update':
            update_keys.append(fn['name'])
        elif ev == 'sync':
            sync_keys.append(fn['name'])
        elif ev == 'player_ctrl':
            n_ctrl += 1
            cvar = 'ysm_sctrl_%d' % i
            lines.append("%s={fn=ysm_fn[%s],target=%s}" % (cvar, _lua_str(fn['name']), _lua_str(fn['ctrl'] or '')))
            lines.append("table.insert(ysm_script_ctrls,%s)" % cvar)
            ctrls.append(fn['ctrl'] or '')
        else:
            n_plain += 1

    # register @sync functions
    for k in sync_keys:
        lines.append("table.insert(ysm._sync_fns, ysm_fn[%s])" % _lua_str(k))

    # run @player_init once on load
    if init_keys:
        lines.append("events.ENTITY_INIT:register(function()")
        for k in init_keys:
            lines.append("  pcall(ysm_fn[%s], {n=0})" % _lua_str(k))
        lines.append("end)")

    # run @player_update + controllers every render frame
    if update_keys or ctrls:
        lines.append("events.WORLD_RENDER:register(function()")
        for k in update_keys:
            lines.append("  pcall(ysm_fn[%s], {n=0})" % _lua_str(k))
        if ctrls:
            lines.append("  for _,__c in ipairs(ysm_script_ctrls) do ysm_script_ctrl_tick(__c) end")
        lines.append("end)")

    summary = {
        'total': len(fns), 'compiled': compiled_ok,
        'plain': n_plain, 'init': len(init_keys), 'update': len(update_keys),
        'sync': len(sync_keys), 'controllers': n_ctrl,
    }
    return '\n'.join(lines), summary
