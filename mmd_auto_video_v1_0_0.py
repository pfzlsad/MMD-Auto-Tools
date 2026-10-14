bl_info = {
    "name": "MMD Auto Video V1.0 - Foot Contact Stage",
    "author": "OpenAI",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MMD Auto",
    "description": "Automates PMX/PMD + VMD + audio + optional image/video background to MP4 using MMD Tools.",
    "category": "Animation",
}

import bpy
import os
import json
import shutil
import struct
import subprocess
from pathlib import Path
import math
from math import radians
from mathutils import Vector
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, EnumProperty
from bpy.types import Operator, Panel, PropertyGroup

SUPPORTED_MODEL = {".pmx", ".pmd"}
SUPPORTED_MOTION = {".vmd"}
SUPPORTED_AUDIO = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}
SUPPORTED_BACKGROUND = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".mp4", ".mov", ".mkv", ".webm"}


def abs_path(p):
    return os.path.abspath(bpy.path.abspath(p)) if p else ""


def log(msg):
    print("[MMD Auto V1] " + msg)


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)


def validate_inputs(model, motion, audio, background=""):
    errors = []
    for p, exts, label in [
        (model, SUPPORTED_MODEL, "模型"),
        (motion, SUPPORTED_MOTION, "动作"),
        (audio, SUPPORTED_AUDIO, "音频"),
    ]:
        p = abs_path(p)
        if not p:
            errors.append(label + "未选择")
        elif not os.path.isfile(p):
            errors.append(label + "不存在: " + p)
        elif Path(p).suffix.lower() not in exts:
            errors.append(label + "格式不支持: " + p)
    if background:
        background = abs_path(background)
        if not os.path.isfile(background):
            errors.append("背景不存在: " + background)
        elif Path(background).suffix.lower() not in SUPPORTED_BACKGROUND:
            errors.append("背景格式不支持: " + background)
    return errors


def clear_scene():
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    # Remove unused datablocks created by previous test runs.
    for collection in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.cameras,
        bpy.data.lights,
        bpy.data.armatures,
    ):
        for block in list(collection):
            if block.users == 0:
                try:
                    collection.remove(block)
                except Exception:
                    pass


def get_mmd_ops():
    if not hasattr(bpy.ops, "mmd_tools"):
        raise RuntimeError(
            "未检测到 MMD Tools。请先安装与 Blender 版本匹配的 MMD Tools。"
        )
    return bpy.ops.mmd_tools


def call_operator_dynamic(op, filepath, extra=None):
    """Call an MMD Tools operator while adapting to minor API differences."""
    extra = extra or {}

    try:
        props = {p.identifier: p for p in op.get_rna_type().properties}
    except Exception:
        props = {}

    kwargs = {}
    if "filepath" in props:
        kwargs["filepath"] = filepath
    elif "file_path" in props:
        kwargs["file_path"] = filepath
    else:
        raise RuntimeError(
            f"{op.idname()} 没有 filepath/file_path 参数，当前 MMD Tools API 可能不兼容。"
        )

    for key, value in extra.items():
        if key in props:
            # Only pass enum values that are actually supported.
            prop = props[key]
            if getattr(prop, "type", None) == 'ENUM' and isinstance(value, set):
                supported = {item.identifier for item in prop.enum_items}
                value = {v for v in value if v in supported}
                if not value:
                    continue
            kwargs[key] = value

    log(f"Calling {op.idname()} with {kwargs}")
    result = op(**kwargs)

    if "FINISHED" not in result:
        raise RuntimeError(f"MMD Tools 操作未完成: {result}")

    return result


def import_model(path, scale):
    mmd = get_mmd_ops()
    op = getattr(mmd, "import_model", None)

    if op is None:
        raise RuntimeError(
            "找不到 bpy.ops.mmd_tools.import_model，请检查 MMD Tools 版本。"
        )

    before = set(bpy.data.objects.keys())
    call_operator_dynamic(
        op,
        path,
        {
            "scale": scale,
            "use_mip_map": True,
        },
    )

    imported = [
        bpy.data.objects[name]
        for name in bpy.data.objects.keys()
        if name not in before
    ]

    # Some MMD Tools versions reuse/rename objects. Fall back to scene objects.
    if not imported:
        imported = list(bpy.context.scene.objects)

    return imported


def select_objects(objects):
    bpy.ops.object.select_all(action='DESELECT')

    for obj in objects:
        try:
            obj.select_set(True)
        except Exception:
            pass

    armature = next((o for o in objects if o.type == 'ARMATURE'), None)
    if armature:
        bpy.context.view_layer.objects.active = armature
    elif objects:
        bpy.context.view_layer.objects.active = objects[0]


def _scene_cameras():
    return [o for o in bpy.context.scene.objects if o.type == 'CAMERA']


def _prepare_camera_import_target():
    """Create/select a Blender camera so MMD Tools has a target for VMD camera import.

    MMD Tools imports camera VMDs onto the currently selected Camera/MMD Camera.
    It may create an MMD_Camera empty parent and animate that parent rather than
    creating a brand-new Camera object by itself.
    """
    scene = bpy.context.scene
    cam = scene.camera
    if cam is None or cam.type != 'CAMERA':
        data = bpy.data.cameras.new("MMD_VMD_IMPORT_CAMERA_DATA")
        cam = bpy.data.objects.new("MMD_VMD_IMPORT_CAMERA", data)
        scene.collection.objects.link(cam)
        cam.location = (0.0, -5.0, 1.5)
        cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
        scene.camera = cam
    for obj in list(bpy.context.selected_objects):
        try:
            obj.select_set(False)
        except Exception:
            pass
    cam.select_set(True)
    bpy.context.view_layer.objects.active = cam
    return cam


def _find_camera_result(before_object_names, target_camera=None):
    """Find the actual render Camera after MMD Tools camera import.

    MMD Tools normally creates an Empty named MMD_Camera and parents the
    Blender Camera under it. The Empty carries the VMD animation. Therefore
    checking only CAMERA objects/FCurves is insufficient.
    """
    candidates = []
    before = set(before_object_names)
    for obj in bpy.context.scene.objects:
        if obj.name not in before and obj.type == 'CAMERA':
            candidates.append(obj)
    # Camera objects may already exist (the target camera) while a newly
    # created MMD_Camera empty carries the animation. Find new empties that
    # contain/parent a camera.
    for obj in bpy.context.scene.objects:
        if obj.name in before or obj.type != 'EMPTY':
            continue
        name = obj.name.casefold()
        children = [c for c in obj.children if c.type == 'CAMERA']
        if children and ('mmd_camera' in name or 'camera' in name):
            candidates.extend(children)
    # Prefer a target/new camera with an MMD camera parent.
    if target_camera is not None and target_camera.name in bpy.context.scene.objects:
        candidates.append(target_camera)
    # De-duplicate while preserving order.
    unique = []
    seen = set()
    for c in candidates:
        if c and c.name not in seen:
            unique.append(c)
            seen.add(c.name)
    return unique


def _new_or_animated_cameras(before_names):
    """Return cameras created/updated by an MMD Tools VMD import."""
    cams = []
    for obj in _scene_cameras():
        if obj.name not in before_names:
            cams.append(obj)
            continue
        ad = getattr(obj, "animation_data", None)
        action = getattr(ad, "action", None) if ad else None
        if action and len(action.fcurves) > 0:
            cams.append(obj)
    # Also make sure any camera object created by the importer is linked to the scene.
    for obj in list(bpy.data.objects):
        if obj.type == 'CAMERA' and obj.name not in before_names and obj.name not in {c.name for c in cams}:
            try:
                if obj.name not in bpy.context.scene.objects:
                    bpy.context.scene.collection.objects.link(obj)
            except Exception:
                pass
            cams.append(obj)
    return cams


def import_motion(path, scale, margin, include_camera=False):
    mmd = get_mmd_ops()

    op = getattr(mmd, "import_vmd", None)
    if op is None:
        op = getattr(mmd, "import_motion", None)

    if op is None:
        raise RuntimeError(
            "找不到 MMD Tools 的 VMD 导入算子（import_vmd/import_motion）。"
        )

    target_camera = _prepare_camera_import_target() if include_camera else None
    before = set(o.name for o in bpy.context.scene.objects)
    result = call_operator_dynamic(
        op,
        path,
        {
            "scale": scale,
            "margin": margin,
            "types": {"MOTION", "CAMERA"} if include_camera else {"MOTION"},
            "update_scene_settings": True,
        },
    )
    cams = _find_camera_result(before, target_camera) if include_camera else []
    if include_camera:
        log(f"VMD 导入后检测到 {len(cams)} 个镜头对象/目标")
    return result, cams


def import_camera_motion(path, scale, margin):
    """Import a separate camera VMD onto a real Blender camera target."""
    if not path:
        return None
    mmd = get_mmd_ops()
    op = getattr(mmd, "import_vmd", None) or getattr(mmd, "import_motion", None)
    if op is None:
        raise RuntimeError("找不到 MMD Tools 的 VMD 导入算子。")

    target_camera = _prepare_camera_import_target()
    before = set(o.name for o in bpy.context.scene.objects)
    result = call_operator_dynamic(
        op,
        path,
        {
            "scale": scale,
            "margin": margin,
            "types": {"CAMERA"},
            "update_scene_settings": True,
        },
    )
    cams = _find_camera_result(before, target_camera)

    if not cams:
        log("CAMERA-only 导入没有发现新相机目标，尝试全类型兼容模式")
        try:
            props = {p.identifier: p for p in op.get_rna_type().properties}
            prop = props.get("types")
            if prop and getattr(prop, "type", None) == 'ENUM':
                all_types = {item.identifier for item in prop.enum_items}
                if all_types:
                    before_retry = set(o.name for o in bpy.context.scene.objects)
                    target_camera = _prepare_camera_import_target()
                    call_operator_dynamic(
                        op,
                        path,
                        {
                            "scale": scale,
                            "margin": margin,
                            "types": all_types,
                            "update_scene_settings": True,
                        },
                    )
                    cams = _find_camera_result(before_retry, target_camera)
        except Exception as exc:
            log("镜头兼容模式导入异常: " + repr(exc))

    if not cams:
        raise RuntimeError(
            "已找到配套镜头 VMD，但 MMD Tools 导入后仍没有可用 Blender 相机。"
            "请确认当前 Blender/MMD Tools 版本支持 VMD Camera 导入。"
        )

    # The target camera is the actual render camera; MMD Tools may attach an
    # MMD_Camera empty above it and put the animation on that parent.
    chosen = target_camera if target_camera in cams else cams[-1]
    scene = bpy.context.scene
    scene.camera = chosen
    log(f"镜头导入成功：{chosen.name}（MMD Camera 动画已绑定）")
    return chosen


