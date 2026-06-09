"""YSM Bedrock JSON -> .bbmodel geometry + animation converter."""

import json
import uuid
import base64
import re
from pathlib import Path

from .lua_gen import molang2lua

# --- Bedrock(left-handed) -> Blockbench/Figura "free"(right-handed) X-axis flip ---
# geckolib renders raw Bedrock geometry inside Minecraft's entity renderer, which
# applies poseStack.scale(-1,-1,1). Figura's "free" format does NOT, so without this
# flip the whole model and its animations render mirrored on the X axis.
def _neg(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return -float(v)
    return "-(" + str(v) + ")"

def _flip_x_elements(elements):
    # Geometry X-mirror is now applied inline in convert_geometry_to_bb (matching
    # Blockbench's bedrock parseCube/parseBone exactly), so this hook is a no-op.
    return

def _flip_x_group(g):
    # See _flip_x_elements: group origin/rotation flip is applied inline.
    return

def _flip_x_anim(bb):
    # Match Blockbench's bedrock animation importer (getKeyframeDataPoints):
    #   position -> invert X
    #   rotation -> invert X and Y
    #   scale    -> unchanged
    # IMPORTANT: Blockbench only mirrors a keyframe whose Bedrock source value is an
    # [x, y, z] ARRAY. A scalar source value (e.g. `-1.0`) is broadcast to all three
    # axes uniformly and is NOT mirrored on X. Scalar-sourced keyframes are tagged
    # with `_src_is_array = False` at build time; honor that flag here so our output
    # matches a normal Blockbench bedrock import (otherwise scalar position/rotation
    # keyframes get their X/Y angle wrongly negated).
    # Do NOT bake the X-axis inversion into the stored keyframe values.
    # Both loaders invert position.x / rotation.x / rotation.y themselves on load:
    #   - Blockbench: the format_version < 5.0 import migration (bbmodel.js)
    #   - Figura: its bbmodel loader follows the old (pre-5.0) bedrock convention
    # If we baked the flip in here, those loaders would invert a second time and
    # mirror the animation in-game (normal in Blockbench, reversed in Figura).
    # We keep the values in the original (bedrock) orientation and only strip the
    # internal bookkeeping flag so it doesn't leak into the output file.
    for an in (bb.get("animators") or {}).values():
        for kf in (an.get("keyframes") or []):
            kf.pop("_src_is_array", None)

def _flip_x_dynamic(db):
    for key, parts in db.items():
        ch = key[2] if isinstance(key, tuple) and len(key) >= 3 else None
        if not isinstance(parts, dict):
            continue
        if ch == "position" and "x" in parts:
            parts["x"] = _neg(parts["x"])
        elif ch == "rotation":
            if "x" in parts:
                parts["x"] = _neg(parts["x"])
            if "y" in parts:
                parts["y"] = _neg(parts["y"])

# ------------------------------------------------------------
#  Utils
# ------------------------------------------------------------
def make_uuid():
    return str(uuid.uuid4())

def read_file(path: Path):
    if not path or not path.exists():
        return None
    with open(path, 'rb') as f:
        return f.read()

def read_json(path: Path):
    data = read_file(path)
    if not data:
        return None
    return json.loads(data.decode('utf-8', errors='replace'))

# ------------------------------------------------------------
#  Geometry conversion  (Bedrock -> Blockbench)
# ------------------------------------------------------------
def bedrock_uv_to_bb(uv, uv_size):
    u, v = (uv or [0, 0])[:2]
    w, h = (uv_size or [0, 0])[:2]
    return [u, v, u + w, v + h]

def generate_bb_cube_faces(cube, tex_width, tex_height, mirror, texture_id=0):
    bb_faces = {}
    uv_data = cube.get("uv")
    if uv_data is None:
        return bb_faces
    size = cube.get("size", [1, 1, 1])
    dx, dy, dz = float(size[0]), float(size[1]), float(size[2])
    face_names = ["north", "south", "east", "west", "up", "down"]

    def emit(bf_name, bb_uv):
        if mirror:
            bb_uv = [bb_uv[2], bb_uv[1], bb_uv[0], bb_uv[3]]
        # Blockbench bedrock import reverses the UV corners on the top/bottom
        # faces: face.uv = [uv[2], uv[3], uv[0], uv[1]]
        if bf_name in ("up", "down"):
            bb_uv = [bb_uv[2], bb_uv[3], bb_uv[0], bb_uv[1]]
        bb_faces[bf_name] = {"uv": bb_uv, "texture": texture_id}

    if isinstance(uv_data, dict):
        for bf in face_names:
            fd = uv_data.get(bf)
            if not fd:
                continue
            bb_uv = bedrock_uv_to_bb(fd.get("uv", [0, 0]), fd.get("uv_size", [0, 0]))
            emit(bf, bb_uv)
    elif isinstance(uv_data, list) and len(uv_data) >= 2:
        ux, uy = float(uv_data[0]), float(uv_data[1])
        box = {
            "north": [ux + dz, uy + dz, dx, dy],
            "south": [ux + dz + dx + dz, uy + dz, dx, dy],
            "east": [ux, uy + dz, dz, dy],
            "west": [ux + dz + dx, uy + dz, dz, dy],
            "up": [ux + dz, uy, dx, dz],
            "down": [ux + dz + dx, uy + dz, dx, -dz],
        }
        for bf, (u, v, w, h) in box.items():
            emit(bf, [u, v, u + w, v + h])
    return bb_faces

def _add_item_pivots(outliner):
    """Make vanilla held items follow the model's animated hand.

    YSM anchors the held item at the `RightHandLocator` / `LeftHandLocator`
    bones (see OpenYSM rightHandBones()/leftHandBones() + YSMClientMapper).
    Figura instead renders held items at a bone whose parent type is
    Right/LeftItemPivot. Without such a bone the game falls back to the vanilla
    arm, so items that have no replacement model bone (sword, shield, trident,
    food, ...) render at the vanilla position and ignore the model's pose.

    We add one empty pivot bone as a child of each hand locator, inheriting its
    (animated) transform, so the item tracks the model's hand. This is generic:
    it keys off YSM's standard locator names, not any specific model/item.
    """
    mapping = (("RightHandLocator", "RightItemPivot"),
               ("LeftHandLocator", "LeftItemPivot"))
    for loc_name, pivot_name in mapping:
        grp, _ = _find_group_and_parent(outliner, loc_name)
        if not isinstance(grp, dict):
            continue
        kids = grp.get("children")
        if not isinstance(kids, list):
            kids = []
            grp["children"] = kids
        if any(isinstance(c, dict) and c.get("name") == pivot_name for c in kids):
            continue
        # zero local offset: pivot sits exactly on the locator (= grip point)
        origin = [float(v) for v in (grp.get("origin") or [0, 0, 0])]
        kids.append({
            "name": pivot_name,
            "uuid": make_uuid(),
            "origin": origin,
            "rotation": [0, 0, 0],
            "visibility": True,
            "export": True,
            "pt": pivot_name,
        })


def convert_geometry_to_bb(geo_data, texture_id=0):
    """Return (elements, outliner, bone_to_uuid, bone_children, all_bones, bone_paths)."""
    elements, bone_el_refs = [], {}
    bone_to_uuid, bone_children = {}, {}
    all_bones = []
    bone_paths = {}

    geometries = (geo_data or {}).get("minecraft:geometry", [])
    if not geometries:
        return elements, [], bone_to_uuid, bone_children, all_bones, bone_paths

    geo = geometries[0]
    bones = geo.get("bones", [])
    bone_dict = {}

    # pass 1: collect
    for bone in bones:
        name = bone.get("name", "unknown")
        parent = bone.get("parent", "")
        bone_dict[name] = bone
        all_bones.append(bone)
        bone_to_uuid[name] = make_uuid()
        if parent:
            bone_children.setdefault(parent, []).append(name)

    # build bone_paths for recursive lookup
    def _build_path(bname, path=()):
        bp = path + (bname,)
        if bname in bone_paths:
            bone_paths[bname] = bp
        else:
            bone_paths[bname] = bp
        for c in bone_children.get(bname, []):
            _build_path(c, bp)
    for root in [b for b in all_bones if not b.get("parent", "")]:
        _build_path(root["name"])

    desc = geo.get("description", {})
    tw = float(desc.get("texture_width", 64))
    th = float(desc.get("texture_height", 64))

    # pass 2: elements from cubes
    for bone in bones:
        name = bone.get("name", "unknown")
        bmirror = bone.get("mirror", False)
        binfl = float(bone.get("inflate", 0))
        uuids = []
        for idx, cube in enumerate(bone.get("cubes", [])):
            origin = cube.get("origin", [0, 0, 0])
            size = cube.get("size", [1, 1, 1])
            ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
            sx, sy, sz = float(size[0]), float(size[1]), float(size[2])
            el = {
                "name": f"{name}_cube_{idx}",
                "type": "cube",
                "uuid": make_uuid(),
                # Blockbench bedrock parseCube mirrors geometry on X:
                #   from[0] = -(origin[0] + size[0]);  to[0] = -origin[0]
                # Y and Z are unchanged.
                "from": [-(ox + sx), oy, oz],
                "to": [-ox, oy + sy, oz + sz],
                "inflate": float(cube.get("inflate", binfl)),
                "visibility": True, "export": True,
            }
            cp = cube.get("pivot")
            if cp and any(abs(v) > 0.001 for v in cp):
                # cube rotation pivot: X negated
                el["origin"] = [-float(cp[0]), float(cp[1]), float(cp[2])]
            cr = cube.get("rotation", [0, 0, 0])
            if any(abs(v) > 0.001 for v in cr):
                # cube rotation: X and Y negated, Z unchanged
                el["rotation"] = [-float(cr[0]), -float(cr[1]), float(cr[2])]
            faces = generate_bb_cube_faces(cube, tw, th, cube.get("mirror", bmirror), texture_id)
            el["faces"] = faces or {}
            elements.append(el)
            uuids.append(el["uuid"])
        bone_el_refs[name] = uuids

    # parent type helper
    def pt(name):
        nl = name.lower().replace(" ", "").replace("_", "")
        if nl in ("allhead", "mhead", "head"): return "Head"
        if "leftarm" in nl: return "LeftArm"
        if "rightarm" in nl: return "RightArm"
        if nl in ("body", "upperbody", "upbody", "downbody", "allbody", "mallbody"): return "Body"
        if "leftleg" in nl: return "LeftLeg"
        if "rightleg" in nl: return "RightLeg"
        if "elytra" in nl: return "Elytra"
        if "cape" in nl: return "Cape"
        return ""

    def build_outliner(bname):
        bone = bone_dict.get(bname)
        if not bone: return None
        pivot = bone.get("pivot", [0, 0, 0])
        rot = bone.get("rotation", [0, 0, 0])
        groups = list(bone_el_refs.get(bname, []))
        for c in bone_children.get(bname, []):
            g = build_outliner(c)
            if g: groups.append(g)
        g = {
            "name": bname, "uuid": bone_to_uuid[bname],
            # Blockbench bedrock parseBone: group origin X negated,
            # group rotation X and Y negated (Z unchanged).
            "origin": [-float(pivot[0]), float(pivot[1]), float(pivot[2])],
            "rotation": [-float(rot[0]), -float(rot[1]), float(rot[2])],
            "visibility": True, "export": True,
        }
        p = pt(bname)
        if p: g["pt"] = p
        if groups: g["children"] = groups
        return g

    roots = [b for b in all_bones if not b.get("parent", "")]
    outliner = [build_outliner(b["name"]) for b in roots if build_outliner(b["name"])]
    _flip_x_elements(elements)
    for _g in outliner:
        _flip_x_group(_g)
    # Item pivots are added AFTER the X-flip so we copy the already-flipped
    # locator origin verbatim (no further coordinate transform needed).
    _add_item_pivots(outliner)
    return elements, outliner, bone_to_uuid, bone_children, all_bones, bone_paths

# ------------------------------------------------------------
#  Animation conversion
# ------------------------------------------------------------
def _is_molang_str(v):
    if not isinstance(v, str): return False
    v = v.strip()
    if not v: return False
    try:
        float(v)
        return False
    except ValueError:
        return True

def _try_numeric(v):
    if isinstance(v, (int, float)): return float(v)
    if not isinstance(v, str): return 0.0
    try:
        return float(v.strip())
    except ValueError:
        m = re.match(r'^\s*(-?[\d.]+)', v)
        if m: return float(m.group(1))
        return 0.0

def _molang_or_numeric(v, channel):
    if _is_molang_str(v):
        lua = molang2lua(v)
        return _try_numeric(v), lua
    return float(v) if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip()) else 0.0, None

