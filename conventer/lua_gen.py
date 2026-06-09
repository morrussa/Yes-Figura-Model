"""MoLang -> Lua transpiler + YSM runtime Lua generator."""

import json
from pathlib import Path

# =============================================================================
# MoLang → Lua Transpiler (Pratt parser)
# =============================================================================

def _tokenize(s):
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in ' \t\r\n': i += 1; continue
        # NOTE: never fold a leading '-' into a numeric literal. MoLang '-' is an
        # operator (unary or binary); folding it broke subtraction, e.g.
        # 'x*550-45' -> num(550), num(-45) (lost the subtraction) and
        # 'cos(x)*-25-50' silently dropped the trailing '-50'. The parser already
        # handles unary minus in prefix position, so emit bare numbers only.
        if c.isdigit() or (c == '.' and i + 1 < n and s[i+1].isdigit()):
            j = i
            while j < n and (s[j].isdigit() or s[j] == '.'): j += 1
            toks.append(('num', s[i:j])); i = j; continue
        if c.isalpha() or c == '_':
            j = i
            while j < n and (s[j].isalnum() or s[j] == '_'): j += 1
            toks.append(('id', s[i:j])); i = j; continue
        if c == "'":
            j = i + 1
            while j < n and s[j] != "'":
                if s[j] == '\\': j += 1
                j += 1
            j += 1
            toks.append(('str', s[i:j])); i = j; continue
        for op in ('->', '??', '<=', '>=', '==', '!=', '&&', '||'):
            if s[i:i+len(op)] == op:
                toks.append((op, op)); i += len(op); break
        else:
            if c in '(){}[],;?:.+-*/%=!<>&|':
                toks.append((c, c)); i += 1; continue
            toks.append(('?', c)); i += 1
    toks.append(('eof', ''))
    return toks

_PREC = {'=': 1, '??': 2, '||': 3, '&&': 4,
         '==': 5, '!=': 5, '<': 6, '<=': 6, '>': 6, '>=': 6,
         '+': 7, '-': 7, '*': 8, '/': 8, '%': 8}

_MATH = {
    "math.floor":"ysm_math.floor","math.round":"ysm_math.round",
    "math.ceil":"ysm_math.ceil","math.trunc":"ysm_math.trunc",
    "math.clamp":"ysm_math.clamp","math.max":"ysm_math.max",
    "math.min":"ysm_math.min","math.abs":"ysm_math.abs",
    "math.exp":"ysm_math.exp","math.ln":"ysm_math.ln",
    "math.sqrt":"ysm_math.sqrt","math.mod":"ysm_math.mod",
    "math.pow":"ysm_math.pow","math.sin":"ysm_math.sin",
    "math.cos":"ysm_math.cos","math.acos":"ysm_math.acos",
    "math.asin":"ysm_math.asin","math.atan":"ysm_math.atan",
    "math.atan2":"ysm_math.atan2",
    "math.lerp":"ysm_math.lerp","math.lerprotate":"ysm_math.lerprotate",
    "math.random":"ysm_math.random","math.random_integer":"ysm_math.random_integer",
    "math.die_roll":"ysm_math.die_roll","math.die_roll_integer":"ysm_math.die_roll_integer",
    "math.hermite_blend":"ysm_math.hermite_blend","math.min_angle":"ysm_math.min_angle",
    "math.pi":"ysm_pi","math.e":"ysm_e",
    "loop":"ysm_loop","for_each":"ysm_for_each",
    "ysm.head_pitch":"ysm.head_pitch()",
    "ysm.head_yaw":"ysm.head_yaw()",
    "ysm.second_order":"ysm.second_order",
    "ysm.first_order":"ysm.first_order",
    "ysm.bone_rot":"ysm.bone_rot",
    "ysm.bone_pos":"ysm.bone_pos",
    "ysm.bone_scale":"ysm.bone_scale",
}

def _lua_str(s):
    # Emit a safe double-quoted Lua string. The old [[...]] long-bracket form
    # broke whenever the literal contained '[' or ']' (e.g. CJK comment
    # keyframes like "取值[0,5]"), producing invalid Lua -> hard keyframe
    # compile error -> scriptError -> the whole avatar fails to load.
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return '"' + s + '"'

def molang2lua(expr):
    # NOTE: expression-only KEYFRAME molang transpiler. It intentionally does NOT
    # resolve user-function calls `fn.<name>(...)` -- those are handled by the
    # statement-level compiler in script_gen.py (functions/*.molang). Per the YSM
    # grammar a keyframe expression *could* legally call fn.*, but no known model
    # does so in keyframes, so it is left unsupported here on purpose.
    if not expr or not expr.strip():
        return "nil"
    toks = _tokenize(expr)
    pos = [0]
    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else ('eof','')
    def consume(t=None):
        t2 = toks[pos[0]]; pos[0] += 1
        if t and t2[0] != t: raise ValueError(f"expected {t}, got {t2[0]}({t2[1]})")
        return t2

    def parse_expr(minp=0):
        t = peek()
        if t[0] == 'eof': return 'nil'
        # prefix
        if t[0] == 'num':
            consume(); left = t[1]
        elif t[0] == 'str':
            consume()
            left = _lua_str(t[1].strip("'"))
        elif t[0] == 'id':
            left = _parse_id()
        elif t[0] == '(':
            consume(); left = parse_expr(0)
            if peek()[0] == ')': consume()
        elif t[0] == '-':
            consume(); left = "(-_num(" + parse_expr(8) + "))"
        elif t[0] == '!':
            consume(); left = "((not _truth(" + parse_expr(8) + ")) and 1 or 0)"
        elif t[0] == '{':
            consume()
            exprs = []
            while peek()[0] != '}':
                if peek()[0] == 'eof': break
                exprs.append(parse_expr(0))
                if peek()[0] == ';': consume()
            if peek()[0] == '}': consume()
            left = "(function() return " + (exprs[-1] if exprs else 'nil') + " end)()"
        else:
            consume(); left = 'nil'
        # infix
        while peek()[0] != 'eof':
            t = peek()
            if t[0] in (';', ')'): break
            prec = _PREC.get(t[0], 0)
            # structural tokens (function call, property access, indexing) always bind tighter
            if t[0] not in ('.', '(', '[') and prec < minp: break
            if t[0] == '.':
                consume(); prop = consume('id')[1]
                left = _dot(left, prop)
            elif t[0] == '(':
                consume(); args = []
                while peek()[0] != ')':
                    if args and peek()[0] == ',': consume()
                    args.append(parse_expr(0))
                if peek()[0] == ')': consume()
                left = left + "(" + ','.join(args) + ")"
            elif t[0] == '[':
                consume(); idx = parse_expr(0)
                if peek()[0] == ']': consume()
                left = "(((" + left + " or {})[" + idx + "]))"
            elif t[0] == '?':
                consume()
                if peek()[0] == '??':
                    # ?? after ? shouldn't happen, just treat as ?
                    consume(); right = parse_expr(prec)
                    left = "(((nil_or(" + left + ")) or " + right + "))"
                else:
                    true_e = parse_expr(0)
                    if peek()[0] == ':':
                        consume(); false_e = parse_expr(prec)
                        left = "((function() if _truth(" + left + ") then return " + true_e + " else return " + false_e + " end end)())"
                    else:
                        left = "((function() if _truth(" + left + ") then return " + true_e + " end end)())"
            elif t[0] == '=':
                consume(); right = parse_expr(prec)
                if left.startswith('ysm_v[') or left.startswith('ysm_c[') or left.startswith('ysm_t['):
                    left = "(function() " + left + "=" + right + ";return " + right + " end)()"
                else:
                    left = "(_assign(" + left + "," + right + "))"
            elif t[0] == '->':
                consume(); right = parse_expr(prec)
                left = right
            elif t[0] == '??':
                consume(); right = parse_expr(prec)
                left = "(((nil_or(" + left + ")) or " + right + "))"
            elif t[0] == '&&':
                consume(); right = parse_expr(prec)
                left = "((_truth(" + left + ") and _truth(" + right + ")) and 1 or 0)"
            elif t[0] == '||':
                consume(); right = parse_expr(prec)
                left = "((_truth(" + left + ") or _truth(" + right + ")) and 1 or 0)"
            elif t[0] in ('==','!=','<','<=','>','>='):
                op = t[0]; consume(); right = parse_expr(prec)
                if op == '==':
                    left = "(_eq(" + left + "," + right + ") and 1 or 0)"
                elif op == '!=':
                    left = "((not _eq(" + left + "," + right + ")) and 1 or 0)"
                else:
                    left = "((_num(" + left + ") " + op + " _num(" + right + ")) and 1 or 0)"
            elif t[0] in ('+','-','*','/','%'):
                op = t[0]; consume(); right = parse_expr(prec)
                if op == '/':
                    left = "_div(" + left + "," + right + ")"
                else:
                    left = "(_num(" + left + ") " + op + " _num(" + right + "))"
            else:
                break
        return left

    def _parse_id():
        t = consume('id')
        parts = [t[1]]
        while peek()[0] == '.':
            consume(); parts.append(consume('id')[1])
        full = '.'.join(parts)
        if full in ('true','false'): return full
        if full in _MATH: return _MATH[full]
        if full.startswith("query.") or full.startswith("q."):
            qn = full.split('.',1)[1].replace('.','_')
            return "ysm_q." + qn + "()"
        if full.startswith("variable.") or full.startswith("v."):
            return "ysm_v['" + full.split('.',1)[1] + "']"
        if full.startswith("context.") or full.startswith("c."):
            return "ysm_c['" + full.split('.',1)[1] + "']"
        if full.startswith("temp.") or full.startswith("t."):
            return "ysm_t['" + full.split('.',1)[1] + "']"
        return full

    def _dot(left, prop):
        if left.startswith('ysm_q.'):
            base = left[:-2]
            return base + prop + "()"
        return left + "." + prop

    try:
        return parse_expr(0)
    except Exception:
        return "0"

# =============================================================================
#  Lua code generator
# =============================================================================