def find_armature():
    armatures = [
        obj for obj in bpy.context.scene.objects
        if obj.type == 'ARMATURE'
    ]

    if not armatures:
        return None

    for obj in armatures:
        name = obj.name.lower()
        if "armature" in name or "model" in name or "mmd" in name:
            return obj

    return armatures[0]


def get_sequence_collection(seq):
    """Blender 4.4+/newer uses seq.strips; older versions use seq.sequences."""
    if hasattr(seq, "strips"):
        return seq.strips
    if hasattr(seq, "sequences"):
        return seq.sequences
    raise RuntimeError(
        "当前 Blender 的 Sequence Editor API 不兼容：找不到 strips/sequences。"
    )


def add_audio_strip(path, frame_start=1, channel=1):
    scene = bpy.context.scene
    seq = scene.sequence_editor_create()
    collection = get_sequence_collection(seq)

    sound = bpy.data.sounds.load(path, check_existing=True)

    strip = collection.new_sound(
        name="MMD_AUDIO",
        filepath=path,
        channel=channel,
        frame_start=frame_start,
    )

    try:
        strip.sound = sound
    except Exception:
        pass

    return strip


def setup_scene_from_audio(audio_strip, fps):
    scene = bpy.context.scene

    scene.render.fps = fps
    scene.render.fps_base = 1.0

    # Blender's sound strip exposes final duration in scene frames.
    length = int(round(audio_strip.frame_final_duration))

    scene.frame_start = 1
    scene.frame_end = max(2, length)
    scene.frame_set(scene.frame_start)

    return length


def frame_range_from_animation():
    start = 1
    end = 1

    for obj in bpy.context.scene.objects:
        ad = getattr(obj, "animation_data", None)
        if not ad or not ad.action:
            continue

        for fcurve in ad.action.fcurves:
            for key in fcurve.keyframe_points:
                start = min(start, int(key.co.x))
                end = max(end, int(key.co.x))

    return start, end


def _model_world_bounds():
    """Return world-space bounding box of the imported model/armature."""
    arm = find_armature()
    objects = []
    if arm:
        objects = [arm] + [o for o in bpy.context.scene.objects if o.parent == arm]
    if not objects:
        objects = [o for o in bpy.context.scene.objects if o.type in {'MESH', 'ARMATURE'} and not o.name.startswith("MMD_AUTO_")]

    corners = []
    for obj in objects:
        if obj.type == 'MESH':
            try:
                corners.extend([obj.matrix_world @ Vector(c) for c in obj.bound_box])
            except Exception:
                pass
        elif obj.type == 'ARMATURE':
            # Armature bound_box is not always useful; use pose/bone heads/tails.
            for b in obj.data.bones:
                try:
                    corners.append(obj.matrix_world @ b.head_local)
                    corners.append(obj.matrix_world @ b.tail_local)
                except Exception:
                    pass

    if not corners:
        return Vector((-1, -1, 0)), Vector((1, 1, 2))

    min_v = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    max_v = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    return min_v, max_v


def setup_camera():
    """Create a portrait-friendly camera from the actual model bounds."""
    scene = bpy.context.scene

    for name in ("MMD_AUTO_CAMERA", "MMD_AUTO_CAMERA_TARGET"):
        old = bpy.data.objects.get(name)
        if old:
            bpy.data.objects.remove(old, do_unlink=True)

    min_v, max_v = _model_world_bounds()
    size = max_v - min_v
    height = max(size.z, 0.1)
    width = max(size.x, 0.1)
    depth = max(size.y, 0.1)
    center = (min_v + max_v) * 0.5

    # Full-body portrait composition: aim a little above the center.
    target_z = min_v.z + height * 0.54
    target = bpy.data.objects.new("MMD_AUTO_CAMERA_TARGET", None)
    scene.collection.objects.link(target)
    target.empty_display_type = 'PLAIN_AXES'
    target.empty_display_size = max(height * 0.025, 0.05)
    target.location = Vector((center.x, center.y, target_z))

    cam_data = bpy.data.cameras.new("MMD_AUTO_CAMERA")
    cam = bpy.data.objects.new("MMD_AUTO_CAMERA", cam_data)
    scene.collection.objects.link(cam)

    cam.data.lens = 50
    cam.data.sensor_width = 36
    cam.data.sensor_height = 24
    cam.data.sensor_fit = 'VERTICAL'
    cam.data.clip_start = max(0.01, height * 0.001)
    cam.data.clip_end = max(100.0, height * 100.0)

    import math
    aspect = max(scene.render.resolution_x, 1) / max(scene.render.resolution_y, 1)
    # With VERTICAL sensor fit, vertical FOV is determined by sensor_height.
    v_fov = 2.0 * math.atan(cam.data.sensor_height / (2.0 * cam.data.lens))
    h_fov = 2.0 * math.atan((cam.data.sensor_height * aspect) / (2.0 * cam.data.lens))

    # Leave enough margin for ears/hair and motion sway.
    dist_v = (height * 0.60) / max(math.tan(v_fov * 0.5), 0.05)
    radius_xy = max((width * width + depth * depth) ** 0.5 * 0.5, 0.1)
    dist_h = radius_xy / max(math.tan(h_fov * 0.5), 0.05)
    distance = max(dist_v, dist_h, height * 1.05) * 1.10

    cam.location = target.location + Vector((0.0, -distance, height * 0.015))

    constraint = cam.constraints.new(type='TRACK_TO')
    constraint.target = target
    constraint.track_axis = 'TRACK_NEGATIVE_Z'
    constraint.up_axis = 'UP_Y'

    scene.camera = cam

    log(
        "Portrait camera auto-fit: "
        f"bounds=({width:.3f}, {depth:.3f}, {height:.3f}), "
        f"resolution={scene.render.resolution_x}x{scene.render.resolution_y}, "
        f"distance={distance:.3f}, lens={cam.data.lens}"
    )
    return cam

def setup_lighting():
    scene = bpy.context.scene
    target = bpy.data.objects.get("MMD_AUTO_CAMERA_TARGET")

    lights = [
        ("MMD_AUTO_KEY", (4.0, -5.0, 9.0), 1200.0, 4.0),
        ("MMD_AUTO_FILL", (-5.0, -2.0, 6.0), 700.0, 5.0),
        ("MMD_AUTO_RIM", (0.0, 5.0, 8.0), 1000.0, 3.0),
    ]

    for name, location, energy, size in lights:
        data = bpy.data.lights.new(name, type='AREA')
        data.energy = energy
        data.shape = 'DISK'
        data.size = size

        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
        obj.location = location

        if target:
            constraint = obj.constraints.new(type='TRACK_TO')
            constraint.target = target
            constraint.track_axis = 'TRACK_NEGATIVE_Z'
            constraint.up_axis = 'UP_Y'



def estimate_foot_sole_z(scene, imported_objects, depsgraph, fallback_z):
    """Estimate the sole from evaluated mesh vertices weighted to foot/toe bones.

    Only inspect geometry near feet; ignore low-hanging hair, skirts and rig controls.
    Returns (z, method). Never alters the character or its animation.
    """
    foot_words = ('足首', 'つま先', '足先', 'ankle', 'toe', 'foot', 'shoe', '靴')
    exclude_words = ('ik', 'ＩＫ', 'Ｉｋ', 'ik親', 'leg', 'ひざ', '膝')
    bone_points = []
    foot_group_names = set()
    for obj in imported_objects:
        if obj.type != 'ARMATURE':
            continue
        try:
            rig = obj.evaluated_get(depsgraph)
            for bone in rig.pose.bones:
                name = bone.name.lower()
                if any(w.lower() in name for w in foot_words) and not any(w.lower() in name for w in exclude_words):
                    bone_points.append(rig.matrix_world @ bone.head)
                    foot_group_names.add(bone.name)
        except Exception as exc:
            log('脚部骨骼读取失败：%s' % exc)
    if not bone_points:
        return fallback_z, '网格包围盒回退（未识别脚骨）'

    # Use the lowest two foot-bone centers as candidates; bone controls below
    # the visible mesh are never themselves used as the sole height.
    bone_points.sort(key=lambda v: v.z)
    low = bone_points[:min(4, len(bone_points))]
    xs = [v.x for v in low]
    ys = [v.y for v in low]
    span = max(max(xs)-min(xs), max(ys)-min(ys), 0.1)
    search_radius = max(0.12, min(0.65, span * 0.28))
    weighted_z = []
    nearby_z = []
    for obj in imported_objects:
        if obj.type != 'MESH':
            continue
        try:
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            if mesh is None:
                continue
            try:
                group_ids = {g.index for g in obj.vertex_groups if g.name in foot_group_names}
                matrix = evaluated.matrix_world
                for v in mesh.vertices:
                    world = matrix @ v.co
                    if not any((world.x-b.x)**2 + (world.y-b.y)**2 <= search_radius**2 for b in low):
                        continue
                    if group_ids and any(g.group in group_ids and g.weight > 0.05 for g in v.groups):
                        weighted_z.append(world.z)
                    nearby_z.append(world.z)
            finally:
                evaluated.to_mesh_clear()
        except Exception as exc:
            log('脚底网格评估失败 (%s)：%s' % (obj.name, exc))
    if weighted_z:
        return min(weighted_z), '脚部骨骼权重网格'
    if nearby_z:
        # Fallback: local geometry near the foot bones, not the global mesh low point.
        return min(nearby_z), '脚部邻域网格'
    return fallback_z, '网格包围盒回退（脚部附近无顶点）'