def _channel_name(cn):
    return {0: "x", 1: "y", 2: "z"}.get(cn, str(cn))

def _safe_kf(expr):
    # Firewall every baked keyframe expression: if it errors at runtime or does
    # not yield a number, fall back to 0 instead of throwing. In Figura a single
    # keyframe eval error calls luaRuntime.error -> scriptError=true, which kills
    # the ENTIRE avatar (all animations + script stop). This makes one bad
    # keyframe a local, harmless 0 instead of a fatal avatar failure.
    # Expose the currently-playing animation's LOCAL time (animation:getTime(), seconds, resets on
    # (re)play and wraps on loop) as query.anim_time during keyframe evaluation, matching YSM/Bedrock
    # semantics. Without this, q.anim_time resolves to the global avatar clock and time-based keyframe
    # exprs (e.g. eye-highlight glints whose envelope is (0.5 - anim_time)) explode.
    #
    # Figura evaluates keyframe values via Avatar.run(fn, owner.animation, delta, animation), but
    # owner.animation is the Instructions *limit* arg of run(toRun, limit, args...), NOT a vararg.
    # FiguraLuaRuntime.run forwards only `args` to the Lua function, so the function actually receives
    # varargs (delta, animation): the Animation is select(2,...), and select(3,...) is nil. Reading a
    # fixed index was the bug. Scan every vararg for the object that answers :getTime() so we stay
    # correct regardless of arg order/count. _kf_at is restored afterwards so non-keyframe contexts
    # keep the global clock.
    return ("(function(...) local _prev=ysm_state._kf_at "
            "for _i=1,select('#',...) do local _c=select(_i,...) local _tc=type(_c) "
            "if _tc=='userdata' or _tc=='table' then "
            "local ok2,t=pcall(function() return _c:getTime() end) "
            "if ok2 and type(t)=='number' then ysm_state._kf_at=t break end end end "
            "local ok,r=pcall(function() return " + expr +
            " end) ysm_state._kf_at=_prev if ok and type(r)=='number' then return r end return 0 end)(...)")