YSM_RUNTIME_HEAD = """-- YSM Runtime for Figura 1.21.3 (auto-generated)
ysm_pi = math.pi
ysm_e = math.exp(1)
ysm_math = setmetatable({pow=function(a,b)return (a or 0)^(b or 0)end,
  round=function(x)return math.floor((x or 0)+0.5)end,
  mod=function(a,b)b=b or 0;if b==0 then return 0 end;return (a or 0)%b end,
  trunc=function(x)return x>=0 and math.floor(x)or math.ceil(x)end,
  clamp=function(v,l,h)return math.max(l,math.min(h,v))end,
  lerp=function(a,b,t)return a+(b-a)*t end,
  lerprotate=function(a,b,t)local d=(b-a)%360;if d>180 then d=d-360 end;return a+d*t end,
  random_integer=math.random,die_roll=function(n,s)local r=0;for i=1,n do r=r+math.random(s)end;return r end,
  die_roll_integer=function(n,s)return ysm_math.die_roll(n,s)end,
  hermite_blend=function(t)return t*t*(3-2*t)end,
  min_angle=function(a)return((a+180)%360)-180 end,
  sin=function(x)return math.sin((x or 0)*ysm_pi/180)end,
  cos=function(x)return math.cos((x or 0)*ysm_pi/180)end,
  tan=function(x)return math.tan((x or 0)*ysm_pi/180)end,
  asin=function(x)return math.asin(x or 0)*180/ysm_pi end,
  acos=function(x)return math.acos(x or 0)*180/ysm_pi end,
  atan=function(x)return math.atan(x or 0)*180/ysm_pi end,
  atan2=function(y,x)return((math.atan2 or math.atan)(y or 0,x or 0))*180/ysm_pi end},{__index=math})
ysm_loop=function(c,f)c=math.min(math.floor(c or 0),1024);for i=1,c do f()end end
ysm_for_each=function()end
nil_or=function(v)return v end
_assign=function(t,v)return v end
_num=function(x) if type(x)=='number' then if x~=x then return 0 end return x end if x==true then return 1 end if x==nil or x==false then return 0 end local n=tonumber(x); return n or 0 end
_truth=function(x) if x==nil or x==false or x==0 then return false end if x==ysm_safezero then return false end if type(x)=='number' and x~=x then return false end return true end
_div=function(a,b) a=_num(a) b=_num(b) if b==0 then return 0 end return a/b end
_eq=function(a,b) if a==b then return true end if type(a)=='number' or type(b)=='number' then return _num(a)==_num(b) end return false end

ysm_q={}
ysm_q.anim_time=function() if ysm_state._kf_at~=nil then return ysm_state._kf_at end local c=ysm_ctrl_cur if c and c._cur then local _at=animations[ysm_model] local _a=_at and _at[c._cur] if _a then local ok,t=pcall(function() return _a:getTime() end) if ok and type(t)=='number' then return t end end end return 0.0 end
ysm_q.life_time=function()return ysm_state.lt end
ysm_q.time_of_day=function()return world.getTime()%24000/24000 end
ysm_q.delta_time=function()return ysm_state.dt end
ysm_q.is_moving=function()local ok,v=pcall(function()return player:isMoving()end)return ok and v or false end
ysm_q.is_sprinting=function()local ok,v=pcall(function()return player:isSprinting()end)return ok and v or false end
ysm_q.is_sneaking=function()local ok,v=pcall(function()return player:isSneaking()end)return ok and v or false end
ysm_q.is_on_ground=function()local ok,v=pcall(function()return player:isOnGround()end)return ok and v or false end
ysm_q.is_in_water=function()local ok,v=pcall(function()return player:isInWater()end)return ok and v or false end
ysm_q.is_swimming=function()local ok,v=pcall(function()return player:isVisuallySwimming()end)return ok and v or false end
ysm_q.is_jumping=function()local ok,v=pcall(function()return host:isJumping()end)return ok and v or false end
ysm_q.is_flying=function()local ok,v=pcall(function()return player:isGliding()end)return ok and v or false end
ysm_q.is_on_fire=function()local ok,v=pcall(function()return player:isOnFire()end)return ok and v or false end
ysm_q.is_riding=function()local ok,v=pcall(function()return player:getVehicle()end)return ok and v~=nil end
ysm_q.is_spectator=function()local ok,v=pcall(function()return player:getGamemode()end)return ok and v=='spectator' end
ysm_q.is_first_person=function()local ok,v=pcall(function()return renderer:isFirstPerson()end)return ok and v or false end
ysm_q.is_alive=function()local ok,v=pcall(function()return player:isAlive()end)return ok and v or false end
ysm_q.is_sleeping=function()local ok,v=pcall(function()return player:isAlive()end)return ok and not v end
ysm_q.is_eating=function()local ok,v=pcall(function()return player:isUsingItem()end)return ok and v or false end
ysm_q.is_using_item=function()local ok,v=pcall(function()return player:isUsingItem()end)return ok and v or false end
ysm_q.health=function()local ok,v=pcall(function()return player:getHealth()end)return ok and v or 0 end
ysm_q.max_health=function()local ok,v=pcall(function()return player:getMaxHealth()end)return ok and v or 0 end
ysm_q.hurt_time=function()return ysm_state._hurt or 0 end
ysm_q.player_level=function()local ok,v=pcall(function()return player:getExperienceLevel()end)return ok and v or 0 end
ysm_q.ground_speed=function()local ok,v=pcall(function()return player:getVelocity()end)if ok and v then return math.sqrt(v.x*v.x+v.z*v.z)*20 end;return 0 end
ysm_q.vertical_speed=function()local ok,v=pcall(function()return player:getVelocity()end)return ok and v and v.y*20 or 0 end
ysm_q.yaw_speed=function()local cy=player:getBodyYaw();local oy=ysm_state._last_yaw;ysm_state._last_yaw=cy;if oy then return(cy-oy)*20 end;return 0 end
ysm_q.head_x_rotation=function()local ok,v=pcall(function()return vanilla_model.HEAD:getOriginRot().y end)if not ok or type(v)~='number' then return 0 end if v>85 then v=85 elseif v<-85 then v=-85 end return v end
ysm_q.head_y_rotation=function()local ok,v=pcall(function()return vanilla_model.HEAD:getOriginRot().x end)return ok and v or 0 end
ysm_q.body_x_rotation=function()local ok,v=pcall(function()return vanilla_model.BODY:getOriginRot().y end)return ok and v or 0 end
ysm_q.body_y_rotation=function()local ok,v=pcall(function()return vanilla_model.BODY:getOriginRot().x end)return ok and v or 0 end
ysm_q.is_creative=function()local ok,v=pcall(function()return player:getGamemode()end)return ok and v=='creative' end
ysm_q.can_fly=function()local ok,v=pcall(function()return host:isFlying()end)return ok and v or false end
ysm_q.is_climbing=function()local ok,v=pcall(function()return player:isClimbing()end)return ok and v or false end
ysm_q.pose=function()local ok,v=pcall(function()return player:getPose()end)return ok and v or 'STANDING' end
ysm_q.is_dead=function()local ok,a=pcall(function()return player:isAlive()end)if ok and a==false then local ok2,mh=pcall(function()return player:getMaxHealth()end)return ok2 and mh~=nil and mh>0 end return false end
ysm_q.position=function()return function(axis)local ok,p=pcall(function()return player:getPos()end)if not ok or not p then return 0 end;axis=axis or 0;if axis==0 then return p.x elseif axis==1 then return p.y else return p.z end end end
ysm_q.position_delta=function()return function(axis)local ok,p=pcall(function()return player:getPos()end)if not ok or not p then return 0 end;local st=ysm_state;if st._pd_t~=st.lt then st._pd_prev=st._pd_cur;st._pd_cur={p.x,p.y,p.z};st._pd_t=st.lt end;local cur=st._pd_cur or {p.x,p.y,p.z};local prev=st._pd_prev or cur;axis=(axis or 0)+1;return (cur[axis] or 0)-(prev[axis] or 0)end end
ysm_q.time_stamp=function()local ok,v=pcall(function()return world.getTime()end)return ok and v or 0 end
ysm_q.eye_target_y_rotation=function()local ok,v=pcall(function()return vanilla_model.HEAD:getOriginRot().y end)return ok and v or 0 end
ysm_q.is_item_name_any=function()return function(slot,...)local items={...}local ok,it=pcall(function()return player:getHeldItem(slot=='offhand')end)if not ok or not it then return 0 end;local ok2,id=pcall(function()return it:getID()end)if not ok2 or not id then return 0 end;for _,n in ipairs(items)do if id==n then return 1 end end;return 0 end end
ysm_q.cardinal_facing_2d=function()local ok,y=pcall(function()return player:getRot().y end)if not ok or not y then return 2 end;local i=math.floor(((y%360)/90)+0.5)%4;local m={[0]=3,[1]=4,[2]=2,[3]=5};return m[i]or 2 end
ysm_q.relative_block_has_any_tag=function()return function(dx,dy,dz,...)dx=dx or 0;dy=dy or 0;dz=dz or 0;if math.abs(dx)>5 or math.abs(dy)>5 or math.abs(dz)>5 then return 0 end;local tags={...};local ok,res=pcall(function()local p=player:getPos();local bs=world.getBlockState(vec(math.floor(p.x+dx),math.floor(p.y+dy),math.floor(p.z+dz)));if not bs then return 0 end;local bt=bs:getTags();if not bt then return 0 end;local set={};for _,t in ipairs(bt) do local s=tostring(t);set[s]=true;set['minecraft:'..s]=true end;for _,w in ipairs(tags) do local ws=tostring(w);if set[ws] or set['minecraft:'..ws] then return 1 end end;return 0 end)return (ok and res)or 0 end end
ysm_safezero=setmetatable({},{__call=function()return ysm_safezero end,__index=function()return ysm_safezero end,__add=function()return 0 end,__sub=function()return 0 end,__mul=function()return 0 end,__div=function()return 0 end,__mod=function()return 0 end,__unm=function()return 0 end,__pow=function()return 0 end,__lt=function()return false end,__le=function()return false end,__len=function()return 0 end,__tostring=function()return '0' end})
setmetatable(ysm_q,{__index=function()return function()return ysm_safezero end end})

ysm_v=setmetatable({},{__index=function()return 0 end})
ysm_c=setmetatable({},{__index=function()return 0 end})
ysm_t=setmetatable({},{__index=function()return 0 end})
ysm_state={at=0,lt=0,dt=0.016,_last_wt=0,_last_yaw=0,_hurt=0,_last_hp=nil}
events.TICK:register(function()
  local ok,hp=pcall(function()return player:getHealth()end)
  if ok and hp then
    if ysm_state._last_hp and hp<ysm_state._last_hp then ysm_state._hurt=10 end
    ysm_state._last_hp=hp
  end
  if ysm_state._hurt>0 then ysm_state._hurt=ysm_state._hurt-1 end
end)
ysm_ctrls={}
ysm={};ysm._extra={};ysm._buttons={};ysm._so={};ysm._fo={}
ysm._dynamic_bones={}
ysm._bone_paths={}
ysm.food_level=20
ysm.has_mainhand=false
ysm.has_offhand=false
ysm.has_helmet=false
ysm.has_chest_plate=false
ysm.has_leggings=false
ysm.has_boots=false
ysm.input_vertical=0
ysm.input_horizontal=0
ysm.elytra_rot_z=0
ysm.is_close_eyes=false
ysm.yya=0
ysm.fps=60
ysm.ground_speed2=0
ysm.rendering_in_inventory=false
ysm.in_shield_block_cooldown=false
ysm.mod_version=function()return '2.5.0' end  -- no Figura API exposes the host's mod version; reports a constant (no real equivalent exists)
ysm._keys={}
ysm.keyboard=function(k)if k==nil then return 0 end;k=tostring(k):lower();local kb=ysm._keys[k];if kb~=nil then local ok,p=pcall(function()return kb:isPressed()end);return (ok and p) and 1 or 0 end;local ok,r=pcall(function()if k=='space' or k=='jump' then return host:isJumping() and 1 or 0 end;if k=='shift' or k=='sneak' then return player:isSneaking() and 1 or 0 end;return 0 end)return (ok and r) or 0 end
if keybinds then
  local function _reg(ch,key) local ok,kb=pcall(function()return keybinds:newKeybind('ysm.kb.'..key,'key.keyboard.'..key)end);if ok and kb then ysm._keys[ch]=kb end end
  local _alnum='abcdefghijklmnopqrstuvwxyz0123456789'
  for i=1,#_alnum do local c=_alnum:sub(i,i);_reg(c,c)end
  local _sym={['-']='minus',['=']='equal',['[']='left.bracket',[']']='right.bracket',[';']='semicolon',["'"]='apostrophe',[',']='comma',['.']='period',['/']='slash',['`']='grave.accent',[' ']='space'}
  _sym[string.char(92)]='backslash'
  for ch,key in pairs(_sym)do _reg(ch,key)end
end
ysm.texture_name=ysm_model or ''
function ysm.particle(name,x,y,z)pcall(function()if x~=nil then particles:newParticle(name,vec(x,y or 0,z or 0))else particles:newParticle(name)end end)end
function ysm.stop_sound(name)pcall(function()if sounds and sounds.stopSound then sounds:stopSound(name)elseif sounds and sounds.stop then sounds:stop(name)end end)end
function ysm.bone_pivot_abs(name)local m=models[ysm_model];local p=m and ysm_find_bone(m,name)if not p then return{x=0,y=0,z=0}end;local ok,v=pcall(function()return p:partToWorldMatrix():apply(0,0,0)end)if ok and v then return{x=v.x,y=v.y,z=v.z}end;return{x=0,y=0,z=0}end
function ysm.play_sound(id,name,vol)pcall(function()local n=name or id;if sounds and sounds.playSound then sounds:playSound(n,(player and player:getPos())or vec(0,0,0),vol or 1)end end)end
events.TICK:register(function()
  local ok,v=pcall(function()return player:getFood()end);if ok and v then ysm.food_level=v end
  local okm,itm=pcall(function()return player:getHeldItem(false)end);if okm and itm then local oe,e=pcall(function()return itm:isEmpty()end);ysm.has_mainhand=(oe and (not e))or false end
  local oko,ito=pcall(function()return player:getHeldItem(true)end);if oko and ito then local oe2,e2=pcall(function()return ito:isEmpty()end);ysm.has_offhand=(oe2 and (not e2))or false end
  local okf,fp=pcall(function()return client:getFPS()end);if okf and fp and fp>0 then ysm.fps=fp end
  local okg,gv=pcall(function()return player:getVelocity()end);if okg and gv then ysm.ground_speed2=math.sqrt(gv.x*gv.x+gv.z*gv.z)*20 end
  local oksb,sb=pcall(function()local it=world.newItem('minecraft:shield');return player:getCooldownPercent(it)end);ysm.in_shield_block_cooldown=(oksb and type(sb)=='number' and sb>0)or false
end)
events.RENDER:register(function(delta,ctx)
  local _okfp,_fp=pcall(function()return renderer:isFirstPerson()end);ysm.rendering_in_inventory=(_okfp and (not _fp))or false  -- YSM source maps this to CameraUtil::isThirdPerson
end)

function ysm.head_pitch()local ok,v=pcall(function()return vanilla_model.HEAD:getOriginRot().x end)return ok and v or 0 end
function ysm.head_yaw()local ok,v=pcall(function()return vanilla_model.HEAD:getOriginRot().y end)if not ok or type(v)~='number' then return 0 end if v>85 then v=85 elseif v<-85 then v=-85 end return v end

function ysm_find_bone(model, name)
  local path=ysm._bone_paths[name]
  if not path then return model[name] end
  local p=model
  for _,part in ipairs(path)do
    p=p[part]
    if not p then return nil end
  end
  return p
end

function ysm.bone_rot(name)
  local m=models[ysm_model];if not m then return{x=0,y=0,z=0}end
  local p=ysm_find_bone(m,name);if not p then return{x=0,y=0,z=0}end
  local r=p:getRot();return{x=-r.x,y=-r.y,z=r.z}
end
function ysm.bone_pos(name)
  local m=models[ysm_model];if not m then return{x=0,y=0,z=0}end
  local p=ysm_find_bone(m,name);if not p then return{x=0,y=0,z=0}end
  local r=p:getPos();return{x=-r.x,y=r.y,z=r.z}
end
function ysm.bone_scale(name)
  local m=models[ysm_model];if not m then return{x=1,y=1,z=1}end
  local p=ysm_find_bone(m,name);if not p then return{x=1,y=1,z=1}end
  return p:getScale()
end

function ysm._update_physics(dt)
  if dt<=0 then return end
  for _,so in pairs(ysm._so)do
    local f=ysm_math.clamp(so.f or 1,0,5)
    local d=ysm_math.clamp(so.d or 1,0,1)
    local r=so.r or 1
    local inp=so.inp or 0
    local k1=d/ysm_pi/f
    local k2=1/((2*ysm_pi*f)^2)
    local k3=r*d/2/ysm_pi/f
    local inpDot=(inp-so.prev)/dt;so.prev=inp
    local maxDt=math.sqrt(4*k2+k1*k1)-k1
    local cyc=math.min(256,math.max(1,math.ceil(dt/maxDt)))
    local sDt=dt/cyc
    local l=so.last;local ld=so.lastDot
    for _=1,cyc do
      l=l+sDt*ld
      ld=ld+sDt*(k3*inpDot+inp-l-k1*ld)/k2
    end
    so.last=l;so.lastDot=ld
  end
  for _,fo in pairs(ysm._fo)do
    local inp=fo.inp or 0
    local resp=fo.r or 1
    if resp>0 then
      fo.last=(1-dt/resp)*fo.last+(dt/resp)*inp
    end
  end
end

function ysm.second_order(name,input,freq,damp,resp)
  freq=ysm_math.clamp(freq or 1,0,5)
  damp=ysm_math.clamp(damp or 1,0,1)
  resp=resp or 1
  local so=ysm._so[name]
  if not so then
    so={last=0,lastDot=0,prev=input,f=freq,d=damp,r=resp,inp=input}
    ysm._so[name]=so;return input
  end
  so.inp=input;so.f=freq;so.d=damp;so.r=resp
  return so.last
end

function ysm.first_order(name,input,resp)
  resp=resp or 1
  local fo=ysm._fo[name]
  if not fo then
    fo={last=input,r=resp,inp=input}
    ysm._fo[name]=fo;return input
  end
  fo.inp=input;fo.r=resp
  return fo.last
end

function ysm._apply_dynamic_bones()
  local m=models[ysm_model];if not m then return end
  local h=ysm_find_bone(m,'Head')
  if h then
    local hp=ysm.head_pitch()or 0;local hy=ysm.head_yaw()or 0
    local cr=h:getRot();local cx,cy,cz=cr.x,cr.y,cr.z
    if not ysm._head_overridden then
      h:setRot(hp/2, hy/2, cz)
    end
    ysm._head_overridden=false
  end
  local ap={}
  if animations then
    for _,a in ipairs(animations:getPlaying(true))do ap[a:getName()]=true end
  end
  for _,db in ipairs(ysm._dynamic_bones)do
    if db.anim and not ap[db.anim] then goto continue end
    local p=ysm_find_bone(m,db.bone)
    if p then
      local ok,ox,oy,oz=pcall(function()return tonumber(db.x()),tonumber(db.y()),tonumber(db.z())end)
      if ok then
        if db.ch=='rot' then
          p:setRot(-ox,-oy,oz)
          if db.bone=='Head' then ysm._head_overridden=true end
        elseif db.ch=='pos' then p:setPos(-ox,oy,oz)
        elseif db.ch=='scale' then p:setScale(ox,oy,oz) end
      end
    end
    ::continue::
  end
end

function ysm_ctrl_init(name,data)
  local c=data;c._nm=name;c._st=c.initial_state;c._anms={};c._entered={}
  ysm_ctrls[name]=c
end

function ysm_ctrl_enter(c,st)
  local nd=c.states[st]
  if nd and nd.on_entry then for _,fn in ipairs(nd.on_entry)do pcall(fn)end end
  for _,an in ipairs(c._anms)do local a=(animations[ysm_model]or {})[an];if a then a:stop()end end
  c._st=st;c._anms={}
  if nd and nd.animations then
    for _,pair in ipairs(nd.animations)do
      local anm,cond=pair[1],pair[2]
      if cond==nil then
        table.insert(c._anms,anm)
        local a=(animations[ysm_model]or {})[anm]
        if a then a:setPriority(0):setOverride(true):play()end
      else
        local ok2,rv2=pcall(cond)
        if ok2 and rv2 then
          table.insert(c._anms,anm)
          local a=(animations[ysm_model]or {})[anm]
          if a then a:setPriority(0):setOverride(true):play()end
        end
      end
    end
  end
  c._entered={[st]=true}
end

function ysm_ctrl_tick(name)
  local c=ysm_ctrls[name];if not c then return end
  if not c._entered[c._st] then
    ysm_ctrl_enter(c,c._st)
    return
  end
  local sd=c.states[c._st];if not sd then return end
  for _,tr in ipairs(sd.transitions or {})do
    local ok,rv=pcall(tr[2])
    if ok and rv then
      local old=c._st;local od=c.states[old]
      if od and od.on_exit then for _,fn in ipairs(od.on_exit)do pcall(fn)end end
      ysm_ctrl_enter(c,tr[1])
      break
    end
  end
end

function ysm_extra_tick()
  for _,cfg in pairs(ysm._extra or {})do
    local k=cfg.animation:match("^#(.+)$")or cfg.animation
    if ysm_v[k] then
      local a=(animations[ysm_model]or {})[k]
      if a then a:setPriority(1):play()end
    end
  end
end

-- ===== YSM action wheel: emote / extra-animation playback =====
-- Mirrors YSM's in-game animation roulette: lets the player manually trigger
-- the model's extra animations (dances, poses, actions, signs). Only ONE emote
-- plays at a time; it runs at a high priority so it overrides locomotion / hold
-- poses, blends in/out smoothly, and is stopped on a second click (toggle) or
-- when another emote is selected.
ysm._emote={cur=nil}
local function ysm_emote_get(nm)
  local at=animations[ysm_model]
  return at and at[nm] or nil
end
function ysm_emote_stop()
  if ysm._emote.cur then
    local a=ysm_emote_get(ysm._emote.cur)
    if a then pcall(function()a:stop()end)end
    ysm._emote.cur=nil
  end
end
function ysm_emote_toggle(nm)
  local was=ysm._emote.cur
  ysm_emote_stop()
  if was==nm then return false end
  local a=ysm_emote_get(nm)
  if not a then return false end
  pcall(function()a:setBlendTime(3,3)end)
  pcall(function()a:setPriority(64):play()end)
  ysm._emote.cur=nm
  return true
end

ysm_state._loco_cur=nil
function ysm_loco_tick()
  if not ysm._loco then return end
  local want=nil
  for _,e in ipairs(ysm._loco)do
    local ok,rv=pcall(e[2])
    if ok and rv then want=e[1];break end
  end
  if want~=ysm_state._loco_cur then
    if ysm_state._loco_cur then
      local prev=(animations[ysm_model]or {})[ysm_state._loco_cur]
      if prev then prev:stop()end
    end
    ysm_state._loco_cur=want
    if want then
      local a=(animations[ysm_model]or {})[want]
      if a then a:setPriority(0):play()end
    end
  end
end

events.ENTITY_INIT:register(function()
  pcall(function()vanilla_model.ALL:setVisible(false)end)
  if ysm_model_scale_x then
    pcall(function()models[ysm_model]:setScale(ysm_model_scale_x,ysm_model_scale_y,ysm_model_scale_z)end)
  end
  for _,hn in ipairs(ysm._hidden_default or {})do
    pcall(function()local m=models[ysm_model];local p=m and ysm_find_bone(m,hn);if p then p:setVisible(false)end end)
  end
  for n,_ in pairs(ysm_ctrls)do ysm_ctrl_init(n,ysm_ctrls[n])end
  pcall(function()
    if not ysm._wheel then return end
    local W=ysm._wheel
    local pages={}
    local function mk(key,def)
      local pg=action_wheel:newPage((def and def.title) or key)
      pages[key]=pg
      return pg
    end
    mk('__main__', W.main)
    for k,def in pairs(W.pages or {})do mk(k,def)end
    local function fill(key,def)
      local pg=pages[key]
      if not (pg and def) then return end
      -- Figura paginates pages with >8 actions automatically (mouse-scroll
      -- shifts between groups of 8), so we add every slot without capping.
      local idx=1
      for _,s in ipairs(def.slots or {})do
        local ac=pg:newAction(idx)
        idx=idx+1
        if s.title then pcall(function()ac:setTitle(s.title)end)end
        if s.item then pcall(function()ac:setItem(s.item)end)end
        if s.kind=='page' then
          local tgt=s.page
          ac:setOnLeftClick(function()local p=pages[tgt];if p then action_wheel:setPage(p)end end)
        elseif s.kind=='back' then
          ac:setOnLeftClick(function()local p=pages['__main__'];if p then action_wheel:setPage(p)end end)
        elseif s.kind=='anim' then
          local nm=s.anim
          ac:setOnLeftClick(function()local on=ysm_emote_toggle(nm);pcall(function()ac:setToggled(on and true or false)end)end)
        elseif s.kind=='var' then
          local vk=s.var
          ac:setOnToggle(function(t)ysm_v[vk]=t and 1 or 0;pcall(function()ac:setToggled(t)end)end)
        elseif s.kind=='cycle' then
          local vk=s.var; local vals=s.values or {}
          ysm._cyc=ysm._cyc or {}
          ac:setOnLeftClick(function()
            local i=(ysm._cyc[vk] or 0)+1
            if i>#vals then i=1 end
            ysm._cyc[vk]=i
            ysm_v[vk]=vals[i]
          end)
        elseif s.kind=='scroll' then
          local vk=s.var; local st=s.step or 0.1; local mn=s.min; local mx=s.max; local df=s.default or 0
          if ysm_v[vk]==nil or ysm_v[vk]==0 then ysm_v[vk]=df end
          ac:setOnScroll(function(d)
            local nv=(ysm_v[vk] or df)+(d or 0)*st
            if mn and nv<mn then nv=mn end
            if mx and nv>mx then nv=mx end
            ysm_v[vk]=nv
          end)
        end
      end
    end
    fill('__main__', W.main)
    for k,def in pairs(W.pages or {})do fill(k,def)end
    if pages['__main__'] then action_wheel:setPage(pages['__main__'])end
  end)
end)

ysm_state._tc=0

events.TICK:register(function()
  local wt=world.getTime()
  local lw=ysm_state._last_wt
  if lw>0 and wt>lw then
    local raw_dt=(wt-lw)/20
    ysm_state.dt=math.min(raw_dt,0.1)
  else
    ysm_state.dt=0.016
  end
  ysm_state._last_wt=wt
  ysm_loco_tick()
  for n,_ in pairs(ysm_ctrls)do ysm_ctrl_tick(n)end
  ysm_extra_tick()
end)

-- Animation clock + per-frame bone application.
-- In YSM, query.anim_time = adjustedTick/20.0 -> GAME-TIME seconds, smoothed by
-- the render partialTick. It is NOT wall-clock and it freezes when the game is
-- paused. We mirror that exactly here:
--   * the clock is GAME TIME (world.getTime(delta)/20): correct rate, freezes
--     on pause, smooth between ticks via partialTick (delta in 0..1).
--   * it is RECOMPUTED every frame, never accumulated, so it is immune to how
--     many times per frame anything reads it -- it can never "run too fast".
-- We run this in WORLD_RENDER, which fires exactly ONCE per frame and is render
-- context independent. That keeps the head/look override running per-frame
-- (smooth, adapts to any FPS / open environment) but avoids the per-pass
-- flicker that events.RENDER would cause: events.RENDER fires once per render
-- PASS (world / first-person / GUI paperdoll), and vanilla_model.HEAD differs
-- between those passes, so applying the head override there makes it jitter.
events.WORLD_RENDER:register(function(delta)
  local d=delta or 0
  local gt; pcall(function() gt=world.getTime(d) end)
  if gt then
    local sec=gt/20
    if not ysm_state._t0 then ysm_state._t0=sec end
    ysm_state.at=sec-ysm_state._t0
    ysm_state.lt=sec-ysm_state._t0
    if (ysm_state._phys_last or -1)>=0 and gt>ysm_state._phys_last then
      ysm._update_physics((gt-ysm_state._phys_last)/20)
    end
    ysm_state._phys_last=gt
  else
    ysm_state.at=ysm_state.at+(ysm_state.dt or 0.016)
    ysm_state.lt=ysm_state.lt+(ysm_state.dt or 0.016)
  end
  ysm._apply_dynamic_bones()
end)
"""