def create_3d_stage(scene, settings, imported_objects):
    """Create a real mesh stage; rendered together with the character and its shadows."""
    scene.frame_set(scene.frame_start)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    coords = []
    # Use evaluated geometry: character is already posed at the first VMD frame.
    for obj in imported_objects:
        if obj.type != 'MESH':
            continue
        try:
            evaluated = obj.evaluated_get(depsgraph)
            corners = [evaluated.matrix_world @ Vector(c) for c in evaluated.bound_box]
            coords.extend(corners)
        except Exception:
            coords.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
    if not coords:
        raise RuntimeError('三维舞台模式需要导入至少一个人物网格。')
    x0, x1 = min(v.x for v in coords), max(v.x for v in coords)
    y0, y1 = min(v.y for v in coords), max(v.y for v in coords)
    z0 = min(v.z for v in coords)
    center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
    # A generous minimum area accommodates dance motion without touching the rig.
    radius = max(settings.stage_radius, 1.5 * max(x1 - x0, y1 - y0))
    sole_z, sole_method = estimate_foot_sole_z(scene, imported_objects, depsgraph, z0)
    top_z = sole_z + settings.stage_z_offset
    depth = settings.stage_thickness
    if settings.stage_shape == 'CIRCLE':
        bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=radius, depth=depth,
                                            location=(center_x, center_y, top_z - depth / 2))
    else:
        bpy.ops.mesh.primitive_cube_add(size=1, location=(center_x, center_y, top_z - depth / 2))
        bpy.context.object.dimensions = (radius * 2, radius * 2, depth)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    stage = bpy.context.object
    stage.name = 'MMD_AUTO_STAGE_PLATFORM'
    mat = bpy.data.materials.new('MMD_AUTO_STAGE_MATERIAL')
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    if bsdf:
        bsdf.inputs['Base Color'].default_value = (*settings.stage_color, 1.0)
        bsdf.inputs['Roughness'].default_value = settings.stage_roughness
        bsdf.inputs['Metallic'].default_value = settings.stage_metallic
    stage.data.materials.append(mat)
    # Stage is visual only: do not create a rigid body that might disturb MMD physics.
    bevel = stage.modifiers.new('Stage edge bevel', 'BEVEL')
    bevel.width = min(depth * 0.12, radius * 0.015)
    bevel.segments = 3
    stage.modifiers.new('Weighted normals', 'WEIGHTED_NORMAL')
    # Opaque world; lights cast shadows from character onto stage.
    scene.render.film_transparent = False
    world = scene.world or bpy.data.worlds.new('MMD_AUTO_STAGE_WORLD')
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get('Background')
    if bg:
        bg.inputs['Color'].default_value = (0.025, 0.028, 0.045, 1.0)
        bg.inputs['Strength'].default_value = 0.55
    log('三维舞台已创建：%s，半径 %.2f，台面 Z %.3f，脚底定位：%s' % (settings.stage_shape, radius, top_z, sole_method))
    return stage


def encode_stage_mp4(ffmpeg, frames_dir, start_frame, fps, audio, output):
    pattern = os.path.join(frames_dir, 'frame_%04d.png')
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'warning',
           '-framerate', str(fps), '-start_number', str(start_frame), '-i', pattern,
           '-i', audio, '-map', '0:v:0', '-map', '1:a:0',
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', output]
    _run_ffmpeg(cmd, '三维舞台视频编码')
    if not os.path.isfile(output) or os.path.getsize(output) < 1024:
        raise RuntimeError('三维舞台视频输出失败：' + output)


def setup_background(scene, background_path):
    """Add an optional image/video background using the World shader.

    World-space background is deliberately used instead of a 3D plane when a
    VMD camera is active.  A camera-parented plane can disappear when MMD Tools
    changes the imported camera transform/constraints.  The World background
    is always behind the character and therefore remains visible for every
    VMD camera keyframe.
    """
    if not background_path:
        log("No background selected; using normal world background.")
        return None

    path = abs_path(background_path)
    if not os.path.isfile(path):
        raise RuntimeError("背景文件不存在: " + path)

    cam = scene.camera
    if not cam:
        raise RuntimeError("创建背景前没有找到场景摄像机。")

    ext = Path(path).suffix.lower()
    try:
        image = bpy.data.images.load(path, check_existing=True)
    except Exception as exc:
        raise RuntimeError("无法加载背景文件: " + repr(exc))

    # Keep an old plane from previous plugin versions out of the render.
    old = bpy.data.objects.get("MMD_AUTO_BACKGROUND")
    if old:
        old.hide_render = True
        old.hide_viewport = True

    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("MMD_AUTO_WORLD")
        scene.world = world
    world.use_nodes = True

    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputWorld")
    bg = nodes.new("ShaderNodeBackground")
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.interpolation = 'Linear'
    tex.extension = 'REPEAT'

    # A World Texture is sampled in screen-independent world directions.  To
    # guarantee a full-frame 2D backdrop, use Generated-style coordinates from
    # the camera view through a Texture Coordinate node when available.
    texcoord = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    # Camera coordinates provide stable screen-relative placement even while
    # the imported MMD camera moves.
    links.new(texcoord.outputs.get("Generated"), mapping.inputs.get("Vector"))
    links.new(mapping.outputs.get("Vector"), tex.inputs.get("Vector"))
    links.new(tex.outputs.get("Color"), bg.inputs.get("Color"))
    bg.inputs["Strength"].default_value = 1.0
    links.new(bg.outputs.get("Background"), out.inputs.get("Surface"))

    # Movie textures must refresh on every rendered frame.
    if image.source == 'MOVIE':
        try:
            tex.image_user.use_auto_refresh = True
            tex.image_user.frame_start = scene.frame_start
            tex.image_user.frame_duration = max(1, image.frame_duration)
            tex.image_user.frame_offset = 0
        except Exception as exc:
            log("警告：设置背景视频逐帧刷新失败：" + repr(exc))

    # Use the world as the actual rendered background.  This avoids all camera
    # parenting/depth/clip-plane problems and works with moving VMD cameras.
    scene.render.film_transparent = False
    scene["mmd_auto_background"] = path
    scene["mmd_auto_background_mode"] = "WORLD_TEXTURE"
    log(f"背景已设置为 World 背景：{image.size[0]}x{image.size[1]}，图像源：{image.source}")
    log(f"Background added: {path} (" + ("video" if image.source == 'MOVIE' else "image") + ")")
    return None

def configure_render(scene, path, width, height, fps, transparent=False):
    # Blender 4.x uses BLENDER_EEVEE_NEXT; older Blender versions use
    # BLENDER_EEVEE. Detect the enum actually exposed by this installation.
    engine_items = {
        item.identifier
        for item in scene.bl_rna.properties["render"].fixed_type.properties["engine"].enum_items
    }
    if "BLENDER_EEVEE_NEXT" in engine_items:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    elif "BLENDER_EEVEE" in engine_items:
        scene.render.engine = "BLENDER_EEVEE"
    else:
        scene.render.engine = "BLENDER_WORKBENCH"

    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.render.fps_base = 1.0

    # Blender 5.x separates the output media type from the image file format.
    # Explicitly switch the render output to VIDEO so animation is actually
    # encoded as a movie instead of silently producing still-image frames.
    try:
        media_prop = scene.render.image_settings.bl_rna.properties.get("media_type")
        if media_prop and any(i.identifier == "VIDEO" for i in media_prop.enum_items):
            scene.render.image_settings.media_type = 'VIDEO'
    except Exception:
        pass

    # Older Blender versions use FFMPEG as the image/output format enum.
    try:
        fmt_prop = scene.render.image_settings.bl_rna.properties.get("file_format")
        if fmt_prop and any(i.identifier == "FFMPEG" for i in fmt_prop.enum_items):
            scene.render.image_settings.file_format = 'FFMPEG'
    except Exception:
        pass

    if transparent:
        # Transparent video: WebM + VP9 is supported by Blender 5.2 and
        # explicitly supports an alpha channel. MP4/H.264 cannot carry alpha.
        try:
            scene.render.ffmpeg.format = 'WEBM'
        except Exception as exc:
            raise RuntimeError("无法设置透明视频容器 WEBM: %r" % (exc,))
        try:
            scene.render.ffmpeg.codec = 'WEBM'
        except Exception as exc:
            raise RuntimeError("无法设置透明视频编码 WEBM/VP9: %r" % (exc,))
        try:
            scene.render.ffmpeg.audio_codec = 'NONE'
        except Exception:
            pass
        try:
            scene.render.ffmpeg.constant_rate_factor = 'MEDIUM'
        except Exception:
            pass
        try:
            scene.render.ffmpeg.use_max_b_frames = False
        except Exception:
            pass
        try:
            color_prop = scene.render.image_settings.bl_rna.properties.get('color_mode')
            if color_prop and any(i.identifier == 'RGBA' for i in color_prop.enum_items):
                scene.render.image_settings.color_mode = 'RGBA'
        except Exception as exc:
            log("警告：设置 RGBA 输出失败：" + repr(exc))
        scene.render.film_transparent = True
        scene["mmd_auto_transparent"] = True
        scene["mmd_auto_transparent_format"] = "WEBM/VP9"
    else:
        try:
            scene.render.ffmpeg.format = 'MPEG4'
        except Exception:
            pass
        try:
            scene.render.ffmpeg.codec = 'H264'
        except Exception:
            pass

        # These exist on current Blender 4.x/5.x FFmpeg settings.
        for attr, value in (
            ("constant_rate_factor", 'MEDIUM'),
            ("audio_codec", 'AAC'),
            ("audio_bitrate", 192),
        ):
            try:
                setattr(scene.render.ffmpeg, attr, value)
            except Exception:
                pass

    scene.render.filepath = path
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True


def render_preview_frame(scene, path, preview_frame):
    """Render a representative action frame, not necessarily frame 1."""
    old_filepath = scene.render.filepath
    old_use_seq = scene.render.use_sequencer
    old_frame = scene.frame_current
    old_format = scene.render.image_settings.file_format
    old_media_type = getattr(scene.render.image_settings, "media_type", None)
    try:
        scene.render.use_sequencer = False
        try:
            scene.render.image_settings.media_type = 'IMAGE'
        except Exception:
            pass
        scene.render.image_settings.file_format = 'PNG'
        scene.render.filepath = path
        # Preview index is relative to the *output* animation, not the
        # absolute Blender timeline.
        frame = min(scene.frame_start + max(1, int(preview_frame)) - 1, scene.frame_end)
        scene.frame_set(frame)
        log(f"Preview frame={frame}")
        bpy.ops.render.render(write_still=True)
    finally:
        try:
            if old_media_type is not None:
                scene.render.image_settings.media_type = old_media_type
        except Exception:
            pass
        scene.render.image_settings.file_format = old_format
        scene.frame_set(old_frame)
        scene.render.use_sequencer = old_use_seq
        scene.render.filepath = old_filepath