def _kv_to_dp(val):
    """Convert a keyframe value (scalar, [x,y,z], or MoLang string) to a data_point dict.
    For MoLang expressions, converts to Lua and embeds as a string in the data_point
    (Figura's animation system evaluates these at runtime).
    Returns (dp dict or None)."""
    if isinstance(val, (int, float)):
        return {"x": float(val), "y": float(val), "z": float(val)}
    if isinstance(val, list) and len(val) >= 3:
        dp = {}
        for ci in range(3):
            v = val[ci] if ci < len(val) else 0.0
            nv, lv = _molang_or_numeric(v, _channel_name(ci))
            if lv:
                dp[_channel_name(ci)] = _safe_kf(str(lv))
            else:
                dp[_channel_name(ci)] = nv
        return dp
    if isinstance(val, str):
        nv, lv = _molang_or_numeric(val, "x")
        if lv:
            s = _safe_kf(str(lv))
            return {"x": s, "y": s, "z": s}
        return {"x": nv, "y": nv, "z": nv}
    return None


def _smooth_interp_fallback(bb):
    """R1: YSM source keyframes are predominantly catmullrom; the converter falls
    back to "linear" whenever a keyframe carries no explicit lerp_mode. Linear does
    not smooth the loop wrap, so short looping cycles (run/walk) visibly jerk once
    per loop ("鬼畜"). Make catmullrom the fallback for rotation/position so the
    loop is smoothed. Scale keeps linear to avoid catmullrom overshoot on 0<->1
    visibility toggles (weapon show/hide). Explicit "step" is always preserved."""
    for guuid, an in (bb.get("animators") or {}).items():
        if guuid == "effects":
            continue
        for kf in an.get("keyframes", []):
            if kf.get("channel") == "scale":
                continue
            if kf.get("interpolation") == "linear":
                kf["interpolation"] = "catmullrom"
    return bb