def to_lua_val(v, indent=0):
    pad = '  ' * indent
    if v is None: return 'nil'
    if isinstance(v, bool): return str(v).lower()
    if isinstance(v, (int, float)): return str(v)
    if isinstance(v, str):
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(v, (list, tuple)):
        if not v: return '{}'
        items = [f"{pad}  {to_lua_val(x, indent+1)}" for x in v]
        return '{\n' + ',\n'.join(items) + '\n' + pad + '}'
    if isinstance(v, dict):
        if not v: return '{}'
        items = []
        for k, val in v.items():
            ks = str(k)
            # NOTE: Python's str.isidentifier() accepts non-ASCII letters (e.g.
            # Chinese), but Lua only allows ASCII identifiers as bare keys. Emit
            # a bare key only for ASCII identifiers; otherwise bracket-quote it.
            is_ascii_ident = (
                ks != ""
                and ks[0].isascii() and (ks[0].isalpha() or ks[0] == "_")
                and all(ch == "_" or (ch.isascii() and ch.isalnum()) for ch in ks)
            )
            if is_ascii_ident:
                lk = ks
            else:
                lk = "['" + ks.replace("\\", "\\\\").replace("'", "\\'") + "']"
            items.append(f"{pad}  {lk} = {to_lua_val(val, indent+1)}")
        return '{\n' + ',\n'.join(items) + '\n' + pad + '}'
    return 'nil'

