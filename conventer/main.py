"""YSM → Figura avatar project generator.
Usage: python -m converter.main <ysm_folder> [output_dir]
"""

import sys, shutil, json
from pathlib import Path

from . import bbmodel_gen as bb
from . import lua_gen

def convert(ysm_path_str, output_dir_str, optimize=True, annotations=None, scaffold=False):
    ysm_path = Path(ysm_path_str)
    out_root = Path(output_dir_str)

    if not ysm_path.exists() or not ysm_path.is_dir():
        raise FileNotFoundError(f"YSM folder not found: {ysm_path}")

    ysm_json_file = ysm_path / "ysm.json"
    if not ysm_json_file.exists():
        raise RuntimeError("ysm.json not found")

    ysm_json = json.loads(ysm_json_file.read_bytes().decode('utf-8', errors='replace'))
    meta = ysm_json.get("metadata", {})
    files = ysm_json.get("files", {})
    model_name = meta.get("name", "ysm_model").replace(" ", "_")

    # Output project dir
    project_dir = out_root / model_name
    if project_dir.exists():
        shutil.rmtree(str(project_dir))
    project_dir.mkdir(parents=True)

    if scaffold:
        tmpl = bb.scaffold_annotations(ysm_path, files)
        tpath = ysm_path / "ysm_annotations.template.json"
        tpath.write_text(json.dumps(tmpl, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote annotation template: {tpath} ({len(tmpl.get('bones', {}))} base-hidden bones)")
        return tpath

    print(f"Generating Figura avatar project: {model_name}")
    print(f"  Source: {ysm_path}")
    print(f"  Mode: {'optimized' if optimize else 'interpreter (no optimization)'}")

    # Load player annotations (compile-directed) BEFORE geometry so role-tagged
    # bones can be split into their own bbmodels.
    ann = annotations
    if ann is None:
        _ann_path = ysm_path / "ysm_annotations.json"
        if _ann_path.exists():
            try:
                ann = json.loads(_ann_path.read_text(encoding="utf-8"))
                print(f"  Annotations: {_ann_path.name} ({len(ann.get('bones', {}))} bone tags)")
            except Exception as e:
                print(f"  [warn] failed to read {_ann_path.name}: {e}")
                ann = None

    # 1. Generate .bbmodel files
    print(f"\n  [.bbmodel]")
    dynamic_bones, bone_paths, role_models, projectile_models = bb.generate_bbmodels(ysm_path, project_dir, model_name, files, annotations=ann)

    # 2. Generate main.lua
    print(f"\n  [main.lua]")
    lua_gen.gen_lua(ysm_path, project_dir, model_name, ysm_json, {}, dynamic_bones, bone_paths, optimize=optimize, annotations=ann, role_models=role_models, projectile_models=projectile_models)

    # 3. Write avatar.json
    authors = [a.get("name","") for a in meta.get("authors",[])]
    avatar = {
        "name": meta.get("name","YSM Model"),
        "description": meta.get("tips",""),
        "authors": authors,
        "version": "1.0",
        "color": "#4a90d9",
        "autoScripts": ["main"],
    }
    (project_dir / "avatar.json").write_text(
        json.dumps(avatar, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"\n  avatar.json written")

    # 4. Copy textures
    src_tex = ysm_path / "textures"
    if src_tex.exists():
        dst_tex = project_dir / "textures"
        dst_tex.mkdir(exist_ok=True)
        for f in src_tex.iterdir():
            if f.suffix.lower() in ('.png','.jpg','.jpeg','.bmp','.webp'):
                shutil.copy2(str(f), str(dst_tex / f"{model_name}.{f.name}"))
        print(f"  textures/ copied")

    # 4b. Copy sounds. Figura registers each custom .ogg by its path relative to
    # the avatar root (separators -> "."). We copy sound files to the project
    # root so the registered name equals the bare effect name used by YSM
    # sound_effects keyframes (e.g. "click"), so ysm.play_sound('click') resolves.
    src_snd = ysm_path / "sounds"
    if src_snd.exists():
        n_snd = 0
        for f in src_snd.iterdir():
            if f.suffix.lower() == '.ogg':
                shutil.copy2(str(f), str(project_dir / f.name))
                n_snd += 1
        if n_snd:
            print(f"  sounds/ copied ({n_snd})")

    print(f"\nDone! Project at: {project_dir}")
    return project_dir

def main():
    # flags: --no-optimize / --interpreter / -O0 disable ALL optimizations and
    # emit a faithful interpreter-mode avatar (YSM base layers played as-is, the
    # native baked scale decides visibility per-frame just like the mod).
    optimize = True
    ann_override = None
    scaffold = False
    positional = []
    for a in sys.argv[1:]:
        if a in ("--no-optimize", "--interpreter", "--raw", "-O0"):
            optimize = False
        elif a in ("--optimize", "-O1"):
            optimize = True
        elif a == "--scaffold":
            scaffold = True
        elif a.startswith("--annotations="):
            try:
                ann_override = json.loads(Path(a.split("=", 1)[1]).read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[warn] could not read annotations file: {e}")
        else:
            positional.append(a)
    if len(positional) < 1:
        print("Usage: python -m converter.main <ysm_folder> [output_dir] [--no-optimize]")
        print("  or:   python converter/main.py <ysm_folder> [output_dir] [--no-optimize]")
        print("  --no-optimize / --interpreter : disable the visibility optimizer")
        sys.exit(1)
    ysm_path = positional[0]
    out_dir = positional[1] if len(positional) > 1 else str(Path(ysm_path).parent / "figura_projects")
    try:
        convert(ysm_path, out_dir, optimize=optimize, annotations=ann_override, scaffold=scaffold)
    except Exception as e:
        print(f"Error: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