def _base_hidden_bones(anim_data):
    """R4: YSM applies its `pre_parallel*` animations as an always-on base layer
    that establishes the default gameplay pose. A bone scaled to 0 by that base
    layer is hidden by default in gameplay (e.g. the GUI picture frame) and only
    revealed by the GUI-only `preview_animation`. Derive hidden bones from that
    base data instead of matching bone names, so it generalizes to any model."""
    hidden = set()
    for an, ao in (anim_data.get("animations", {}) or {}).items():
        if not isinstance(ao, dict):
            continue
        if not str(an).startswith("pre_parallel"):
            continue
        for bn, ba in (ao.get("bones", {}) or {}).items():
            if not isinstance(ba, dict):
                continue
            sc = ba.get("scale")
            vals = None
            if isinstance(sc, (int, float)):
                vals = [sc, sc, sc]
            elif isinstance(sc, list):
                vals = sc
            elif isinstance(sc, dict):
                flat = []
                ok = True
                for kv in sc.values():
                    if isinstance(kv, dict):
                        kv = kv.get("post", kv.get("pre", kv))
                    if isinstance(kv, (int, float)):
                        flat.append([kv, kv, kv])
                    elif isinstance(kv, list):
                        flat.append(kv)
                    else:
                        ok = False
                if ok and flat and all(f == flat[0] for f in flat):
                    vals = flat[0]
            if vals is not None:
                nums = [v for v in vals if isinstance(v, (int, float))]
                if nums and all(abs(float(v)) < 1e-6 for v in nums):
                    hidden.add(bn)
    return hidden