def _read_json(path):
    if not path or not path.exists(): return None
    return json.loads(path.read_bytes().decode('utf-8', errors='replace'))


# R2+R3: appended Lua that (a) overrides the locomotion driver so arms always
# return to idle (defensively stops every non-wanted loco animation each tick),
# and (b) adds YSM's weapon/equipment hold hook (classify held item -> play the
# matching hold animation, which poses the arm + reveals custom weapon bones,
# replacing the vanilla item).
YSM_HOLD_AND_LOCO_OVERRIDE = r"""
-- ===== R3: robust locomotion driver (arms always return to idle) =====
function ysm_loco_tick()
  if not ysm._loco then return end
  local want=nil
  for _,e in ipairs(ysm._loco) do
    local ok,rv=pcall(e[2])
    if ok and rv then want=e[1]; break end
  end
  ysm_state._loco_cur=want
  -- '' is the AnimationManager STOP sentinel (riding): treat as "no loco anim".
  -- Switching is routed through the native setBlend crossfade manager so the
  -- outgoing state fades out while the new one fades in (YSM transition length).
  -- The manager owns play()/stop(), so we no longer hard-cut every tick.
  ysm_blend_set("loco", (want~="" and want) or nil, 0)
  ysm_hold_tick()
end

-- ===== R2: weapon/equipment hold hook (item replacement) =====
-- Mirrors YSM ConditionHold + InnerClassify: classify the held item and play the
-- matching hold_* animation. Those animations pose the arm and scale the model's
-- custom weapon bones (Bow, etc.) into view; the vanilla item is hidden for that
-- hand so the custom model replaces it. Priority above locomotion so the holding
-- pose wins over the arm swing.
ysm_state._hold = ysm_state._hold or {mainhand=nil, offhand=nil}

-- Item -> ordered list of YSM InnerClassify category labels. We return EVERY
-- plausible label (most specific first) and let the resolver pick whichever the
-- model actually defines an animation for, so matching never depends on a
-- single exact string. Mirrors YSM InnerClassify.getItemType plus the
-- charged_crossbow / fishing special cases handled in the hold predicates.
local function ysm_item_categories(id, stack)
  local out, n = {}, 0
  local function add(x) n=n+1; out[n]=x end
  if not id or id=="" then return out end
  if id:find("crossbow$") then
    -- charged crossbow takes precedence over a plain crossbow (CrossbowItem.isCharged)
    local ok,charged=pcall(function()
      local t=stack:getTag(); if not t then return false end
      local cp=t.ChargedProjectiles
      if cp and #cp>0 then return true end
      return t.Charged==1 or t.Charged==true
    end)
    if ok and charged then add("charged_crossbow") end
    add("crossbow")
  elseif id:find("_sword$") then add("sword")
  elseif id:find("_axe$") then add("axe")
  elseif id:find("_pickaxe$") then add("pickaxe")
  elseif id:find("_shovel$") then add("shovel")
  elseif id:find("_hoe$") then add("hoe")
  elseif id=="minecraft:shield" then add("shield")
  elseif id:find("bow$") then add("bow")
  elseif id:find("fishing_rod$") then add("fishing_rod"); add("fishing")
  elseif id=="minecraft:trident" then add("spear")
  elseif id=="minecraft:splash_potion" or id=="minecraft:lingering_potion" then add("throwable_potion")
  end
  return out
end

-- Generic YSM trigger resolver. Given a family prefix (hold_mainhand,
-- hold_offhand, use_mainhand, use_offhand, swing, swing_offhand) and the
-- relevant ItemStack, return the matching animation NAME that the model
-- actually defines, trying every YSM matcher form in priority order:
--   prefix$<exact id>  >  prefix#<tag>  >  prefix:<inner category>  >  prefix:<use action>
-- We only ever return a name that exists in the model, so unused conventions
-- emit no calls (no dead triggers, nothing model-specific hardcoded). Empty
-- hands are deliberately left to locomotion (we do NOT force :empty, which
-- would fight the natural arm swing).
local function ysm_resolve(prefix, stack)
  local anims_t = animations[ysm_model] or {}
  if stack==nil then return nil end
  local ok,empty=pcall(function() return stack:isEmpty() end)
  if ok and empty then return nil end
  local id=nil
  local ok2,gid=pcall(function() return stack:getID() end)
  if ok2 then id=gid end
  -- 1) exact item id   (hold_mainhand$minecraft:mace, swing$minecraft:mace, ...)
  if id and anims_t[prefix.."$"..id] then return prefix.."$"..id end
  -- 2) item tags       (hold_mainhand#minecraft:swords, ...)
  local okt,tags=pcall(function() return stack:getTags() end)
  if okt and type(tags)=="table" then
    for _,tg in ipairs(tags) do
      if anims_t[prefix.."#"..tg] then return prefix.."#"..tg end
    end
  end
  -- 3) inner category  (hold_mainhand:sword, hold_*:charged_crossbow, swing:spear, ...)
  for _,cat in ipairs(ysm_item_categories(id, stack)) do
    if anims_t[prefix..":"..cat] then return prefix..":"..cat end
  end
  -- 4) vanilla use action (use_*:bow / use_*:crossbow / use_*:spear / :eat / :drink / :block ...)
  local oku,ua=pcall(function() return stack:getUseAction() end)
  if oku and type(ua)=="string" and ua~="" and ua~="NONE" then
    local low=ua:lower()
    if anims_t[prefix..":"..low] then return prefix..":"..low end
  end
  return nil
end

-- Equipment-driven triggers. Mirrors YSM ConditionHold / ConditionUse /
-- ConditionSwing, including the gate that a hand's hold pose yields while that
-- same hand is swinging or using its item (YSM checkSwingAndUse). Polled per
-- tick (state only); the chosen animation then plays natively per frame.
--   hold_* : looping pose while the item is simply held
--   use_*  : looping pose while the item is in use (drawing bow, eating, blocking...)
--   swing* : the attack swing (hand-accurate via getSwingArm)
function ysm_hold_tick()
  if not ysm_model or not animations[ysm_model] then return end

  -- Set of animations that swap in a CUSTOM replacement model bone (e.g. the
  -- model's own drawn bow). Built once from the reveal map. We only hide the
  -- vanilla held item when the active pose is one of these -- for items the
  -- model does NOT replace (sword, shield, trident, crossbow, food, ...) the
  -- real item must stay visible, otherwise it just vanishes from the hand.
  if not ysm._reveal_anims then
    ysm._reveal_anims = {}
    if ysm._reveal then
      for _,revs in pairs(ysm._reveal) do
        if type(revs)=="table" then for _,an in ipairs(revs) do ysm._reveal_anims[an]=true end end
      end
    end
  end

  local using=false; pcall(function() using=player:isUsingItem() end)
  local uhand=nil; if using then pcall(function() uhand=player:getActiveHand() end) end
  local swarm=nil; pcall(function() swarm=player:getSwingArm() end)  -- "MAIN_HAND"/"OFF_HAND"/nil

  local hands = {
    {key="mainhand", prefix="hold_mainhand", off=false, hand="MAIN_HAND", vi=(vanilla_model and vanilla_model.RIGHT_ITEM)},
    {key="offhand",  prefix="hold_offhand",  off=true,  hand="OFF_HAND",  vi=(vanilla_model and vanilla_model.LEFT_ITEM)},
  }
  for _,h in ipairs(hands) do
    local stack=nil
    local ok,sk=pcall(function() return player:getHeldItem(h.off) end)
    if ok then stack=sk end

    -- USE: while this hand is actively using its item (bow draw, eat, block...)
    local use_want=nil
    if using and uhand==h.hand then use_want=ysm_resolve("use_"..h.key, stack) end
    ysm_blend_set("use_"..h.key, use_want, 3)

    -- HOLD: yields while this same hand is using its item (YSM checkSwingAndUse).
    -- We deliberately do NOT gate hold on swinging here -- the attack swing is a
    -- one-shot overlay (handled below) that wins by priority and then releases,
    -- so the hold pose stays underneath instead of blinking out on every swing.
    local gated = (use_want~=nil) or (using and uhand==h.hand)
    local hold_want=nil
    if not gated then hold_want=ysm_resolve(h.prefix, stack) end
    ysm_blend_set("hold_"..h.key, hold_want, 2)

    -- Hide the vanilla item ONLY when the active pose swaps in a custom model
    -- bone for it. Otherwise keep the real item visible.
    local active = hold_want or use_want
    local hide = (active~=nil) and (ysm._reveal_anims[active]==true)
    if h.vi then pcall(function() h.vi:setVisible(not hide) end) end
  end

  -- SWING: the attack swing is a ONE-SHOT clip (authored as once / hold / even
  -- loop). Playing it through the looping crossfade channel truncated it to the
  -- ~0.3s swing flag and made it flicker. Instead we (re)play it once on each
  -- new swing -- rising edge of getSwingArm or a getSwingTime reset -- at a
  -- priority above hold/use, then force-stop it after its own length so loop /
  -- hold-type clips don't get stuck. It overlays the hold pose, never replaces it.
  do
    -- ysm_anim_get is a `local function` declared AFTER ysm_hold_tick, so it is
    -- NOT in scope here (referencing it would resolve to a nil global and crash
    -- on swing). Use a self-contained local lookup against the animations table.
    local function _ag(nm)
      if not nm then return nil end
      local at = animations[ysm_model]
      return at and at[nm] or nil
    end
    local nowt; pcall(function() nowt=client.getSystemTime() end)
    nowt = (type(nowt)=="number") and (nowt/1000) or ((ysm_state._swclk or 0) + (ysm_state.dt or 0.05))
    ysm_state._swclk = nowt
    local st=0; pcall(function() st=player:getSwingTime() end)
    local newswing = (swarm~=nil) and ((ysm_state._psw==nil) or (swarm~=ysm_state._psw) or (st < (ysm_state._pst or 0)))
    if newswing then
      local off=(swarm=="OFF_HAND")
      local prefix=off and "swing_offhand" or "swing"
      local stack=nil
      local ok,sk=pcall(function() return player:getHeldItem(off) end)
      if ok then stack=sk end
      local nm=ysm_resolve(prefix, stack)
      if ysm_state._sw and ysm_state._sw.anim then
        local pa=_ag(ysm_state._sw.anim); if pa then pcall(function() pa:stop() end) end
      end
      if nm then
        local a=_ag(nm)
        if a then
          local len=1.0; local okl,L=pcall(function() return a:getLength() end)
          if okl and type(L)=="number" and L>0 then len=L end
          pcall(function() a:setPriority(4):setBlend(1):play() end)
          ysm_state._sw={anim=nm, t1=nowt+len}
        else ysm_state._sw=nil end
      else ysm_state._sw=nil end
    end
    -- expire the one-shot swing clip after its natural length
    if ysm_state._sw and nowt >= ysm_state._sw.t1 then
      local pa=_ag(ysm_state._sw.anim); if pa then pcall(function() pa:stop() end) end
      ysm_state._sw=nil
    end
    ysm_state._psw = swarm
    ysm_state._pst = (swarm~=nil) and st or nil
  end
end

-- ===== native setBlend crossfade manager (NO GSAnimBlend dependency) =====
-- Figura has no built-in fade: setBlend(w) only sets a static [0,1] weight, so
-- the old blendTime()/onBlend() chain (GSAnimBlend-only) silently no-op'd under
-- pcall and nothing was ever smoothed. Here we drive setBlend over wall-clock
-- time in RENDER to crossfade between states, mirroring YSM's per-controller
-- transition length. Each "channel" (locomotion, each hand) keeps at most one
-- current + one outgoing animation and symmetrically fades weight w: incoming
-- 0->1, outgoing 1->0, then the outgoing is stopped.
ysm_state._ch = ysm_state._ch or {}            -- [chan] = {cur, prev, w, pr}
local YSM_BLEND_SEC = 0.12                      -- transition length (~2-3 ticks)

local function ysm_anim_get(name)
  if not name then return nil end
  local at = animations[ysm_model] or {}
  return at[name]
end

-- Request `name` (or nil) as the active animation for `chan`, crossfading from
-- whatever is currently playing. Re-requesting the same name is a no-op.
function ysm_blend_set(chan, name, pr)
  local c = ysm_state._ch[chan]
  if not c then c = {cur=nil, prev=nil, w=1, pr=pr or 0}; ysm_state._ch[chan]=c end
  c.pr = pr or c.pr
  if name == c.cur then return end
  -- previous transition still mid-flight: drop its outgoing immediately
  if c.prev then local pa=ysm_anim_get(c.prev); if pa then pcall(function() pa:stop() end) end end
  c.prev = c.cur
  c.cur  = name
  c.w    = 0
  if c.cur then
    local a = ysm_anim_get(c.cur)
    if a then pcall(function() a:setPriority(c.pr):setBlend(0):play() end) end
  end
end

events.RENDER:register(function()
  local now; pcall(function() now = client.getSystemTime() end)
  local dt
  if now then dt = (now - (ysm_state._bms or now)) / 1000; ysm_state._bms = now else dt = 0.03 end
  if dt ~= dt or dt < 0 or dt > 0.5 then dt = 0.03 end       -- clamp pauses / lag / NaN
  local step = (YSM_BLEND_SEC > 0) and (dt / YSM_BLEND_SEC) or 1
  for _,c in pairs(ysm_state._ch) do
    if c.w < 1 then
      c.w = math.min(1, c.w + step)
      if c.cur  then local a=ysm_anim_get(c.cur);  if a then pcall(function() a:setBlend(c.w)   end) end end
      if c.prev then local p=ysm_anim_get(c.prev); if p then pcall(function() p:setBlend(1-c.w) end) end end
      if c.w >= 1 and c.prev then
        local p=ysm_anim_get(c.prev); if p then pcall(function() p:stop() end) end
        c.prev = nil
      end
    end
  end
end)
function ysm_anim_playing(a)
  if not a then return false end
  local ok,st=pcall(function() return a:getPlayState() end)
  if ok and st~=nil then return st=="PLAYING" end
  local ok2,b=pcall(function() return a:isPlaying() end)
  return ok2 and b==true
end

-- visibility optimizer: base-hidden bones stay setVisible(false) until one of
-- their reveal animations is actually playing. A "reveal" is any non-base
-- (non pre_parallel*) animation that drives the bone's scale -- whether that
-- scale is static numeric or Molang-driven. We do NOT re-evaluate the scale
-- here: the magnitude is baked natively into the bbmodel keyframes (numeric or
-- compiled Molang), so native playback sets the per-frame value (0 = hidden).
-- setVisible only lifts the base-hide; native scale does the rest.
function ysm_update_hidden()
  local m=models[ysm_model]; if not m then return end
  local anims_t=animations[ysm_model]; if not anims_t then return end
  for _,bn in ipairs(ysm._hidden_default or {}) do
    local show=false
    local revs=ysm._reveal and ysm._reveal[bn]
    if revs then
      for _,an in ipairs(revs) do
        if ysm_anim_playing(anims_t[an]) then show=true break end
      end
    end
    pcall(function() local p=ysm_find_bone(m,bn); if p then p:setVisible(show) end end)
  end
end

events.RENDER:register(function(delta, ctx)
  local m = models[ysm_model]; if not m then return end
  local fp = (ctx == "FIRST_PERSON")
  pcall(function()
    for _,c in pairs(m:getChildren()) do
      local nm = c.getName and c:getName()
      if nm == "ArmModel" then
        c:setVisible(fp)
      elseif ysm._reveal and ysm._reveal[nm] ~= nil then
        -- base-hidden bone: visibility managed by ysm_update_hidden()
      else
        c:setVisible(not fp)
      end
    end
  end)
  ysm_update_hidden()
end)
"""