def render_rgba_png_sequence(scene, frames_dir, transparent=True):
    """Render every animation frame as a real RGBA PNG with film transparency."""
    ensure_dir(frames_dir)
    prefix = os.path.join(frames_dir, "frame_")
    old_use_sequencer = scene.render.use_sequencer
    old_filepath = scene.render.filepath
    old_media_type = getattr(scene.render.image_settings, "media_type", None)
    old_format = scene.render.image_settings.file_format
    old_color_mode = getattr(scene.render.image_settings, "color_mode", None)
    old_film_transparent = scene.render.film_transparent
    import time
    state = {"start": time.time(), "last_log": -1}
    wm = bpy.context.window_manager
    total = max(1, scene.frame_end - scene.frame_start + 1)

    def on_frame(scene_obj):
        try:
            done = min(total, max(0, scene_obj.frame_current - scene.frame_start + 1))
            frac = done / total
            try:
                wm.progress_update(frac)
            except Exception:
                pass
            if done == 1 or done == total or done - state["last_log"] >= 30:
                elapsed = time.time() - state["start"]
                remain = elapsed / max(done, 1) * (total - done)
                def fmt(sec):
                    sec = int(max(0, sec)); h, rem = divmod(sec, 3600); m, ss = divmod(rem, 60)
                    return f"{h:02d}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"
                log(f"RGBA PNG 渲染 {frac*100:6.2f}% | 帧 {done}/{total} | 已用 {fmt(elapsed)} | 预计剩余 {fmt(remain)}")
                state["last_log"] = done
        except Exception:
            pass

    handlers = bpy.app.handlers.frame_change_post
    handlers.append(on_frame)
    try:
        try: wm.progress_begin(0.0, 1.0)
        except Exception: pass
        scene.render.use_sequencer = False
        try:
            if old_media_type is not None:
                scene.render.image_settings.media_type = 'IMAGE'
        except Exception:
            pass
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGBA'
        try: scene.render.image_settings.color_depth = '8'
        except Exception: pass
        try: scene.render.image_settings.compression = 15
        except Exception: pass
        scene.render.film_transparent = transparent
        scene.render.filepath = prefix
        scene.render.use_file_extension = True
        scene.render.use_overwrite = True
        log(f"开始输出透明 PNG 序列：{frames_dir} | {total} 帧 | RGBA")
        bpy.ops.render.render(animation=True)
    finally:
        try:
            if on_frame in handlers: handlers.remove(on_frame)
        except Exception: pass
        try: wm.progress_end()
        except Exception: pass
        try:
            if old_media_type is not None: scene.render.image_settings.media_type = old_media_type
        except Exception: pass
        scene.render.image_settings.file_format = old_format
        if old_color_mode is not None:
            try: scene.render.image_settings.color_mode = old_color_mode
            except Exception: pass
        scene.render.use_sequencer = old_use_sequencer
        scene.render.filepath = old_filepath
        scene.render.film_transparent = old_film_transparent

    expected = [os.path.join(frames_dir, f"frame_{i:04d}.png") for i in range(scene.frame_start, scene.frame_end + 1)]
    missing = [x for x in expected if not os.path.isfile(x) or os.path.getsize(x) < 100]
    if missing:
        raise RuntimeError(f"透明 PNG 序列不完整，缺失 {len(missing)}/{len(expected)} 帧。首个缺失：{missing[0]}")
    log(f"RGBA PNG 序列完成：{len(expected)} 帧")
    return expected


def _run_ffmpeg(cmd, failure_label):
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace')
    except Exception as exc:
        raise RuntimeError(f"启动 FFmpeg 失败（{failure_label}）：{exc!r}")
    if result.returncode != 0:
        raise RuntimeError(f"{failure_label}失败：\n" + (result.stdout or "")[-4500:])
    return result.stdout or ""


def verify_movie_alpha(ffmpeg, movie_path, frame_indices):
    """Decode selected alpha planes and require both transparent and visible pixels."""
    import statistics
    reports = []
    for idx in sorted(set(max(0, int(i)) for i in frame_indices)):
        cmd = [ffmpeg, '-v', 'error', '-i', movie_path, '-vf', 'alphaextract,format=gray',
               '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1']
        # Select a frame by seeking through decoded frames, avoiding assumptions about timestamps.
        if idx:
            cmd = [ffmpeg, '-v', 'error', '-i', movie_path, '-vf',
                   f"select='eq(n\\,{idx})',alphaextract,format=gray", '-vsync', '0',
                   '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1']
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0 or not res.stdout:
            err = res.stderr.decode('utf-8', 'replace')[-1200:]
            raise RuntimeError("无法读取透明视频 Alpha 平面，编码结果不能确认透明：" + err)
        lo, hi = min(res.stdout), max(res.stdout)
        reports.append((idx, lo, hi))
    if not reports or not any(lo < 250 and hi > 0 for _, lo, hi in reports):
        raise RuntimeError("透明视频 Alpha 验证失败：未检测到同时存在透明像素和可见像素的画面。请检查 PNG 帧是否真正带 Alpha。")
    log("Alpha 验证通过（帧索引 / Alpha 最小值 / 最大值）：" + repr(reports))
    return reports