def _apply_hidden_bones(outliner, hidden):
    """R4: set default visibility=False on bones the base layer hides."""
    if not hidden:
        return
    def walk(g, inherited=False):
        if isinstance(g, dict):
            h = inherited or (g.get("name") in hidden)
            if h:
                g["visibility"] = False
            for c in (g.get("children", []) or []):
                walk(c, h)
    for g in outliner:
        walk(g)


def convert_animations_to_bb(anim_data, bone_to_uuid, model_name):
    bb_anims = []
    dynamic_bones = {}
    for anim_name, anim_obj in (anim_data.get("animations", {}) or {}).items():
        if not isinstance(anim_obj, dict): continue
        loop = anim_obj.get("loop", "once")
        if loop is True: loop = "loop"
        elif loop == "hold_on_last_frame": loop = "hold"
        else: loop = "once"
        bb = {
            "name": anim_name, "loop": loop,
            "length": float(anim_obj.get("animation_length", 0)),
            "animators": {}, "override": True,
        }
        for bone_name, bone_anim in (anim_obj.get("bones", {}) or {}).items():
            if not isinstance(bone_anim, dict): continue
            guuid = bone_to_uuid.get(bone_name)
            if not guuid:
                for bn, bu in bone_to_uuid.items():
                    if bn.lower().replace(" ", "") == bone_name.lower().replace(" ", ""):
                        guuid = bu; break
                if not guuid: continue
            kfs = []
            for channel in ("rotation", "position", "scale"):
                cd = bone_anim.get(channel)
                if cd is None: continue
                if isinstance(cd, (int, float)):
                    dp = {"x": float(cd), "y": float(cd), "z": float(cd)}
                    kfs.append({"channel": channel, "interpolation": "linear", "time": 0.0,
                                "data_points": [dp], "_src_is_array": False})
                elif isinstance(cd, list):
                    has_molang = any(_is_molang_str(v) for v in cd if v is not None)
                    if has_molang:
                        dp = {}
                        for ci in range(3):
                            v = cd[ci] if ci < len(cd) else 0.0
                            nv, lv = _molang_or_numeric(v, _channel_name(ci))
                            # Route through _safe_kf like the dict path so these constant-molang
                            # keyframes also get the crash firewall AND local query.anim_time.
                            dp[_channel_name(ci)] = _safe_kf(str(lv)) if lv else float(nv)
                        kfs.append({"channel": channel, "interpolation": "linear", "time": 0.0,
                                    "data_points": [dp], "_src_is_array": True})
                    else:
                        dp = {}
                        for ci in range(3):
                            v = cd[ci] if ci < len(cd) else 0.0
                            dp[_channel_name(ci)] = float(v) if isinstance(v, (int, float)) else _try_numeric(v)
                        kfs.append({"channel": channel, "interpolation": "linear", "time": 0.0,
                                    "data_points": [dp], "_src_is_array": True})
                elif isinstance(cd, dict):
                    for ts, kd in sorted(cd.items(), key=lambda x: float(x[0]) if x[0].replace('.','').replace('-','').isdigit() else 0):
                        try: tv = float(ts)
                        except: continue
                        if isinstance(kd, dict):
                            lm = {"catmullrom": "catmullrom", "linear": "linear", "step": "step"}.get(kd.get("lerp_mode"), "linear")
                            post = kd.get("post", kd.get("pre", 0.0))
                            pre = kd.get("pre")
                            dp_post = _kv_to_dp(post)
                            if dp_post is None: continue
                            kf = {"channel": channel, "interpolation": lm, "time": tv, "data_points": [dp_post],
                                  "_src_is_array": isinstance(post, list) or isinstance(pre, list)}
                            if pre is not None:
                                dp_pre = _kv_to_dp(pre)
                                if dp_pre:
                                    kf["data_points"] = [dp_pre, dp_post]
                            kfs.append(kf)
                        elif isinstance(kd, (int, float)):
                            dp = {"x": float(kd), "y": float(kd), "z": float(kd)}
                            kfs.append({"channel": channel, "interpolation": "linear", "time": tv,
                                        "data_points": [dp], "_src_is_array": False})
                        elif isinstance(kd, list):
                            dp = _kv_to_dp(kd)
                            if dp:
                                kfs.append({"channel": channel, "interpolation": "linear", "time": tv,
                                            "data_points": [dp], "_src_is_array": True})
                        elif isinstance(kd, str):
                            dp = _kv_to_dp(kd)
                            if dp:
                                kfs.append({"channel": channel, "interpolation": "linear", "time": tv,
                                            "data_points": [dp], "_src_is_array": False})
            if kfs:
                bb["animators"][guuid] = {"keyframes": kfs}
        # timeline
        tl = anim_obj.get("timeline", {})
        if tl:
            ekfs = []
            for ts, evts in tl.items():
                try: tv = float(ts)
                except: continue
                if isinstance(evts, list):
                    script = " ".join(str(x) for x in evts)
                else:
                    script = str(evts)
                try:
                    _lua = molang2lua(script)
                except Exception:
                    _lua = "nil"
                # YSM binds query.anim_time inside timeline/instruction keyframes to the
                # playing animation's LOCAL time (executeTo path: setAnimTime(adjustedTick/20)),
                # NOT the global avatar clock. Figura fires an instruction keyframe at its own
                # timeline position, so animation:getTime() at fire time == that local tick,
                # matching the mod. Bind _kf_at for the duration of the instruction (by anim
                # name, robust to Figura not forwarding the Animation as a vararg here) and
                # restore afterwards, exactly like _safe_kf does for baked bone-value exprs.
                _an_lit = "'" + anim_name.replace("\\", "\\\\").replace("'", "\\'") + "'"
                _wrapped = ("(function() local _p=ysm_state._kf_at "
                            "local _A=(animations[ysm_model] or {})[" + _an_lit + "] "
                            "if _A then local ok,t=pcall(function() return _A:getTime() end) "
                            "if ok and type(t)=='number' then ysm_state._kf_at=t end end "
                            "pcall(function() return " + _lua + " end) "
                            "ysm_state._kf_at=_p end)()")
                ekfs.append({"channel": "timeline", "interpolation": "linear", "time": tv,
                             "data_points": [{"script": _wrapped}]})
            if ekfs:
                bb["animators"]["effects"] = {"keyframes": ekfs}
        _flip_x_anim(bb)
        _smooth_interp_fallback(bb)
        bb_anims.append(bb)
    _flip_x_dynamic(dynamic_bones)
    return bb_anims, dynamic_bones