def gen_lua(ysm_path, project_dir, model_name, ysm_json, animations_by_bone, dynamic_bones=None, bone_paths=None, optimize=True, annotations=None, role_models=None):
    files = ysm_json.get("files", {})
    player = files.get("player", {})
    props = ysm_json.get("properties", {})
    meta = ysm_json.get("metadata", {})

    lines = [YSM_RUNTIME_HEAD]

    # ---- player annotations (compile-directed escape hatches) ----
    # The player tags individual bones to override the optimizer's automatic
    # decisions. This is how we cover the undecidable / open-world cases the
    # static analyzer can't reason about (held items, armor swaps, mod-injected
    # state, ...): the HUMAN supplies the bit the compiler cannot prove.
    #   mode = "force_visible" : never base-hide (always rendered)
    #   mode = "force_hidden"  : never reveal (always skipped)
    #   mode = "interpreter"   : give up optimizing this bone; run YSM base
    #                            layers so the native scale drives it as-is
    #   role = "<name>"        : semantic tag (e.g. firstperson_hand, held_item)
    _ann = annotations or {}
    _ann_bones = _ann.get("bones", {}) if isinstance(_ann, dict) else {}
    _force_visible, _force_hidden, _interp_bones, _roles = set(), set(), set(), {}
    for _bn, _cfg in (_ann_bones.items() if isinstance(_ann_bones, dict) else []):
        if not isinstance(_cfg, dict):
            continue
        _mode = _cfg.get("mode")
        if _mode == "force_visible":
            _force_visible.add(_bn)
        elif _mode == "force_hidden":
            _force_hidden.add(_bn)
        elif _mode == "interpreter":
            _interp_bones.add(_bn)
        if _cfg.get("role"):
            _roles[_bn] = _cfg["role"]
    _role_models = role_models or {}
    if _ann_bones:
        print(f"  [annotations] force_visible={len(_force_visible)} force_hidden={len(_force_hidden)} interpreter={len(_interp_bones)} roles={len(_roles)}")

    # Model info
    lines.append(f"\n-- model: {meta.get('name','')}")
    lines.append(f"ysm_model = {to_lua_val(model_name)}")

    # YSM scaling (width_scale / height_scale from properties)
    ws = float(props.get("width_scale", 0.7))
    hs = float(props.get("height_scale", 0.7))
    lines.append(f"\n-- YSM scale: width_scale={ws}, height_scale={hs}")
    lines.append(f"ysm_model_scale_x = {hs}")
    lines.append(f"ysm_model_scale_y = {ws}")
    lines.append(f"ysm_model_scale_z = {hs}")

    # Bone paths for recursive lookup
    if bone_paths:
        lines.append(f"\n-- bone paths for recursive lookup (Figura only finds direct children)")
        lines.append(f"ysm._bone_paths = {{")
        for bname, bpath in sorted(bone_paths.items()):
            path_list = list(bpath)
            lines.append(f"  [{to_lua_val(bname)}] = {to_lua_val(path_list)},")
        lines.append(f"}}")

    # Collect animation names
    anim_names = set()
    for ak, ap in (player.get("animation", {}) or {}).items():
        ad = _read_json(ysm_path / ap)
        if ad:
            for an in (ad.get("animations", {}) or {}):
                anim_names.add(an)
    lines.append(f"\nysm_anim_names = {to_lua_val(sorted(anim_names))}")

    # ---- Figura-native visibility optimizer ----
    # YSM hides bones by scaling them to 0 in an ALWAYS-ON `pre_parallel*` base layer
    # (played by the engine, not the controller) and reveals them by scaling back to
    # ~1 in specific animations (hold_/use_/pose_/gui/...). Confirmed against OpenYSM
    # source: RenderUtils.scaleMatrixForBone collapses scale-0 bones (and skips locator
    # attach), i.e. visibility is purely runtime scale, never a static flag.
    #
    # Perpetually running a scale-0 layer is wasteful & inelegant in Figura, where
    # setVisible(false) TRULY skips a bone. So we optimize:
    #   1) every bone the base layer scales to 0  -> default setVisible(false);
    #   2) setVisible(true) ONLY while one of its "reveal" animations is playing.
    # Detection is gated on live play-state at runtime, so reveal anims that are never
    # actually played (e.g. idle_old*) can't cause false reveals. This generalizes to
    # BOTH the decorative frame (`Backgrounds`, revealed by `gui`) and weapons
    # (`Bow`/`arrow`/`Broom`, revealed by hold/use poses) with no base layer at all.
    def _flatten_scale(sc):
        out = []
        def _add(v):
            if isinstance(v, (int, float)):
                out.append(float(v))
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, (int, float)):
                        out.append(float(x))
        if isinstance(sc, (int, float, list)):
            _add(sc)
        elif isinstance(sc, dict):
            for _v in sc.values():
                if isinstance(_v, dict):
                    for _kk in ("pre", "post", "vector"):
                        if _kk in _v:
                            _add(_v[_kk])
                else:
                    _add(_v)
        return out

    SHOW_THRESHOLD = 0.5
    def _scale_has_molang(sc):
        # True only if an actual scale VALUE position holds a non-numeric
        # (Molang) string. Keyframe metadata keys like "lerp_mode"
        # ("catmullrom", "linear", ...) or "easing" must NOT count -- otherwise a
        # purely numeric animation gets misclassified as Molang and routed to a
        # degenerate constant evaluator. Mirror the value positions that
        # _flatten_scale reads.
        found = [False]
        def _val(v):
            if found[0]:
                return
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return
                try:
                    float(s)
                except ValueError:
                    found[0] = True
            elif isinstance(v, list):
                for x in v:
                    _val(x)
        if isinstance(sc, (str, int, float, list)):
            _val(sc)
        elif isinstance(sc, dict):
            for _v in sc.values():
                if isinstance(_v, dict):
                    for _kk in ("pre", "post", "vector"):
                        if _kk in _v:
                            _val(_v[_kk])
                else:
                    _val(_v)
        return found[0]
    def _collect_scale_exprs(sc):
        # Lua scalar expressions for every scalar in this scale channel (numeric ->
        # literal, Molang -> compiled). Used to evaluate live reveal scale at runtime.
        exprs = []
        def _add(v):
            if isinstance(v, (int, float)):
                exprs.append(repr(float(v)))
            elif isinstance(v, str):
                s = v.strip()
                if not s:
                    return
                try:
                    exprs.append(repr(float(s)))
                except ValueError:
                    exprs.append("(" + molang2lua(s) + ")")
            elif isinstance(v, list):
                for x in v:
                    _add(x)
            elif isinstance(v, dict):
                handled = False
                for _kk in ("pre", "post", "vector"):
                    if _kk in v:
                        _add(v[_kk]); handled = True
                if not handled:
                    for x in v.values():
                        _add(x)
        _add(sc)
        return exprs
    hidden_default = []          # bones scaled to 0 in pre_parallel*
    reveal_anims = {}            # bone -> set of anims whose NUMERIC scale reveals it (~1)
    reveal_dyn = {}              # bone -> list of (anim, [lua exprs]) for MOLANG-driven scale
    _anim_cache = {}
    for _ak, _ap in (player.get("animation", {}) or {}).items():
        _ad = _read_json(ysm_path / _ap)
        if _ad:
            _anim_cache[_ak] = _ad
    # pass 1: collect base-hidden bones from the pre_parallel* base layer
    for _ad in _anim_cache.values():
        for _an, _ao in (_ad.get("animations", {}) or {}).items():
            if not (isinstance(_ao, dict) and str(_an).startswith("pre_parallel")):
                continue
            for _bn, _ba in (_ao.get("bones", {}) or {}).items():
                if not isinstance(_ba, dict):
                    continue
                _vals = _flatten_scale(_ba.get("scale"))
                if _vals and max(abs(v) for v in _vals) < 1e-6 and _bn not in hidden_default:
                    hidden_default.append(_bn)
    _hidden_set = set(hidden_default)
    # pass 2: find reveals (outside pre_parallel*) for each hidden bone. A reveal is
    # either a STATIC numeric scale >= threshold, or a MOLANG scale (held-item / armor
    # / TAC weapon swaps etc.) whose value is decided per-frame by queries. Molang
    # reveals are emitted as live evaluators so a base-hidden bone is never stuck
    # hidden, and only shows when its evaluated scale actually reaches threshold.
    for _ad in _anim_cache.values():
        for _an, _ao in (_ad.get("animations", {}) or {}).items():
            if not isinstance(_ao, dict) or str(_an).startswith("pre_parallel"):
                continue
            for _bn, _ba in (_ao.get("bones", {}) or {}).items():
                if _bn not in _hidden_set or not isinstance(_ba, dict):
                    continue
                _sc = _ba.get("scale")
                if _sc is None:
                    continue
                if _scale_has_molang(_sc):
                    # Molang-driven scale: the compiled scale is baked into the
                    # native bbmodel keyframes, which decide the per-frame
                    # magnitude (0 = hidden) at playback time. We only need to
                    # lift the base-hide while this anim is live; no live Lua
                    # evaluator -- native scale does the rest.
                    reveal_anims.setdefault(_bn, set()).add(_an)
                else:
                    _vals = _flatten_scale(_sc)
                    if _vals and max(_vals) >= SHOW_THRESHOLD:
                        reveal_anims.setdefault(_bn, set()).add(_an)
    if optimize:
        lines.append(f"\n-- base-hidden bones + reveal animations (Figura visibility optimizer)")
        # role-tagged bones are split into their own bbmodels, so they must not be
        # managed by the main model's visibility optimizer.
        _ann_skip = _force_visible | _force_hidden | _interp_bones | set(_roles.keys())
        _hd_emit = [b for b in sorted(hidden_default) if b not in _ann_skip]
        _hs_emit = [b for b in sorted(_hidden_set) if b not in _ann_skip]
        lines.append(f"ysm._hidden_default = {to_lua_val(_hd_emit)}")
        lines.append("ysm._reveal = {")
        for _bn in _hs_emit:
            lines.append(f"  [{to_lua_val(_bn)}] = {to_lua_val(sorted(reveal_anims.get(_bn, set())))},")
        lines.append("}")
        # Molang-driven scale reveals are folded into ysm._reveal above; their
        # per-frame magnitude is handled by the native baked bbmodel keyframes, so
        # no live Lua evaluator (ysm._reveal_dyn) is emitted anymore.
        lines.append("ysm._base_layers = {}")
    else:
        # ---- interpreter mode (no optimization) ----
        # Faithfully reproduce YSM's engine: instead of statically analysing which
        # bones the base layer hides and managing setVisible, keep every always-on
        # `pre_parallel*` base layer PLAYING continuously. The native baked scale
        # (0 => collapsed/hidden, ~1 => shown) then decides visibility per-frame,
        # exactly like the mod -- no hidden/reveal analysis at all.
        _base_layers = sorted({
            _an
            for _ad in _anim_cache.values()
            for _an, _ao in (_ad.get("animations", {}) or {}).items()
            if isinstance(_ao, dict) and str(_an).startswith("pre_parallel")
        })
        lines.append(f"\n-- interpreter mode: no visibility optimization; YSM base layers run as-is")
        lines.append("ysm._hidden_default = {}")
        lines.append("ysm._reveal = {}")
        lines.append(f"ysm._base_layers = {to_lua_val(_base_layers)}")
        lines.append("events.ENTITY_INIT:register(function()")
        lines.append("  local at=animations[ysm_model] or {}")
        lines.append("  for _,n in ipairs(ysm._base_layers) do local a=at[n]; if a then pcall(function() a:setPriority(0):play() end) end end")
        lines.append("end)")
        lines.append("events.TICK:register(function()")
        lines.append("  local at=animations[ysm_model] or {}")
        lines.append("  for _,n in ipairs(ysm._base_layers) do local a=at[n]; if a and not ysm_anim_playing(a) then pcall(function() a:setPriority(0):play() end) end end")
        lines.append("end)")

    # Animation controllers
    controllers = player.get("animation_controllers", [])
    if controllers:
        lines.append(f"\n-- animation controllers from YSM")
        for cp in controllers:
            cd = _read_json(ysm_path / cp)
            if not cd: continue
            for cname, cobj in (cd.get("animation_controllers", {}) or {}).items():
                states = cobj.get("states", {})
                lines.append(f"ysm_ctrls[{to_lua_val(cname)}] = {{")
                lines.append(f"  initial_state = {to_lua_val(cobj.get('initial_state','default'))},")
                lines.append(f"  states = {{")
                for sname, sobj in states.items():
                    lines.append(f"    [{to_lua_val(sname)}] = {{")
                    alist = sobj.get("animations", [])
                    apairs = []
                    for a in alist:
                        if isinstance(a, str):
                            apairs.append("{" + to_lua_val(a) + ",nil}")
                        elif isinstance(a, dict):
                            for k, cond in a.items():
                                lc = molang2lua(cond) if cond else "nil"
                                apairs.append("{" + to_lua_val(k) + ",function() return " + lc + " end}")
                    lines.append(f"      animations = {{{','.join(apairs)}}},")
                    trans = []
                    for tr in sobj.get("transitions", []):
                        if isinstance(tr, dict):
                            for target, cond in tr.items():
                                lc = molang2lua(cond)
                                trans.append("{" + to_lua_val(target) + ",function() return " + lc + " end}")
                    lines.append(f"      transitions = {{{','.join(trans)}}},")
                    oe = sobj.get("on_entry", [])
                    if oe:
                        oea = [f"function() {molang2lua(e)} end" for e in oe if isinstance(e, str)]
                        lines.append(f"      on_entry = {{{','.join(oea)}}},")
                    ox = sobj.get("on_exit", [])
                    if ox:
                        oxa = [f"function() {molang2lua(e)} end" for e in ox if isinstance(e, str)]
                        lines.append(f"      on_exit = {{{','.join(oxa)}}},")
                    bt = sobj.get("blend_transition")
                    if bt is not None and isinstance(bt, (int, float)):
                        lines.append(f"      blend_transition = {float(bt)},")
                    lines.append(f"    }},")
                lines.append(f"  }},")
                lines.append(f"}}")
    # Built-in locomotion animations: in YSM these standard state animations
    # (idle/walk/run/sneak/swim/fly/climb) are auto-played by the mod based on
    # player state, NOT by animation_controllers (which here only cover
    # expressions / extras). So we always emit a locomotion driver, regardless
    # of whether the model defines its own controllers.
    # Faithful port of YSM's player animation state machine:
    #   AnimationRegister.registerAnimationState() + AnimationManager.predicate()
    # (com.elfmcys.yesstevemodel.client.animation). YSM iterates priority buckets
    # HIGHEST(0) -> LOWEST(4) and, within a bucket, registration order; the FIRST
    # predicate that tests true wins. A flat first-match-wins list in that exact
    # order is therefore behaviorally identical. The leading {''} sentinel mirrors
    # AnimationManager's hard STOP when the player rides a live vehicle (ride/sit
    # poses are driven by the model's own controllers, not this locomotion driver).
    # MIN_SPEED 0.05 on limbSwingAmount -> player:isMoving(); getVerticalSpeed ->
    # velocity.y*20; pose/onClimbable/isVisuallySwimming map to the verified
    # Figura EntityAPI/LivingEntityAPI methods.
    loco_priority = [
        ("",                 "ysm_q.is_riding()"),                                            # AnimationManager: STOP while riding
        # ---- Priority.HIGHEST ----
        ("death",            "ysm_q.is_dead()"),                                              # isDeadOrDying()
        ("riptide",          "ysm_q.pose()=='SPIN_ATTACK'"),                                  # isAutoSpinAttack()
        ("sleep",            "ysm_q.pose()=='SLEEPING'"),                                     # pose==SLEEPING
        ("swim",             "ysm_q.is_swimming()"),                                          # isSwimming()
        ("climb",            "ysm_q.pose()=='SWIMMING' and ysm_q.is_moving()"),               # pose==SWIMMING && moving (crawl)
        ("climbing",         "ysm_q.pose()=='SWIMMING'"),                                     # pose==SWIMMING (crawl)
        ("ladder_up",        "ysm_q.is_climbing() and ysm_q.vertical_speed()>0.01"),          # onClimbable && vSpeed>0
        ("ladder_stillness", "ysm_q.is_climbing() and math.abs(ysm_q.vertical_speed())<=0.01"),# onClimbable && vSpeed==0
        ("ladder_down",      "ysm_q.is_climbing() and ysm_q.vertical_speed()<-0.01"),         # onClimbable && vSpeed<0
        # ---- Priority.HIGH ----
        ("fly",              "ysm_q.can_fly()"),                                              # abilities.flying (creative)
        ("elytra_fly",       "ysm_q.is_flying()"),                                            # FALL_FLYING && isFallFlying
        # ---- Priority.NORMAL ----
        ("swim_stand",       "ysm_q.is_in_water() and not ysm_q.is_on_ground()"),             # inWater && !onGround
        ("attacked",         "ysm_q.hurt_time()>0"),                                          # hurtTime>0 (PLAY_ONCE)
        ("jump",             "not ysm_q.is_on_ground() and not ysm_q.is_in_water()"),         # !onGround && !inWater
        ("sneak",            "ysm_q.is_on_ground() and ysm_q.pose()=='CROUCHING' and ysm_q.is_moving()"),# onGround && CROUCHING && moving
        ("sneaking",         "ysm_q.is_on_ground() and ysm_q.pose()=='CROUCHING'"),           # onGround && CROUCHING
        # ---- Priority.LOW ----
        ("run",              "ysm_q.is_on_ground() and ysm_q.is_sprinting()"),                # onGround && sprinting
        ("walk",             "ysm_q.is_on_ground() and ysm_q.is_moving()"),                   # onGround && limbSwing>0.05
        # ---- Priority.LOWEST ----
        ("idle",             "true"),                                                        # always
    ]
    # Keep states whose animation exists in the model; always keep the '' STOP sentinel.
    loco_entries = [(a, c) for a, c in loco_priority if a == "" or a in anim_names]
    if loco_entries:
        lines.append("\n-- built-in locomotion animations (auto-played by player state)")
        lines.append("ysm._loco = {")
        for a, c in loco_entries:
            lines.append("  {" + to_lua_val(a) + ",function() return " + c + " end},")
        lines.append("}")

    # Extra animations
    ea = props.get("extra_animation", {})
    buttons = props.get("extra_animation_buttons", [])
    if buttons:
        lines.append(f"\n-- extra animation buttons")
        lines.append(f"ysm._extra = {{")
        for btn in buttons:
            bid = btn.get("id", "")
            anim_ref = ea.get(bid, bid)
            lines.append(f"  [{to_lua_val(bid)}] = {{title={to_lua_val(btn.get('name',bid))},animation={to_lua_val(anim_ref)}}},")
        lines.append(f"}}")
        lines.append(f"ysm._buttons = {{")
        for btn in buttons[:8]:
            bid = btn.get("id", "")
            anim_ref = ea.get(bid, bid)
            lines.append(f"  {{id={to_lua_val(bid)},title={to_lua_val(btn.get('name',bid))},anim={to_lua_val(anim_ref)}}},")
        lines.append(f"}}")

    # ---- Action wheel (extra animations / roulette) ----
    # Build a multi-page Figura action wheel from YSM's roulette config so the
    # player can manually trigger the model's extra animations the same way YSM's
    # in-game roulette does. Fully data-driven from ysm.json (extra_animation /
    # extra_animation_classify / extra_animation_buttons); no per-model hardcoding.
    import re as _re_wheel
    def _wheel_clean(s):
        return _re_wheel.sub("\u00a7.", "", str(s)) if s is not None else ""
    def _wheel_varkey(val):
        s = str(val or "").strip()
        for pre in ("variable.", "v."):
            if s.startswith(pre):
                return s[len(pre):]
        return ""
    classify = props.get("extra_animation_classify", []) or []
    buttons_by_id = {b.get("id"): b for b in (buttons or []) if isinstance(b, dict) and b.get("id")}
    # Generic icons chosen purely by SLOT KIND (never by the project's category
    # names), so the wheel is fully project-agnostic: sub-menu links get one
    # icon, animations another, etc. No name-based / per-model mapping.
    _ICON_SUBPAGE = "minecraft:chest"     # opens a sub-page (category / config group)
    _ICON_ANIM = "minecraft:music_disc_cat"  # plays an extra animation
    _ICON_BACK = "minecraft:arrow"        # return to the main page
    _ICON_RANGE = "minecraft:clock"       # scroll-adjustable numeric var
    _ICON_RADIO = "minecraft:painting"    # click-to-cycle option var
    if ea or classify:
        main_slots = []
        sub_pages = {}
        for key, label in ea.items():
            lab = _wheel_clean(label)
            if isinstance(key, str) and key.startswith("#"):
                pid = key[1:]
                main_slots.append({"kind": "page", "title": lab or pid, "page": pid, "item": _ICON_SUBPAGE})
            elif isinstance(label, str) and label.startswith("#"):
                gid = label[1:]
                main_slots.append({"kind": "page", "title": _wheel_clean(gid), "page": "cfg:" + gid, "item": _ICON_SUBPAGE})
            else:
                main_slots.append({"kind": "anim", "title": lab or str(key), "anim": str(key), "item": _ICON_ANIM})
        for c in classify:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if not cid:
                continue
            slots = []
            for ak, alabel in (c.get("extra_animation") or {}).items():
                if not isinstance(ak, str):
                    continue
                if ak.startswith("#return"):
                    slots.append({"kind": "back", "title": _wheel_clean(alabel) or "\u8fd4\u56de", "item": _ICON_BACK})
                elif ak.startswith("#"):
                    continue
                else:
                    slots.append({"kind": "anim", "title": _wheel_clean(alabel) or ak, "anim": ak, "item": _ICON_ANIM})
            sub_pages[cid] = {"title": _wheel_clean(cid), "slots": slots}
        for gid, b in buttons_by_id.items():
            slots = [{"kind": "back", "title": "\u8fd4\u56de", "item": _ICON_BACK}]
            for form in (b.get("config_forms") or []):
                if not isinstance(form, dict):
                    continue
                ftype = form.get("type")
                vk = _wheel_varkey(form.get("value"))
                if not vk:
                    continue
                if ftype == "range":
                    mn = form.get("min")
                    df = mn if mn is not None else 1
                    slots.append({"kind": "scroll", "title": _wheel_clean(form.get("title", "")) or vk, "var": vk,
                                  "step": form.get("step", 0.1), "min": mn, "max": form.get("max"), "default": df,
                                  "item": _ICON_RANGE})
                elif ftype == "radio":
                    vals = []
                    for _lbl, expr in (form.get("labels") or {}).items():
                        m = _re_wheel.search(r"=\s*(-?\d+(?:\.\d+)?)", str(expr))
                        vals.append(float(m.group(1)) if m else 0)
                    slots.append({"kind": "cycle", "title": _wheel_clean(form.get("title", "")) or vk, "var": vk,
                                  "values": vals, "item": _ICON_RADIO})
            sub_pages["cfg:" + gid] = {"title": _wheel_clean(b.get("name", gid)), "slots": slots}
        lines.append("\n-- action wheel (extra animations / roulette)")
        lines.append("ysm._wheel = " + to_lua_val({"main": {"title": "YSM", "slots": main_slots}, "pages": sub_pages}))

    lines.append(f"\n-- dynamic bone expressions from Molang keyframes")
    if dynamic_bones:
        lines.append(f"ysm._dynamic_bones = {{")
        for (anim_name, bone_name, channel), parts in dynamic_bones.items():
            xl = f"function()return({parts.get('x','0')})end"
            yl = f"function()return({parts.get('y','0')})end"
            zl = f"function()return({parts.get('z','0')})end"
            ch = {"rotation":"rot","position":"pos","scale":"scale"}.get(channel, channel)
            lines.append(f"  {{anim={to_lua_val(anim_name)},bone={to_lua_val(bone_name)},ch={to_lua_val(ch)},x={xl},y={yl},z={zl}}},")
        lines.append(f"}}")
    else:
        lines.append(f"ysm._dynamic_bones = {{}}")

    lines.append(YSM_HOLD_AND_LOCO_OVERRIDE)

    # ---- annotation runtime: force visibility + roles + per-bone interpreter ----
    # Registered AFTER the template RENDER override so these forced setVisible
    # calls win the final say each frame.
    if _force_hidden or _force_visible or _roles or _interp_bones:
        lines.append("\n-- player annotations (compile-directed): force visibility, roles, interpreter fallback")
        lines.append(f"ysm._force_hidden = {to_lua_val(sorted(_force_hidden))}")
        lines.append(f"ysm._force_visible = {to_lua_val(sorted(_force_visible))}")
        lines.append(f"ysm._interp_bones = {to_lua_val(sorted(_interp_bones))}")
        lines.append("ysm._roles = {")
        for _b in sorted(_roles):
            lines.append(f"  [{to_lua_val(_b)}] = {to_lua_val(_roles[_b])},")
        lines.append("}")
        # bone -> standalone bbmodel stem (loaded as models.<stem>), independently controllable
        lines.append("ysm._role_models = {")
        for _b in sorted(_role_models):
            _rm = _role_models[_b]
            _stem = _rm["stem"] if isinstance(_rm, dict) else _rm
            lines.append(f"  [{to_lua_val(_b)}] = {to_lua_val(_stem)},")
        lines.append("}")
        # bone -> original parent bone name in the main model (false => follow model root).
        # The split model follows this parent's world matrix every frame (O(parts),
        # the split's OWN keyframe animations stay native -- no animation-to-Lua).
        lines.append("ysm._role_parents = {")
        for _b in sorted(_role_models):
            _rm = _role_models[_b]
            _parent = _rm.get("parent") if isinstance(_rm, dict) else None
            lines.append(f"  [{to_lua_val(_b)}] = {to_lua_val(_parent) if _parent else 'false'},")
        lines.append("}")
        # ---- role-split model runtime: logical re-parenting via matrix follow ----
        # held_item / generic: follow the original parent bone's WORLD matrix.
        # firstperson_hand: render only in first person, anchored to the host arm pivot.
        # NOTE: world<->model space conversion + arm pivot need in-game calibration.
        # ysm._role_calib[bone] may hold a Matrix4 offset applied after the follow.
        lines.append("ysm._role_calib = ysm._role_calib or {}")
        lines.append("events.RENDER:register(function(delta, ctx)")
        lines.append("  local main = models[ysm_model]; if not main then return end")
        lines.append("  for bn, stem in pairs(ysm._role_models) do")
        lines.append("    local sm = models[stem]")
        lines.append("    if sm then")
        lines.append("      local role = ysm._roles[bn]")
        lines.append("      if role == 'firstperson_hand' then")
        lines.append("        local fp = (ctx == 'FIRST_PERSON')")
        lines.append("        sm:setVisible(fp)")
        lines.append("        if fp then")
        lines.append("          local arm = ysm_find_bone(main, 'RightArm') or ysm_find_bone(main, 'rightArm')")
        lines.append("          if arm then")
        lines.append("            local m = arm:partToWorldMatrix()")
        lines.append("            local c = ysm._role_calib[bn]")
        lines.append("            sm:setMatrix(c and (m * c) or m)  -- CALIBRATE: arm pivot offset")
        lines.append("          end")
        lines.append("        end")
        lines.append("      else")
        lines.append("        local pn = ysm._role_parents[bn]")
        lines.append("        local pp = (pn and ysm_find_bone(main, pn)) or main")
        lines.append("        if pp then")
        lines.append("          local m = pp:partToWorldMatrix()")
        lines.append("          local c = ysm._role_calib[bn]")
        lines.append("          sm:setMatrix(c and (m * c) or m)  -- CALIBRATE: world<->model offset")
        lines.append("        end")
        lines.append("      end")
        lines.append("    end")
        lines.append("  end")
        lines.append("end)")
        lines.append("events.RENDER:register(function()")
        lines.append("  local m=models[ysm_model]; if not m then return end")
        lines.append("  for _,bn in ipairs(ysm._force_hidden) do pcall(function() local p=ysm_find_bone(m,bn); if p then p:setVisible(false) end end) end")
        lines.append("  for _,bn in ipairs(ysm._force_visible) do pcall(function() local p=ysm_find_bone(m,bn); if p then p:setVisible(true) end end) end")
        lines.append("  for _,bn in ipairs(ysm._interp_bones) do pcall(function() local p=ysm_find_bone(m,bn); if p then p:setVisible(true) end end) end")
        lines.append("end)")
        if _interp_bones and optimize:
            _bl = sorted({
                _an
                for _ad in _anim_cache.values()
                for _an, _ao in (_ad.get("animations", {}) or {}).items()
                if isinstance(_ao, dict) and str(_an).startswith("pre_parallel")
            })
            lines.append("-- interpreter-fallback bones: run YSM base layers so native scale drives them")
            lines.append(f"ysm._base_layers = {to_lua_val(_bl)}")
            lines.append("events.ENTITY_INIT:register(function()")
            lines.append("  local at=animations[ysm_model] or {}")
            lines.append("  for _,n in ipairs(ysm._base_layers) do local a=at[n]; if a then pcall(function() a:setPriority(0):play() end) end end")
            lines.append("end)")
            lines.append("events.TICK:register(function()")
            lines.append("  local at=animations[ysm_model] or {}")
            lines.append("  for _,n in ipairs(ysm._base_layers) do local a=at[n]; if a and not ysm_anim_playing(a) then pcall(function() a:setPriority(0):play() end) end end")
            lines.append("end)")

    # YSM 2.5.0 custom-function / script (molang script) support
    try:
        from . import script_gen
        _files_section = ysm_json.get("files", {}) if isinstance(ysm_json, dict) else {}
        _script_block, _script_summary = script_gen.gen_scripts(ysm_path, _files_section)
        if _script_block:
            lines.append(_script_block)
            print(f"    [scripts] {_script_summary}")
    except Exception as _e:
        print(f"    [scripts] skipped: {_e}")

    lines.append(f"\n-- end of generated YSM avatar")

    main_lua = '\n'.join(lines)
    p = project_dir / "main.lua"
    with open(p, "w", encoding="utf-8") as f:
        f.write(main_lua)
    print(f"  {p.name}: {len(main_lua.splitlines())} lines")