def encode_rgba_sequence_to_mov(ffmpeg, frames_dir, start_frame, fps, width, height, movie_path, frame_count):
    pattern = os.path.join(frames_dir, 'frame_%04d.png')
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'warning',
           '-framerate', str(fps), '-start_number', str(start_frame), '-i', pattern,
           '-frames:v', str(frame_count), '-an', '-c:v', 'prores_ks', '-profile:v', '4',
           '-pix_fmt', 'yuva444p10le', '-vendor', 'apl0', '-movflags', '+write_colr', movie_path]
    log("FFmpeg：将 RGBA PNG 序列编码为 MOV / ProRes 4444（保留 Alpha）")
    _run_ffmpeg(cmd, '透明 MOV 编码')
    if not os.path.isfile(movie_path) or os.path.getsize(movie_path) < 1024:
        raise RuntimeError('透明 MOV 未生成或文件大小异常：' + movie_path)
    verify_movie_alpha(ffmpeg, movie_path, [0, max(0, frame_count // 2), max(0, frame_count - 1)])


def composite_background_and_audio(ffmpeg, transparent_movie, background_path, audio_path,
                                   final_path, width, height, fps, frame_count):
    """Composite verified-alpha foreground with optional still/movie background and source audio."""
    bg = abs_path(background_path) if background_path else ''
    if not os.path.isfile(audio_path):
        raise RuntimeError('音频文件不存在：' + audio_path)
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'warning']
    if bg:
        ext = Path(bg).suffix.lower()
        if ext in {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}:
            cmd += ['-loop', '1', '-framerate', str(fps), '-i', bg]
        else:
            cmd += ['-stream_loop', '-1', '-i', bg]
        fg_index, audio_index = 1, 2
        cmd += ['-i', transparent_movie, '-i', audio_path]
        filt = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1[bg];"
                f"[1:v]format=rgba[fg];[bg][fg]overlay=shortest=1:format=auto[v]")
        cmd += ['-filter_complex', filt, '-map', '[v]', '-map', f'{audio_index}:a:0']
        log('FFmpeg：合成背景 + 已验证 Alpha 的人物视频 + 原始音频')
    else:
        fg_index, audio_index = 0, 1
        cmd += ['-i', transparent_movie, '-i', audio_path,
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy']
        log('未选择背景：保留透明 MOV，并将音频封装进 MOV')
    cmd += ['-r', str(fps), '-frames:v', str(frame_count), '-c:a', 'aac', '-b:a', '192k', '-shortest']
    if bg:
        cmd += ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', final_path]
    else:
        # QuickTime MOV supports ProRes video with AAC audio.
        cmd += ['-movflags', '+faststart', final_path]
    _run_ffmpeg(cmd, '背景/音频合成')
    if not os.path.isfile(final_path) or os.path.getsize(final_path) < 1024:
        raise RuntimeError('最终输出文件不存在或文件大小异常：' + final_path)


def assemble_audio_and_render(scene, silent_video, audio_path, final_path):
    """Mux the already-rendered silent MP4 with audio. Prefer external FFmpeg.
    This avoids rendering the whole animation a second time just to add sound.
    """
    if not os.path.isfile(silent_video) or os.path.getsize(silent_video) < 1024:
        raise RuntimeError("无声 MP4 没有正常生成，无法合成音频: " + silent_video)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        log("Using external FFmpeg for fast audio mux: " + ffmpeg)
        cmd = [
            ffmpeg, "-y",
            "-i", silent_video,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            final_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception as exc:
            log("External FFmpeg launch failed: " + repr(exc))
        else:
            if result.returncode == 0 and os.path.isfile(final_path) and os.path.getsize(final_path) >= 1024:
                log("Final MP4 created: " + final_path)
                return
            log("External FFmpeg failed; falling back to Blender VSE.\n" + (result.stderr[-2000:] if result.stderr else ""))

    log("FFmpeg executable not found/failed; falling back to Blender VSE export.")
    if scene.sequence_editor:
        scene.sequence_editor_clear()
    seq = scene.sequence_editor_create()
    collection = get_sequence_collection(seq)

    movie = collection.new_movie(
        name="MMD_RENDER",
        filepath=silent_video,
        channel=1,
        frame_start=1,
        fit_method='FIT',
    )
    sound = bpy.data.sounds.load(audio_path, check_existing=True)
    audio = collection.new_sound(
        name="MMD_AUDIO",
        filepath=audio_path,
        channel=2,
        frame_start=1,
    )
    try:
        audio.sound = sound
    except Exception:
        pass

    scene.frame_start = 1
    scene.frame_end = min(
        max(2, int(movie.frame_final_duration)),
        max(2, int(audio.frame_final_duration)),
    )
    scene.render.use_sequencer = True
    scene.render.filepath = final_path
    bpy.ops.render.render(animation=True)

    if not os.path.isfile(final_path):
        raise RuntimeError("Blender VSE 合成完成后仍未找到最终 MP4: " + final_path)

def write_manifest(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def read_vmd_channels(path):
    """Read the VMD channel counts needed to distinguish motion/camera files."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 58:
            return {"bone": 0, "morph": 0, "camera": 0, "light": 0}
        off = 50  # 30-byte header + 20-byte model name
        bone = struct.unpack_from("<I", data, off)[0]
        off += 4 + bone * 111
        if off + 4 > len(data):
            return {"bone": bone, "morph": 0, "camera": 0, "light": 0}
        morph = struct.unpack_from("<I", data, off)[0]
        off += 4 + morph * 23
        if off + 4 > len(data):
            return {"bone": bone, "morph": morph, "camera": 0, "light": 0}
        camera = struct.unpack_from("<I", data, off)[0]
        off += 4 + camera * 61
        light = struct.unpack_from("<I", data, off)[0] if off + 4 <= len(data) else 0
        return {"bone": bone, "morph": morph, "camera": camera, "light": light}
    except Exception:
        return {"bone": 0, "morph": 0, "camera": 0, "light": 0}



def read_vmd_motion_last_frame(path):
    """Read last bone/morph VMD frame (zero-based). Do not count camera frames.

    VMD motion starts at frame 0, corresponding to Blender scene frame 1
    with import margin=0. Returns None when the file cannot be parsed.
    """
    try:
        with open(path, "rb") as stream:
            header = stream.read(54)
            if len(header) < 54 or not header.startswith(b"Vocaloid Motion Data"):
                return None
            bone_count = struct.unpack_from("<I", header, 50)[0]
            if bone_count > 10_000_000:
                return None
            last_frame = None
            for _ in range(bone_count):
                record = stream.read(111)
                if len(record) != 111:
                    return None
                frame = struct.unpack_from("<I", record, 15)[0]
                last_frame = frame if last_frame is None else max(last_frame, frame)
            raw = stream.read(4)
            if len(raw) != 4:
                return last_frame
            morph_count = struct.unpack("<I", raw)[0]
            if morph_count > 10_000_000:
                return None
            for _ in range(morph_count):
                record = stream.read(23)
                if len(record) != 23:
                    return None
                frame = struct.unpack_from("<I", record, 15)[0]
                last_frame = frame if last_frame is None else max(last_frame, frame)
            return last_frame
    except (OSError, ValueError, struct.error):
        return None


def set_timeline_to_motion(scene, motion_path, audio_frames, armature):
    """Use VMD motion length for baking/render, not audio or camera duration.

    This prevents trailing still frames being mistaken for warmup frames.
    No animation keyframes or physical simulation settings are modified.
    """
    last_vmd_frame = read_vmd_motion_last_frame(motion_path)
    if last_vmd_frame is not None:
        # VMD frame 0 -> Blender frame 1, so VMD last frame 687 -> 688 frames.
        motion_end = max(2, int(last_vmd_frame) + 1)
        source = "VMD骨骼/表情末帧"
    else:
        # Fallback: only the model's own action, never camera or audio.
        action = getattr(getattr(armature, 'animation_data', None), 'action', None) if armature else None
        if action is None:
            raise RuntimeError("无法读取VMD动作帧数，也找不到模型骨骼动作，无法安全确定渲染范围")
        motion_end = max(2, int(math.ceil(float(action.frame_range[1]))))
        source = "骨骼动作帧范围（VMD解析失败）"
    scene.frame_start = 1
    scene.frame_end = motion_end
    scene.frame_set(1)
    log("正式动画范围：1-%d（%s）；音频长度 %d 帧，仅用于配音，不延长物理烘焙" %
        (motion_end, source, audio_frames))
    return motion_end


def find_separate_camera_vmd(motion_path, camera_dir=""):
    """Find a camera-only VMD from an explicit camera folder.

    Batch pairing rule:
    1) exact same stem: 动作01.vmd <-> 动作01.vmd
    2) convenient suffix: 动作01.vmd <-> 动作01_camera.vmd / 动作01-camera.vmd
    The camera file must contain camera keyframes and no character bone keyframes.
    If no matching camera exists, return empty string so the caller uses the default fixed camera.
    """
    if not camera_dir:
        return ""
    camera_dir = abs_path(camera_dir)
    if not os.path.isdir(camera_dir):
        return ""
    stem = Path(abs_path(motion_path)).stem.casefold()
    candidates = []
    for name in os.listdir(camera_dir):
        p = os.path.join(camera_dir, name)
        if not os.path.isfile(p) or Path(name).suffix.lower() not in SUPPORTED_MOTION:
            continue
        info = read_vmd_channels(p)
        if info["camera"] <= 0 or info["bone"] > 0:
            continue
        cstem = Path(name).stem.casefold()
        if cstem == stem:
            candidates.append((0, p, info))
        elif cstem in {f"{stem}_camera", f"{stem}-camera", f"{stem} camera"}:
            candidates.append((1, p, info))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (x[0], Path(x[1]).name.casefold()))
    best_rank = candidates[0][0]
    best = [x for x in candidates if x[0] == best_rank]
    if len(best) > 1:
        raise RuntimeError(
            "镜头文件夹中发现多个与动作匹配的镜头 VMD，请每个动作只保留一个同名镜头文件："
            + ", ".join(Path(x[1]).name for x in best)
        )
    return best[0][1]



def find_paired_audio(motion_path, audio_dir=""):
    """Find an audio file whose stem exactly matches the motion filename stem."""
    motion_path = abs_path(motion_path)
    stem = Path(motion_path).stem.casefold()
    search_dirs = []
    if audio_dir:
        search_dirs.append(abs_path(audio_dir))
    search_dirs.append(os.path.dirname(motion_path))

    seen = set()
    for d in search_dirs:
        if not d or d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            if Path(name).suffix.lower() not in SUPPORTED_AUDIO:
                continue
            if Path(name).stem.casefold() == stem:
                return p
    return ""


def collect_batch_jobs(motion_dir, audio_dir=""):
    motion_dir = abs_path(motion_dir)
    if not motion_dir or not os.path.isdir(motion_dir):
        raise RuntimeError("批量动作文件夹不存在: " + str(motion_dir))

    jobs = []
    for name in sorted(os.listdir(motion_dir), key=str.casefold):
        p = os.path.join(motion_dir, name)
        if not os.path.isfile(p) or Path(name).suffix.lower() not in SUPPORTED_MOTION:
            continue
        info = read_vmd_channels(p)
        # Batch jobs are character-motion VMDs. Camera-only VMDs are paired
        # automatically with the character motion instead of becoming jobs.
        if info["bone"] <= 0:
            continue
        audio = find_paired_audio(p, audio_dir)
        jobs.append((p, audio))
    return jobs


def find_ffmpeg():
    """Locate FFmpeg for transparent WebM audio muxing.

    Search order:
    1) FFmpeg on PATH
    2) common Windows locations, including the user's configured C:\\ffmpeg tree
    3) FFMPEG_EXE environment variable
    """
    candidates = []
    env_path = os.environ.get("FFMPEG_EXE", "").strip().strip('"')
    if env_path:
        candidates.append(env_path)

    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)

    candidates.extend([
        r"C:\\ffmpeg\\ffmpeg-master-latest-win64-gpl-shared\\bin\\ffmpeg.exe",
        r"C:\\ffmpeg\\bin\\ffmpeg.exe",
        r"C:\\ffmpeg\\ffmpeg-master-latest-win64-gpl\\bin\\ffmpeg.exe",
    ])

    # Also search one level below C:\\ffmpeg for ffmpeg.exe.
    root = r"C:\\ffmpeg"
    if os.path.isdir(root):
        try:
            for name in os.listdir(root):
                p = os.path.join(root, name, "bin", "ffmpeg.exe")
                if os.path.isfile(p):
                    candidates.append(p)
        except Exception:
            pass

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(os.path.expandvars(candidate))
        key = os.path.normcase(candidate)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate):
            return candidate
    return ""


def mux_transparent_audio(video_path, audio_path, output_path):
    """Mux audio into an existing VP9/WebM Alpha video without re-encoding VP9."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg，无法给透明 WebM 合并音频。请确认 FFmpeg 路径已配置。")
    if not os.path.isfile(video_path) or os.path.getsize(video_path) < 1024:
        raise RuntimeError("透明 WebM 没有正常生成，无法合并音频：" + video_path)
    if not os.path.isfile(audio_path):
        raise RuntimeError("音频文件不存在：" + audio_path)

    temp_path = output_path + ".audio_tmp.webm"
    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass

    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "libopus",
        "-b:a", "192k",
        "-shortest",
        temp_path,
    ]
    log("FFmpeg: mux Alpha WebM + audio")
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError("启动 FFmpeg 合并透明视频音频失败：%r" % (exc,))
    if result.returncode != 0 or not os.path.isfile(temp_path) or os.path.getsize(temp_path) < 1024:
        tail = (result.stdout or "")[-2500:]
        raise RuntimeError("透明 WebM 音频合并失败：%s" % tail)

    try:
        os.replace(temp_path, output_path)
    except Exception as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise RuntimeError("替换透明 WebM 文件失败：%r" % (exc,))


def activate_mmd_physics(scene, imported_objects=None):
    """Build the MMD Tools physics rig before Blender point-cache baking.

    The MMD Tools 'Build' button is separate from bpy.ops.ptcache.bake_all.
    Never silently report success if this step fails.
    """
    objects = list(imported_objects) if imported_objects is not None else list(scene.objects)
    roots = [obj for obj in objects if getattr(obj, "mmd_type", None) == 'ROOT']
    if not roots:
        raise RuntimeError("未找到 MMD 根对象，无法自动启用 MMD 物理。请确认 PMX 由 MMD Tools 导入。")
    build_op = getattr(getattr(bpy.ops, "mmd_tools", None), "build_rig", None)
    if build_op is None:
        raise RuntimeError("当前 MMD Tools 不提供 build_rig 操作，无法自动启用物理。")
    built = 0
    existing = 0
    old_active = bpy.context.view_layer.objects.active
    old_selection = list(bpy.context.selected_objects)
    try:
        for root in roots:
            root_props = getattr(root, "mmd_root", None)
            if root_props is not None and bool(getattr(root_props, "is_built", False)):
                log("MMD 物理已构建，跳过重复构建：" + root.name)
                existing += 1
                continue
            bpy.ops.object.select_all(action='DESELECT')
            root.select_set(True)
            bpy.context.view_layer.objects.active = root
            try:
                if hasattr(build_op, 'poll') and not build_op.poll():
                    raise RuntimeError("MMD Tools build_rig.poll() 未通过")
                result = build_op()
                if 'FINISHED' not in result:
                    raise RuntimeError("build_rig 返回 " + str(result))
            except Exception as exc:
                raise RuntimeError("无法为模型 %s 启用 MMD 物理：%s" % (root.name, exc)) from exc
            built += 1
            log("MMD 物理已启用：" + root.name)
    finally:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in old_selection:
            try:
                obj.select_set(True)
            except ReferenceError:
                pass
        if old_active is not None and old_active.name in bpy.context.view_layer.objects:
            bpy.context.view_layer.objects.active = old_active
    rigid_count = sum(obj.rigid_body is not None for obj in scene.objects)
    if rigid_count == 0:
        raise RuntimeError("已执行 MMD 物理构建，但场景没有刚体。请检查模型是否包含 MMD 刚体。")
    if scene.rigidbody_world is None:
        raise RuntimeError("MMD 物理构建后仍没有刚体世界，无法进行物理烘焙。")
    return {"roots": len(roots), "built": built, "already_built": existing, "rigid_bodies": rigid_count}