# ------------------------------------------------------------
#  Texture handling
# ------------------------------------------------------------
def build_texture_list(ysm_path, file_entry, model_name):
    texs = file_entry.get("texture", [])
    if isinstance(texs, str): texs = [texs]
    bb_textures = []
    for idx, te in enumerate(texs):
        tp = te if isinstance(te, str) else (list(te.values())[0] if isinstance(te, dict) else "")
        p = ysm_path / tp
        b64, w, h = None, None, None
        rp = f"textures/{model_name}.{Path(tp).name}"
        if p.exists():
            raw = p.read_bytes()
            b64 = "data:image/png;base64," + base64.b64encode(raw).decode()
            if raw[:8] == b'\x89PNG\r\n\x1a\n' and len(raw) >= 24:
                w = (raw[16]<<24)|(raw[17]<<16)|(raw[18]<<8)|raw[19]
                h = (raw[20]<<24)|(raw[21]<<16)|(raw[22]<<8)|raw[23]
        name = Path(tp).stem
        bt = {"name": name, "relative_path": rp, "source": b64 or ""}
        if w: bt["width"] = float(w); bt["uv_width"] = float(w)
        if h: bt["height"] = float(h); bt["uv_height"] = float(h)
        bb_textures.append(bt)
    return bb_textures

# ------------------------------------------------------------
#  Build .bbmodel JSON
# ------------------------------------------------------------
def build_bbmodel(elements, outliner, animations, textures, tw, th, name):
    m = {
        "meta": {"format_version": "4.5", "model_format": "free", "box_uv": False, "predicate_name": name},
        "name": name, "resolution": {"width": tw, "height": th},
        "elements": elements, "outliner": outliner,
    }
    if textures: m["textures"] = textures
    if animations: m["animations"] = animations
    return m

# ------------------------------------------------------------
#  Main entry (called by lua_gen)
# ------------------------------------------------------------
def _find_group_and_parent(container, bone_name):
    """Find a bone group by name in an outliner; return (group, containing_list)."""
    for g in container:
        if isinstance(g, dict):
            if g.get("name") == bone_name:
                return g, container
            ch = g.get("children")
            if isinstance(ch, list):
                res = _find_group_and_parent(ch, bone_name)
                if res[0]:
                    return res
    return None, None


def _collect_subtree(group, el_uuids, bone_uuids):
    """Collect element uuids (string children) and bone uuids in a group subtree."""
    if not isinstance(group, dict):
        return
    bu = group.get("uuid")
    if bu:
        bone_uuids.add(bu)
    for c in (group.get("children", []) or []):
        if isinstance(c, str):
            el_uuids.add(c)
        elif isinstance(c, dict):
            _collect_subtree(c, el_uuids, bone_uuids)


def _find_parent_bone_name(container, bone_name, parent_name=None):
    """Return (found, parent_bone_name) for a bone in the outliner.
    parent_bone_name is None when the bone sits at the outliner top level
    (i.e. its split model should follow the main model root, not a bone)."""
    for g in container:
        if isinstance(g, dict):
            if g.get("name") == bone_name:
                return True, parent_name
            ch = g.get("children")
            if isinstance(ch, list):
                f, p = _find_parent_bone_name(ch, bone_name, g.get("name"))
                if f:
                    return True, p
    return False, None


def scaffold_annotations(ysm_path, files_section):
    """Build a starter ysm_annotations.template.json: every base-hidden bone with
    a guessed semantic role. The player then promotes entries to force_visible /
    force_hidden / interpreter, or keeps/edits the guessed role (role => the bone
    subtree is split into its own bbmodel)."""
    player = files_section.get("player", {})
    base_hidden = set()
    for _ak, _ap in (player.get("animation", {}) or {}).items():
        _ad = read_json(ysm_path / _ap)
        if _ad:
            base_hidden |= _base_hidden_bones(_ad)
    def _guess(bn):
        n = bn.lower()
        if any(k in n for k in ("hand", "arm", "palm", "finger")):
            return "firstperson_hand"
        if any(k in n for k in ("item", "hold", "weapon", "sword", "bow", "gun",
                                "tool", "shield", "sheath", "scabbard", "tachi",
                                "sbd", "blade", "katana", "knife", "axe", "staff")):
            return "held_item"
        return None
    bones = {}
    for bn in sorted(base_hidden):
        entry = {"mode": "optimize"}
        g = _guess(bn)
        if g:
            entry["role"] = g
        bones[bn] = entry
    return {
        "_comment": ("Auto-generated starter. mode: optimize (default) | force_visible | "
                     "force_hidden | interpreter. role (e.g. firstperson_hand, held_item) "
                     "splits that bone's subtree into its own bbmodel. Edit/remove freely."),
        "bones": bones,
    }