def _action_fcurves(action):
    """Support legacy actions and Blender 4.4+/5 layered actions."""
    found = []
    seen = set()
    def add_curves(curves):
        for fc in curves:
            if fc.as_pointer() not in seen:
                found.append(fc)
                seen.add(fc.as_pointer())
    try:
        add_curves(action.fcurves)
    except (AttributeError, RuntimeError):
        pass
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for slot in action.slots:
                    try:
                        bag = strip.channelbag(slot, ensure=False)
                        if bag:
                            add_curves(bag.fcurves)
                    except (AttributeError, RuntimeError, TypeError):
                        pass
    except (AttributeError, RuntimeError):
        pass
    return found


def fix_vmd_initial_pose(scene, armature=None):
    """Prevent a rest/T-pose at frame 1 when imported VMD keys start later.

    For every animated pose-bone channel, copy the first imported value to
    frame 1 if that channel has no earlier key. This does not remove or shift
    existing VMD keys and therefore preserves the original motion timing.
    """
    if armature is None:
        armature = find_armature()
    if armature is None or armature.type != 'ARMATURE':
        raise RuntimeError("无法修复 VMD 初始姿势：未找到角色骨架")
    ad = armature.animation_data
    action = getattr(ad, 'action', None) if ad else None
    if action is None:
        raise RuntimeError("无法修复 VMD 初始姿势：角色没有活动动作")
    fixed = 0
    inspected = 0
    for fc in _action_fcurves(action):
        if 'pose.bones[' not in fc.data_path:
            continue
        keys = fc.keyframe_points
        if not keys:
            continue
        inspected += 1
        first = min(keys, key=lambda key: float(key.co.x))
        first_frame = float(first.co.x)
        if first_frame <= 1.0001:
            continue
        first_value = float(first.co.y)
        # Do not evaluate at frame 1: some imports extrapolate a rest pose.
        anchor = keys.insert(1.0, first_value, options={'FAST'})
        anchor.interpolation = 'CONSTANT'
        fc.update()
        fixed += 1
    if inspected == 0:
        raise RuntimeError("无法修复 VMD 初始姿势：动作中没有骨骼动画曲线")
    scene.frame_set(1)
    log("VMD 起始姿势同步：检查 %d 条骨骼曲线，补齐 %d 条第 1 帧初始值" %
        (inspected, fixed))
    return {"bone_curves": inspected, "anchored_curves": fixed,
            "status": "fixed" if fixed else "already_aligned"}


def physics_cache_summary(scene):
    """Inspect Blender physics cache providers in the current scene."""
    rigid_world = scene.rigidbody_world
    rigid_cache = rigid_world.point_cache if rigid_world else None
    caches = []
    if rigid_cache is not None:
        caches.append(("Rigid Body World", rigid_cache))
    for obj in scene.objects:
        for modifier in obj.modifiers:
            if modifier.type in {'CLOTH', 'SOFT_BODY'}:
                point_cache = getattr(modifier, "point_cache", None)
                if point_cache is not None:
                    caches.append((obj.name + "/" + modifier.name, point_cache))
        for psys in obj.particle_systems:
            if psys.point_cache is not None:
                caches.append((obj.name + "/" + psys.name, psys.point_cache))
    return caches


def bake_scene_physics(scene, settings):
    """Bake physics before render; raise if Blender reports a failure.

    MMD Tools physics uses Blender rigid bodies, which are baked via
    bpy.ops.ptcache.bake_all. The same call also bakes supported point caches.
    """
    caches = physics_cache_summary(scene)
    if not caches:
        log("物理烘焙：场景中未找到刚体世界、布料或软体缓存，跳过。")
        return {"status": "no_cache", "cache_count": 0, "baked_count": 0}

    start = int(scene.frame_start)
    end = int(scene.frame_end)
    if end <= start:
        raise RuntimeError("物理烘焙失败：结束帧必须大于开始帧。")

    # Clear previous baked data: otherwise new VMD motions can reuse stale physics.
    if any(bool(getattr(cache, 'is_baked', False)) for _, cache in caches):
        try:
            with bpy.context.temp_override(scene=scene):
                result = bpy.ops.ptcache.free_bake_all()
            if 'FINISHED' not in result:
                raise RuntimeError(str(result))
            log("已清除旧物理烘焙缓存")
        except Exception as exc:
            raise RuntimeError("清除旧物理缓存失败：%s" % exc) from exc

    # Blender's rigid body cache must cover the whole animation, including
    # the VMD margin / any lead-in frames before the rendered first frame.
    for label, cache in caches:
        try:
            if cache.is_baked:
                log("物理缓存已烘焙，保留：" + label)
                continue
            cache.frame_start = min(start, int(cache.frame_start))
            cache.frame_end = end
        except (AttributeError, TypeError, ValueError) as exc:
            log("物理缓存范围设置跳过 %s: %r" % (label, exc))

    log("开始物理烘焙：%s-%s，共 %s 组缓存" % (start, end, len(caches)))
    original_frame = scene.frame_current
    output_start = int(scene.frame_start)
    scene.frame_set(output_start)
    try:
        # bake_all is context-sensitive: pass the correct scene explicitly.
        with bpy.context.temp_override(scene=scene):
            result = bpy.ops.ptcache.bake_all(bake=True)
        if 'FINISHED' not in result:
            raise RuntimeError("Blender 没有完成物理烘焙：" + str(result))
    except Exception as exc:
        raise RuntimeError("物理烘焙失败，请检查刚体世界、磁盘空间及 Blender 控制台：%s" % exc) from exc
    finally:
        scene.frame_start = output_start
        scene.frame_set(max(output_start, original_frame))

    baked_count = sum(bool(getattr(cache, 'is_baked', False)) for _, cache in caches)
    if baked_count != len(caches):
        missing = [name for name, cache in caches if not getattr(cache, 'is_baked', False)]
        raise RuntimeError("物理烘焙未完成，未烘焙缓存：" + ", ".join(missing[:6]))
    log("物理烘焙完成：%s/%s" % (baked_count, len(caches)))
    return {"status": "baked", "cache_count": len(caches), "baked_count": baked_count}


def build_pipeline(settings, motion_override="", audio_override="", camera_override="", batch_index=0, batch_total=0):
    scene = bpy.context.scene
    model = abs_path(settings.model_path)
    motion = abs_path(motion_override) if motion_override else abs_path(settings.motion_path)
    audio = abs_path(audio_override) if audio_override else abs_path(settings.audio_path)
    background = abs_path(settings.background_path) if settings.output_mode == 'COMPOSITE' else ""

    errors = validate_inputs(model, motion, audio, background)
    if errors:
        raise RuntimeError("\n".join(errors))

    model_dir = os.path.dirname(model)
    output_dir = abs_path(settings.output_dir)

    if not output_dir:
        output_dir = os.path.join(model_dir, "output")

    ensure_dir(output_dir)

    stem = Path(motion).stem if motion_override else Path(model).stem
    if batch_index and batch_total:
        log(f"===== 批量任务 {batch_index}/{batch_total}: {stem} =====")

    frames_dir = os.path.join(output_dir, stem + "_MMD_Auto_RGBA_Frames")
    transparent_path = os.path.join(output_dir, stem + "_MMD_Auto_Transparent.mov")
    final_path = os.path.join(output_dir, stem + "_MMD_Auto.mp4")
    blend_path = os.path.join(output_dir, stem + "_MMD_Auto.blend")
    manifest_path = os.path.join(output_dir, stem + "_MMD_Auto.json")

    if settings.clear_scene:
        clear_scene()

    log("1/7 Import model: " + model)
    imported = import_model(model, settings.model_scale)
    select_objects(imported)

    motion_info = read_vmd_channels(motion)
    embedded_camera = motion_info["camera"] > 0
    separate_camera = abs_path(camera_override) if camera_override else find_separate_camera_vmd(motion, settings.batch_camera_dir if batch_index and batch_total else "")

    log("2/7 Import motion: " + motion)
    if embedded_camera:
        log(f"检测到动作 VMD 内含镜头数据：{motion_info['camera']} 个镜头关键帧")
    # MMD Tools' VMD margin inserts a rest-pose lead-in before the first VMD key.
    # Force zero for automated renders: otherwise the first output frames can
    # interpolate from a T-pose and violently disturb the rigid-body simulation.
    if settings.motion_margin:
        log("注意：自动视频模式将 VMD Margin 从 %d 覆盖为 0，防止默认姿势进入视频。" % settings.motion_margin)
    _, embedded_cameras = import_motion(motion, settings.motion_scale, 0, include_camera=embedded_camera)
    imported_camera = embedded_cameras[-1] if embedded_cameras else None

    if separate_camera:
        camera_info = read_vmd_channels(separate_camera)
        log("2/7b Import separate camera: " + separate_camera)
        log(f"检测到独立镜头 VMD：{camera_info['camera']} 个镜头关键帧")
        imported_camera = import_camera_motion(separate_camera, settings.motion_scale, 0)

    armature = find_armature()
    has_bone_animation = False
    if armature and armature.animation_data and armature.animation_data.action:
        has_bone_animation = any(
            "pose.bones[" in fc.data_path
            for fc in armature.animation_data.action.fcurves
        )

    if not has_bone_animation:
        log(
            "WARNING: 当前 VMD 没有检测到角色骨骼动画。"
            "请确认选择的是人物动作 VMD，而不是 Camera VMD / 镜头文件。"
        )

    log("3/7 Add audio and determine timeline: " + audio)
    audio_strip = add_audio_strip(audio)
    audio_frames = setup_scene_from_audio(audio_strip, settings.fps)

    # IMPORTANT: audio may be longer than the VMD motion (e.g. 752 vs 688).
    # Do not bake/render silent tail frames, and never shift motion keys.
    # Import margin stays 0 and no warmup frames are generated.
    set_timeline_to_motion(scene, motion, audio_frames, armature)

    # Bake after importing model + VMD and finalizing the timeline, before
    # preview and PNG sequence rendering. Batch tasks each bake their own scene.
    physics_result = {"status": "disabled", "cache_count": 0, "baked_count": 0}
    # With import margin=0, the first imported pose should already be the
    # actual VMD start pose. Fill only channels that begin later, without
    # moving original VMD keyframes or inventing a rest-pose transition.
    initial_pose_result = fix_vmd_initial_pose(scene, armature) if has_bone_animation else {"status": "no_bone_animation"}
    if has_bone_animation:
        scene.frame_set(1)
        log("VMD 正式起始帧检查：导入 margin=0；已在第 1 帧评估骨骼姿势")
    if settings.auto_bake_physics:
        activation_result = activate_mmd_physics(scene, imported)
        physics_result = bake_scene_physics(scene, settings)
        physics_result["mmd_activation"] = activation_result
    physics_result["initial_pose"] = initial_pose_result

    log("4/7 Create camera")
    if imported_camera is not None:
        scene.camera = imported_camera
        log("使用 VMD 镜头数据；镜头导入已验证成功。")
    elif embedded_camera or separate_camera:
        # A camera file was explicitly requested or embedded camera data was
        # detected. Never silently fall back to the default camera if import failed.
        raise RuntimeError(
            "检测到镜头数据，但没有成功建立 Blender 相机；已停止，避免误用默认固定相机。"
        )
    else:
        setup_camera()
        log("未检测到镜头数据，使用默认固定相机（不自动生成镜头移动）。")

    log("5/8 Create lighting")
    setup_lighting()
    stage_object = create_3d_stage(scene, settings, imported) if settings.output_mode == 'STAGE' else None

    # First establish a clean transparent foreground render.
    # The selected background is deliberately NOT rendered in this stage.
    # It will be composited later from the transparent WebM output.
    log("6/8 Transparent mode: skip World background for alpha test")

    scene = bpy.context.scene
    configure_render(
        scene,
        transparent_path,
        settings.width,
        settings.height,
        settings.fps,
        transparent=(settings.output_mode != 'STAGE'),
    )

    scene["mmd_auto_version"] = "1.0.0"
    scene["mmd_auto_model"] = model
    scene["mmd_auto_motion"] = motion
    scene["mmd_auto_audio"] = audio

    # Remove the initial audio strip before the first render.
    if scene.sequence_editor:
        scene.sequence_editor_clear()

    if settings.preview_first:
        preview_path = os.path.join(output_dir, stem + "_MMD_Auto_preview.png")
        log("7/8 Render camera preview: " + preview_path)
        render_preview_frame(scene, preview_path, settings.preview_frame)
        scene["mmd_auto_preview"] = preview_path

    log("8/8 输出 PNG 帧序列并编码视频")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg。请确认已安装 FFmpeg，或设置 FFMPEG_EXE 环境变量。")
    is_stage = settings.output_mode == 'STAGE'
    frame_paths = render_rgba_png_sequence(scene, frames_dir, transparent=not is_stage)
    frame_count = scene.frame_end - scene.frame_start + 1
    if is_stage:
        final_output = final_path
        encode_stage_mp4(ffmpeg, frames_dir, scene.frame_start, settings.fps, audio, final_output)
    else:
        encode_rgba_sequence_to_mov(ffmpeg, frames_dir, scene.frame_start, settings.fps,
                                    settings.width, settings.height, transparent_path, frame_count)
        final_output = final_path if background else os.path.join(output_dir, stem + "_MMD_Auto_Transparent_Audio.mov")
        composite_background_and_audio(ffmpeg, transparent_path, background, audio,
                                       final_output, settings.width, settings.height,
                                       settings.fps, frame_count)

    manifest = {
        "version": "1.0.0",
        "output_mode": settings.output_mode,
        "stage_object": stage_object.name if stage_object else None,
        "embedded_camera_keyframes": motion_info["camera"],
        "separate_camera": separate_camera,
        "camera_folder": settings.batch_camera_dir if batch_index and batch_total else "",
        "blender_version": bpy.app.version_string,
        "model": model,
        "motion": motion,
        "audio": audio,
        "background": background,
        "rgba_frames_dir": frames_dir,
        "transparent_video": transparent_path if not is_stage else None,
        "output": final_output,
        "blend": blend_path,
        "fps": settings.fps,
        "resolution": [settings.width, settings.height],
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "has_character_bone_animation": has_bone_animation,
        "physics_bake": physics_result,
        "notes": [
            "Uses MMD Tools for PMX/PMD and VMD import.",
            "If has_character_bone_animation is false, the selected VMD may be a camera-only motion file.",
            "Renders every frame as RGBA PNG, encodes MOV/ProRes 4444, verifies decoded Alpha planes, then composites optional background and source audio with FFmpeg.",
            "Optional Blender physics cache baking runs after VMD import and before rendering.",
        ],
    }

    write_manifest(manifest_path, manifest)

    scene.render.filepath = final_output
    scene["mmd_auto_output"] = final_output
    scene["mmd_auto_output_type"] = "STAGE_MP4" if is_stage else ("TRANSPARENT_PRORES4444_MOV" if not background else "MP4_COMPOSITE_WITH_BACKGROUND_AUDIO")
    scene["mmd_auto_rgba_frames_dir"] = frames_dir

    log("Saving Blender project: " + blend_path)
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    return {
        "output": final_output,
        "transparent": transparent_path,
        "frames_dir": frames_dir,
        "blend": blend_path,
        "manifest": manifest_path,
        "frames": scene.frame_end,
    }


class MMDAutoSettings(PropertyGroup):
    model_path: StringProperty(
        name="模型",
        subtype='FILE_PATH',
    )
    motion_path: StringProperty(
        name="动作",
        subtype='FILE_PATH',
    )
    camera_path: StringProperty(
        name="独立镜头",
        subtype='FILE_PATH',
        description="可选：选择单独的镜头 VMD；若动作 VMD 已包含镜头，可留空",
    )
    audio_path: StringProperty(
        name="音频",
        subtype='FILE_PATH',
    )
    output_mode: EnumProperty(
        name="输出模式",
        items=[('COMPOSITE', '原背景合成', '保留原来的透明人物/背景合成流程'),
               ('STAGE', '三维舞台', 'Blender 直接渲染人物、平台和真实阴影')],
        default='COMPOSITE',
    )
    stage_shape: EnumProperty(name='平台形状', items=[('CIRCLE', '圆形', ''), ('SQUARE', '方形', '')], default='CIRCLE')
    stage_radius: FloatProperty(name='平台半径/半宽', default=4.0, min=0.2, max=200.0)
    stage_thickness: FloatProperty(name='平台厚度', default=0.3, min=0.01, max=20.0)
    stage_z_offset: FloatProperty(name='平台高度微调', default=0.0, min=-10.0, max=10.0)
    stage_roughness: FloatProperty(name='粗糙度', default=0.35, min=0.0, max=1.0)
    stage_metallic: FloatProperty(name='金属度', default=0.05, min=0.0, max=1.0)
    stage_color: bpy.props.FloatVectorProperty(name='平台颜色', subtype='COLOR', size=3,
                                               default=(0.11, 0.12, 0.16), min=0.0, max=1.0)
    background_path: StringProperty(
        name="背景",
        subtype='FILE_PATH',
        description="可选：PNG/JPG/WebP/BMP 静态背景，或 MP4/MOV/MKV/WebM 视频背景",
    )
    output_dir: StringProperty(
        name="输出目录",
        subtype='DIR_PATH',
    )

    model_scale: FloatProperty(
        name="模型缩放",
        default=0.08,
        min=0.0001,
        max=10.0,
    )
    motion_scale: FloatProperty(
        name="动作缩放",
        default=0.08,
        min=0.0001,
        max=10.0,
    )
    motion_margin: IntProperty(
        name="VMD Margin",
        default=0,
        min=0,
        max=1000,
        description="VMD 导入前置空白帧；建议保持 0，避免第一帧出现默认姿势",
    )

    fps: IntProperty(
        name="FPS",
        default=30,
        min=1,
        max=120,
    )
    width: IntProperty(
        name="宽",
        default=1080,
        min=320,
        max=7680,
    )
    height: IntProperty(
        name="高",
        default=1920,
        min=240,
        max=4320,
    )

    auto_bake_physics: BoolProperty(
        name="渲染前自动物理烘焙",
        default=True,
        description="导入模型和 VMD 后，先执行 MMD Tools 物理构建，再烘焙缓存",
    )

    render_video: BoolProperty(
        name="自动渲染 MP4",
        default=True,
    )
    preview_first: BoolProperty(
        name="生成前先保存一帧预览",
        default=True,
        description="完整渲染前保存一张 PNG，检查模型是否正确进入镜头",
    )
    preview_frame: IntProperty(
        name="预览帧",
        default=60,
        min=1,
        max=100000,
        description="正式动画的第 N 帧；1 表示 VMD 起始帧",
    )
    show_render_progress: BoolProperty(
        name="显示渲染进度",
        default=True,
        description="渲染期间在 Blender 状态/控制台输出帧数、百分比、已用时间和预计剩余时间",
    )

    batch_motion_dir: StringProperty(
        name="动作文件夹",
        subtype='DIR_PATH',
        description="批量生成：文件夹中的每个 VMD 都会寻找同名音频",
    )
    batch_audio_dir: StringProperty(
        name="配套音频文件夹",
        subtype='DIR_PATH',
        description="可选；为空时优先在动作文件夹中寻找同名音频",
    )
    batch_camera_dir: StringProperty(
        name="配套镜头文件夹",
        subtype='DIR_PATH',
        description="批量生成：从此文件夹按动作文件名寻找同名镜头 VMD；找不到则使用默认固定相机",
    )

    clear_scene: BoolProperty(
        name="生成前清空场景",
        default=True,
    )