def generate_bbmodels(ysm_path, project_dir, model_name, files_section, annotations=None):
    """Generate .bbmodel files for main model, arm, and sub-entities.
    Returns a tuple of (all_dynamic_bones, all_bone_paths)."""
    player = files_section.get("player", {})
    ms = player.get("model", {})
    all_dynamic_bones = {}
    all_bone_paths = {}
    role_models = {}

    # Main model
    main_path = ysm_path / ms.get("main", "")
    main_data = read_json(main_path)
    if main_data:
        els, out, buuid, bchild, all_bones, bone_paths = convert_geometry_to_bb(main_data)
        all_bone_paths.update(bone_paths)
        # Merge arm model
        arm_path = ysm_path / ms.get("arm", "")
        arm_data = read_json(arm_path)
        if arm_data:
            aels, aout, abuuid, abchild, _, abone_paths = convert_geometry_to_bb(arm_data)
            # Arm bones are nested under the synthetic "ArmModel" group, so their
            # lookup paths must include that ancestor. Never overwrite a main-model
            # bone of the same name (RightArm/LeftArm/...): animations bind to the
            # main-model bone, and Figura resolves bone names by direct child only.
            for _abn, _abp in abone_paths.items():
                if _abn not in all_bone_paths:
                    all_bone_paths[_abn] = ("ArmModel",) + _abp
            for e in aels:
                if e["uuid"] not in {x["uuid"] for x in els}:
                    els.append(e)
            for bn, bu in abuuid.items():
                if bn not in buuid: buuid[bn] = bu
            arm_root = {"name": "ArmModel", "uuid": make_uuid(), "origin": [0,0,0],
                        "rotation": [0,0,0], "visibility": False, "export": True, "children": aout}
            out.append(arm_root)

        desc = main_data.get("minecraft:geometry", [{}])[0].get("description", {})
        tw, th = int(desc.get("texture_width", 64)), int(desc.get("texture_height", 64))

        # Animations
        all_anims = []
        base_hidden = set()
        for ak, ap in (player.get("animation", {}) or {}).items():
            ad = read_json(ysm_path / ap)
            if ad:
                base_hidden |= _base_hidden_bones(ad)
                anims, dyn = convert_animations_to_bb(ad, buuid, model_name)
                all_anims.extend(anims)
                all_dynamic_bones.update(dyn)
        _apply_hidden_bones(out, base_hidden)

        # Textures
        texs = build_texture_list(ysm_path, player, model_name)

        # ---- role-driven bbmodel splitting (compile-directed) ----
        # A bone tagged with a `role` is lifted out of the main model into its own
        # standalone .bbmodel (loaded as models.<stem> in Figura, independently
        # controllable). We carry along its elements, its subtree, the animation
        # channels that target it, and the shared textures.
        role_bones = {}
        for _bn, _cfg in ((annotations or {}).get("bones", {}) or {}).items():
            if isinstance(_cfg, dict) and _cfg.get("role"):
                role_bones[_bn] = _cfg["role"]
        for _bn, _role in role_bones.items():
            _grp, _plist = _find_group_and_parent(out, _bn)
            if not _grp:
                print(f"  [split] bone '{_bn}' (role={_role}) not found in outliner; skipped")
                continue
            # original parent bone in the main model: the split model follows its
            # world transform at runtime (logical re-parenting; None => model root).
            _pf, _parent_name = _find_parent_bone_name(out, _bn)
            _sub_el_uuids, _sub_bone_uuids = set(), set()
            _collect_subtree(_grp, _sub_el_uuids, _sub_bone_uuids)
            _sub_els = [e for e in els if e["uuid"] in _sub_el_uuids]
            els = [e for e in els if e["uuid"] not in _sub_el_uuids]
            try:
                _plist.remove(_grp)
            except ValueError:
                pass
            _sub_anims = []
            for _a in all_anims:
                _am = {u: k for u, k in (_a.get("animators", {}) or {}).items() if u in _sub_bone_uuids}
                if _am:
                    _na = dict(_a)
                    _na["animators"] = _am
                    _sub_anims.append(_na)
            _grp["visibility"] = True
            _stem = f"{model_name}_{_role}"
            _sbb = build_bbmodel(_sub_els, [_grp], _sub_anims, texs, tw, th, _stem)
            _sp = project_dir / f"{_stem}.bbmodel"
            with open(_sp, "w", encoding="utf-8") as f:
                json.dump(_sbb, f, indent=2, ensure_ascii=False)
            role_models[_bn] = {"stem": _stem, "role": _role, "parent": _parent_name}
            print(f"  {_sp.name}: {len(_sub_els)} elements split out (role={_role}, bone={_bn}, parent={_parent_name})")

        bb = build_bbmodel(els, out, all_anims, texs, tw, th, model_name)
        p = project_dir / f"{model_name}.bbmodel"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(bb, f, indent=2, ensure_ascii=False)
        print(f"  {p.name}: {len(els)} elements, {len(all_anims)} animations")

    # Sub-entities
    for stype in ("projectiles", "vehicles"):
        sub_list = files_section.get(stype)
        if sub_list is None: continue
        if isinstance(sub_list, dict):
            # Dict format: keys are match IDs (e.g. "minecraft:arrow"),
            # values are model configs. Preserve keys as match_id.
            _items = []
            for _key, _val in sub_list.items():
                if isinstance(_val, dict):
                    _val = dict(_val)  # shallow copy to avoid mutating source
                    if "match" not in _val:
                        _val["match"] = [_key]
                    _items.append(_val)
            sub_list = _items
        if not isinstance(sub_list, list): continue
        for se in sub_list:
            if not isinstance(se, dict): continue
            match_id = (se.get("match") or ["unknown"])[0]
            sp = ysm_path / se.get("model", "")
            sd = read_json(sp)
            if not sd: continue
            sels, sout, sbuuid, _, _, sbpath = convert_geometry_to_bb(sd)
            def _hide_tree(g):
                if isinstance(g, dict):
                    g["visibility"] = False
                    for c in (g.get("children", []) or []):
                        _hide_tree(c)
            for g in sout:
                _hide_tree(g)
            all_bone_paths.update(sbpath)
            sanims = []
            sap = se.get("animation")
            if sap:
                sad = read_json(ysm_path / sap)
                if sad:
                    sanims, s_dyn = convert_animations_to_bb(sad, sbuuid, match_id)
                    all_dynamic_bones.update(s_dyn)
            stex = []
            stp = se.get("texture")
            if stp: stex = build_texture_list(ysm_path, {"texture": stp}, f"{model_name}_{match_id}")
            sn = match_id.replace(":", "_")
            sbb = build_bbmodel(sels, sout, sanims, stex, tw, th, sn)
            spath = project_dir / f"{model_name}_{sn}.bbmodel"
            with open(spath, "w", encoding="utf-8") as f:
                json.dump(sbb, f, indent=2, ensure_ascii=False)
            print(f"  {spath.name}: {len(sels)} elements, {len(sanims)} animations ({stype})")

    return all_dynamic_bones, all_bone_paths, role_models