class MMDAUTO_OT_bake_physics(Operator):
    bl_idname = "mmd_auto.bake_physics"
    bl_label = "烘焙当前场景物理"
    bl_description = "先启用 MMD Tools 物理，再按当前时间范围执行 Blender 物理烘焙"
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            initial_pose = fix_vmd_initial_pose(context.scene)
            activation = activate_mmd_physics(context.scene)
            result = bake_scene_physics(context.scene, context.scene.mmd_auto)
            result["mmd_activation"] = activation
            if result["status"] == "no_cache":
                self.report({'WARNING'}, "当前场景没有找到可烘焙的物理缓存")
            else:
                self.report({'INFO'}, "物理烘焙完成：%d 组缓存" % result["baked_count"])
            return {'FINISHED'}
        except Exception as exc:
            log("PHYSICS ERROR: " + repr(exc))
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class MMDAUTO_OT_build(Operator):
    bl_idname = "mmd_auto.build"
    bl_label = "生成 MMD 视频"
    bl_description = "模型 + VMD + 音频 -> Blender 场景 -> MP4"

    def execute(self, context):
        settings = context.scene.mmd_auto

        try:
            if not settings.model_path or not settings.motion_path or not settings.audio_path:
                self.report({'ERROR'}, "请先选择模型、动作和音频")
                return {'CANCELLED'}

            camera_path = abs_path(settings.camera_path)
            if camera_path:
                if not os.path.isfile(camera_path):
                    self.report({'ERROR'}, "独立镜头文件不存在")
                    return {'CANCELLED'}
                camera_info = read_vmd_channels(camera_path)
                if camera_info["camera"] <= 0 or camera_info["bone"] > 0:
                    self.report({'ERROR'}, "独立镜头需要是包含镜头关键帧、且不含人物骨骼动作的 VMD 文件")
                    return {'CANCELLED'}

            result = build_pipeline(settings, camera_override=camera_path)

            self.report(
                {'INFO'},
                "完成: " + result["output"],
            )
            return {'FINISHED'}

        except Exception as exc:
            log("ERROR: " + repr(exc))
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class MMDAUTO_OT_batch_build(Operator):
    bl_idname = "mmd_auto.batch_build"
    bl_label = "批量生成 MMD"
    bl_description = "一个模型 + 动作文件夹 + 同名音频/镜头 -> 依次生成多个 MMD 视频"

    def execute(self, context):
        settings = context.scene.mmd_auto
        try:
            if not settings.model_path:
                self.report({'ERROR'}, "请先选择模型")
                return {'CANCELLED'}
            if not settings.batch_motion_dir:
                self.report({'ERROR'}, "请先选择动作文件夹")
                return {'CANCELLED'}

            jobs = collect_batch_jobs(settings.batch_motion_dir, settings.batch_audio_dir)
            if not jobs:
                raise RuntimeError("动作文件夹中没有找到 .vmd 文件")

            missing = [p for p, a in jobs if not a]
            if missing:
                names = ", ".join(Path(p).name for p in missing[:8])
                extra = " ..." if len(missing) > 8 else ""
                raise RuntimeError(
                    "以下动作没有找到同名音频，请检查文件名是否完全对应：" + names + extra
                )

            log(f"开始批量生成，共 {len(jobs)} 个任务")
            results = []
            for idx, (motion, audio) in enumerate(jobs, 1):
                # 每个任务都从干净场景开始，避免前一个动作/音频残留。
                settings.motion_path = motion
                settings.audio_path = audio
                camera = find_separate_camera_vmd(motion, settings.batch_camera_dir)
                if camera:
                    log("匹配到配套镜头: " + camera)
                else:
                    info = read_vmd_channels(motion)
                    if info["camera"] > 0:
                        log("未找到同名独立镜头，动作 VMD 内含镜头数据，将使用内含镜头。")
                    else:
                        log("未找到同名配套镜头，使用默认固定相机。")
                result = build_pipeline(
                    settings,
                    motion_override=motion,
                    audio_override=audio,
                    camera_override=camera,
                    batch_index=idx,
                    batch_total=len(jobs),
                )
                results.append(result["output"])

            log("===== 批量生成完成 =====")
            for p in results:
                log("完成: " + p)
            self.report({'INFO'}, f"批量完成：{len(results)} 个视频")
            return {'FINISHED'}

        except Exception as exc:
            log("BATCH ERROR: " + repr(exc))
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class MMDAUTO_OT_validate(Operator):
    bl_idname = "mmd_auto.validate"
    bl_label = "检查环境"

    def execute(self, context):
        settings = context.scene.mmd_auto

        try:
            get_mmd_ops()

            errors = validate_inputs(
                settings.model_path,
                settings.motion_path,
                settings.audio_path,
                settings.background_path,
            )

            if errors:
                self.report({'WARNING'}, "；".join(errors)[:240])
                return {'CANCELLED'}

            self.report(
                {'INFO'},
                "检查通过：MMD Tools + 模型 + VMD + 音频均可用",
            )
            return {'FINISHED'}

        except Exception as exc:
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class MMDAUTO_OT_save_config(Operator):
    bl_idname = "mmd_auto.save_config"
    bl_label = "保存配置"

    def execute(self, context):
        settings = context.scene.mmd_auto

        base = (
            abs_path(settings.output_dir)
            or os.path.dirname(abs_path(settings.model_path))
            or os.path.expanduser("~")
        )

        ensure_dir(base)

        config_path = os.path.join(
            base,
            "mmd_auto_config.json",
        )

        data = {
            "model_path": settings.model_path,
            "motion_path": settings.motion_path,
            "camera_path": settings.camera_path,
            "audio_path": settings.audio_path,
            "background_path": settings.background_path,
            "output_dir": settings.output_dir,
            "model_scale": settings.model_scale,
            "motion_scale": settings.motion_scale,
            "motion_margin": settings.motion_margin,
            "fps": settings.fps,
            "width": settings.width,
            "height": settings.height,
            "render_video": settings.render_video,
            "auto_bake_physics": settings.auto_bake_physics,
        }

        write_manifest(config_path, data)

        self.report(
            {'INFO'},
            "配置已保存: " + config_path,
        )
        return {'FINISHED'}


class MMDAUTO_PT_panel(Panel):
    bl_label = "MMD Auto Video V1.0 + 三维舞台"
    bl_idname = "MMDAUTO_PT_panel"

    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MMD Auto"

    def draw(self, context):
        settings = context.scene.mmd_auto
        layout = self.layout

        # 1. Single-task inputs
        box = layout.box()
        box.label(text="单个视频", icon='FILE_MOVIE')
        col = box.column(align=True)
        col.prop(settings, "model_path", text="模型")
        col.prop(settings, "motion_path", text="动作")
        col.prop(settings, "camera_path", text="镜头（可选）")
        col.prop(settings, "audio_path", text="音频")
        col.prop(settings, "output_mode", text="输出模式")
        if settings.output_mode == 'COMPOSITE':
            col.prop(settings, "background_path", text="背景")
        else:
            stage_box = box.box()
            stage_box.label(text="三维舞台与灯光", icon='MESH_CYLINDER')
            stage_box.prop(settings, 'stage_shape')
            stage_box.prop(settings, 'stage_radius')
            stage_box.prop(settings, 'stage_thickness')
            stage_box.prop(settings, 'stage_z_offset')
            stage_box.prop(settings, 'stage_color')
            stage_box.prop(settings, 'stage_roughness')
            stage_box.prop(settings, 'stage_metallic')

        # Main action stays close to the required input fields.
        row = box.row()
        row.scale_y = 1.25
        row.enabled = bool(
            settings.model_path and settings.motion_path and settings.audio_path
        )
        row.operator("mmd_auto.build", text="生成视频", icon='RENDER_ANIMATION')

        # 2. Batch generation
        batch = layout.box()
        batch.label(text="批量生成", icon='DUPLICATE')
        batch.label(text="按文件名匹配动作、音频和镜头", icon='INFO')
        col = batch.column(align=True)
        col.prop(settings, "batch_motion_dir", text="动作文件夹")
        col.prop(settings, "batch_audio_dir", text="音频文件夹")
        col.prop(settings, "batch_camera_dir", text="镜头文件夹")
        row = batch.row()
        row.scale_y = 1.15
        row.enabled = bool(settings.model_path and settings.batch_motion_dir)
        row.operator("mmd_auto.batch_build", text="开始批量生成", icon='RENDER_ANIMATION')

        # 3. Output options
        output = layout.box()
        output.label(text="输出设置", icon='OUTPUT')
        output.prop(settings, "output_dir", text="保存位置")
        row = output.row(align=True)
        row.prop(settings, "width", text="宽")
        row.prop(settings, "height", text="高")
        output.prop(settings, "fps", text="帧率")

        # 4. Advanced options; preserve all existing settings.
        advanced = layout.box()
        advanced.label(text="高级设置", icon='PREFERENCES')
        col = advanced.column(align=True)
        col.prop(settings, "model_scale", text="模型缩放")
        col.prop(settings, "motion_scale", text="动作缩放")
        col.prop(settings, "motion_margin", text="VMD Margin（自动生成固定为0）")
        advanced.separator()
        col = advanced.column(align=True)
        col.prop(settings, "preview_first", text="生成预览图")
        row = col.row()
        row.enabled = settings.preview_first
        row.prop(settings, "preview_frame", text="预览帧")
        col.prop(settings, "show_render_progress", text="显示渲染进度")
        col.prop(settings, "clear_scene", text="生成前清空场景")
        # Kept visible for compatibility with existing saved settings.
        col.prop(settings, "render_video", text="自动渲染 MP4")

        physics = layout.box()
        physics.label(text="物理烘焙", icon='PHYSICS')
        physics.prop(settings, "auto_bake_physics", text="生成视频前自动烘焙")
        physics.operator("mmd_auto.bake_physics", text="烘焙当前场景物理", icon='MOD_PHYSICS')

        # 5. Utility actions
        tools = layout.box()
        tools.label(text="工具", icon='TOOL_SETTINGS')
        row = tools.row(align=True)
        row.operator("mmd_auto.validate", text="检查环境", icon='CHECKMARK')
        row.operator("mmd_auto.save_config", text="保存配置", icon='FILE_TICK')


classes = (
    MMDAutoSettings,
    MMDAUTO_OT_bake_physics,
    MMDAUTO_OT_build,
    MMDAUTO_OT_batch_build,
    MMDAUTO_OT_validate,
    MMDAUTO_OT_save_config,
    MMDAUTO_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.mmd_auto = bpy.props.PointerProperty(
        type=MMDAutoSettings
    )


def unregister():
    if hasattr(bpy.types.Scene, "mmd_auto"):
        del bpy.types.Scene.mmd_auto

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
