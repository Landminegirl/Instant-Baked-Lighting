
bl_info = {
    "name": "Baked Lighting Game Maps",
    "author": "LandmineGirl",
    "version": (1, 7, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N Panel > Bake Maps",
    "description": "Bake Blender lighting, material channels, normals, AO, selected objects, and collections to game-ready textures.",
    "category": "Object",
}

import bpy
import os
import re
import math
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


TEMP_TEX_NODE_NAME = "__BLGM_TEMP_BAKE_TARGET__"
TEMP_UV_NODE_NAME = "__BLGM_TEMP_BAKE_UV__"
TEMP_OVERRIDE_NODE_PREFIX = "__BLGM_TEMP_OVERRIDE__"


# ============================================================
# Channel data
# ============================================================

CHANNEL_LABELS = {
    "LIT": "Lit / Emission",
    "BASE_COLOR": "Base Color",
    "NORMAL": "Normal",
    "EMISSION_ONLY": "Emission Only",
    "DIFFUSE": "Diffuse",
    "GLOSSY": "Glossy",
    "TRANSMISSION": "Transmission",
    "ROUGHNESS": "Roughness",
    "METALLIC": "Metallic",
    "SPECULAR": "Specular",
    "ALPHA": "Alpha",
    "SHADOW": "Shadow",
    "ENVIRONMENT": "Environment",
    "LIGHT_DIRECT": "Direct Light",
    "LIGHT_INDIRECT": "Indirect Light",
    "UV": "UV Layout",
}

CHANNEL_SUFFIXES = {
    "LIT": "lit_baked",
    "BASE_COLOR": "base_color",
    "NORMAL": "normal",
    "EMISSION_ONLY": "emission_only",
    "DIFFUSE": "diffuse",
    "GLOSSY": "glossy",
    "TRANSMISSION": "transmission",
    "ROUGHNESS": "roughness",
    "METALLIC": "metallic",
    "SPECULAR": "specular",
    "ALPHA": "alpha",
    "SHADOW": "shadow",
    "ENVIRONMENT": "environment",
    "LIGHT_DIRECT": "direct_light",
    "LIGHT_INDIRECT": "indirect_light",
    "UV": "uv_layout",
    "AO_TEMP": "ao_temp",
}

CUSTOM_INPUT_CHANNELS = {"METALLIC", "SPECULAR", "ALPHA"}

CHANNEL_PROP_MAP = [
    ("bake_base_color", "BASE_COLOR"),
    ("bake_lit_emission", "LIT"),
    ("bake_normal", "NORMAL"),
    ("bake_emission_only", "EMISSION_ONLY"),
    ("bake_diffuse", "DIFFUSE"),
    ("bake_glossy", "GLOSSY"),
    ("bake_transmission", "TRANSMISSION"),
    ("bake_roughness", "ROUGHNESS"),
    ("bake_metallic", "METALLIC"),
    ("bake_specular", "SPECULAR"),
    ("bake_alpha", "ALPHA"),
    ("bake_shadow", "SHADOW"),
    ("bake_environment", "ENVIRONMENT"),
    ("bake_direct_light", "LIGHT_DIRECT"),
    ("bake_indirect_light", "LIGHT_INDIRECT"),
    ("bake_uv_layout", "UV"),
]


# ============================================================
# Basic helpers
# ============================================================

def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\-]+", "_", name.strip())
    return name or "Object"


def abspath(path: str) -> str:
    return bpy.path.abspath(path)


def ensure_folder(path: str) -> str:
    folder = abspath(path)
    os.makedirs(folder, exist_ok=True)
    return folder


def set_if_exists(obj, attr, value):
    if hasattr(obj, attr):
        try:
            setattr(obj, attr, value)
        except Exception:
            pass


def set_colorspace(image, colorspace):
    try:
        image.colorspace_settings.name = colorspace
    except Exception:
        pass


def get_selected_mesh(context):
    obj = context.object
    if obj is None:
        raise RuntimeError("No active object selected.")
    if obj.type != "MESH":
        raise RuntimeError("Active object must be a mesh.")
    return obj


def unique_mesh_objects(objects):
    seen = set()
    result = []
    for obj in objects:
        if not obj or obj.type != "MESH":
            continue
        if obj.name in seen:
            continue
        seen.add(obj.name)
        result.append(obj)
    return result


def meshes_from_collection(collection, include_children=True):
    if collection is None:
        return []

    objects = [obj for obj in collection.objects if obj.type == "MESH"]

    if include_children:
        for child in collection.children:
            objects.extend(meshes_from_collection(child, include_children=True))

    return unique_mesh_objects(objects)


def get_target_collection(context, settings):
    if settings.target_scope == "CHOSEN_COLLECTION":
        if settings.target_collection is None:
            raise RuntimeError("Target Scope is Chosen Collection, but no collection is assigned.")
        return settings.target_collection

    return context.collection


def get_target_mesh_objects(context, settings):
    if settings.target_scope == "ACTIVE":
        return [get_selected_mesh(context)]

    if settings.target_scope == "SELECTED":
        meshes = unique_mesh_objects(context.selected_objects)
        if not meshes:
            raise RuntimeError("No selected mesh objects.")
        return meshes

    if settings.target_scope in {"ACTIVE_COLLECTION", "CHOSEN_COLLECTION"}:
        collection = get_target_collection(context, settings)
        meshes = meshes_from_collection(collection, settings.include_collection_children)
        if not meshes:
            raise RuntimeError("No mesh objects found in the target collection.")
        return meshes

    return [get_selected_mesh(context)]


def active_target_name(context, objects):
    if len(objects) == 1:
        return safe_name(objects[0].name)

    col = context.scene.blgm_settings.target_collection
    if context.scene.blgm_settings.target_scope == "CHOSEN_COLLECTION" and col:
        return safe_name(col.name)

    if context.scene.blgm_settings.target_scope == "ACTIVE_COLLECTION" and context.collection:
        return safe_name(context.collection.name)

    return "Combined_Bake"


def force_object_mode(context):
    obj = context.object
    if obj:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass


def find_input(node, names):
    if not node:
        return None
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None


def find_output(node, names):
    if not node:
        return None
    for name in names:
        if name in node.outputs:
            return node.outputs[name]
    return None


def first_socket_link(input_socket):
    if input_socket and input_socket.is_linked and input_socket.links:
        return input_socket.links[0]
    return None


def socket_source_if_linked(socket):
    link = first_socket_link(socket)
    if link:
        return link.from_socket
    return None


def find_alpha_source_socket(mat):
    """
    Finds the best alpha source in common Blender material setups:
    - Principled BSDF Alpha input
    - Image Texture Alpha feeding Principled Alpha
    - Image Texture Alpha feeding Mix Shader factor for Transparent BSDF fences/cards
    - Any image alpha output in the material as a fallback
    """
    if not mat or not mat.use_nodes:
        return None, None

    nodes = mat.node_tree.nodes

    # 1) Principled BSDF Alpha input, including linked image alpha.
    principled = find_principled(nodes)
    alpha_input = find_input(principled, ["Alpha"]) if principled else None
    if alpha_input:
        linked = socket_source_if_linked(alpha_input)
        if linked:
            return linked, None

        value_node = nodes.new("ShaderNodeValue")
        value_node.name = f"{TEMP_OVERRIDE_NODE_PREFIX}_ALPHA_VALUE"
        value_node.label = "TEMP Alpha Value"
        try:
            value_node.outputs[0].default_value = float(alpha_input.default_value)
        except Exception:
            value_node.outputs[0].default_value = 1.0
        return value_node.outputs[0], value_node

    # 2) Any image texture alpha output fallback.
    for node in nodes:
        if node.bl_idname == "ShaderNodeTexImage" and "Alpha" in node.outputs:
            return node.outputs["Alpha"], None

    value_node = nodes.new("ShaderNodeValue")
    value_node.name = f"{TEMP_OVERRIDE_NODE_PREFIX}_ALPHA_VALUE"
    value_node.label = "TEMP Alpha Value"
    value_node.outputs[0].default_value = 1.0
    return value_node.outputs[0], value_node


def set_material_alpha_settings(mat):
    """
    Different Blender versions expose alpha settings under slightly different
    property names. Set the ones that exist.
    """
    for attr, value in [
        ("blend_method", "BLEND"),
        ("surface_render_method", "BLENDED"),
        ("use_screen_refraction", False),
        ("show_transparent_back", True),
    ]:
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:
                pass

    if hasattr(mat, "alpha_threshold"):
        try:
            mat.alpha_threshold = 0.5
        except Exception:
            pass


def copy_custom_bake_props(src_obj, dst_obj):
    """
    obj.copy() usually copies custom props, but do it explicitly so the
    material builder always finds the baked images on the export duplicate.
    """
    for key in src_obj.keys():
        if str(key).startswith("BLGM_"):
            try:
                dst_obj[key] = src_obj[key]
            except Exception:
                pass


def copy_uv_layer_values(src_layer, dst_layer):
    if not src_layer or not dst_layer:
        return

    count = min(len(src_layer.data), len(dst_layer.data))
    for i in range(count):
        try:
            dst_layer.data[i].uv = src_layer.data[i].uv
        except Exception:
            pass


def ensure_uv_layer_for_export(obj, uv_name, source_obj=None):
    """
    Make absolutely sure the export/preview object has the UV map that the
    baked material nodes request by name.

    This fixes copies that show the baked image on the wrong UV map because
    the duplicate had the original UV active/rendering instead of the bake UV,
    or because the bake UV name was missing/renamed after duplication/joining.
    """
    if not obj or obj.type != "MESH":
        return uv_name

    mesh = obj.data
    uv_name = (uv_name or obj.get("BLGM_BAKE_UV", "") or "").strip()

    if not uv_name:
        if mesh.uv_layers.active:
            uv_name = mesh.uv_layers.active.name
        else:
            uv_name = "BLGM_BAKE_UV"

    # If the target UV exists, just activate it.
    if mesh.uv_layers.get(uv_name):
        set_active_uv(mesh, uv_name)
        obj["BLGM_BAKE_UV"] = uv_name
        return uv_name

    # Try to copy the UV data from the source object's matching UV layer.
    src_layer = None
    if source_obj and source_obj.type == "MESH":
        src_layer = source_obj.data.uv_layers.get(uv_name)

    # Fallback to the current active UV on the export mesh.
    fallback_layer = mesh.uv_layers.active if mesh.uv_layers else None

    # If source has the wanted UV and geometry matches, copy it into a new layer.
    if src_layer:
        new_layer = mesh.uv_layers.new(name=uv_name)
        copy_uv_layer_values(src_layer, new_layer)
        set_active_uv(mesh, uv_name)
        obj["BLGM_BAKE_UV"] = uv_name
        return uv_name

    # Last resort: rename/copy the active layer so the material's UV Map node
    # points at something valid instead of silently using the wrong/default UV.
    if fallback_layer:
        try:
            fallback_layer.name = uv_name
            set_active_uv(mesh, uv_name)
            obj["BLGM_BAKE_UV"] = uv_name
            return uv_name
        except Exception:
            new_layer = mesh.uv_layers.new(name=uv_name)
            copy_uv_layer_values(fallback_layer, new_layer)
            set_active_uv(mesh, uv_name)
            obj["BLGM_BAKE_UV"] = uv_name
            return uv_name

    # Mesh had no UVs at all. Create a blank one so the node name resolves.
    new_layer = mesh.uv_layers.new(name=uv_name)
    set_active_uv(mesh, uv_name)
    obj["BLGM_BAKE_UV"] = uv_name
    return uv_name


def force_baked_material_uv(mat, uv_name):
    """
    Force every UV Map node in our generated baked/export material to the
    bake UV name. This is intentionally broad because the material is newly
    generated by this add-on, so it should not contain user-authored UV nodes.
    """
    if not mat or not mat.use_nodes:
        return

    for node in mat.node_tree.nodes:
        if node.bl_idname == "ShaderNodeUVMap":
            try:
                node.uv_map = uv_name
            except Exception:
                pass


def assign_export_material(obj, mat, uv_name, clear_existing=True):
    uv_name = ensure_uv_layer_for_export(obj, uv_name)

    if clear_existing:
        obj.data.materials.clear()

    obj.data.materials.append(mat)
    force_baked_material_uv(mat, uv_name)
    set_active_uv(obj.data, uv_name)
    obj["BLGM_BAKE_UV"] = uv_name


# ============================================================
# Image creation / saving
# ============================================================

def image_suffix(kind):
    return CHANNEL_SUFFIXES.get(kind, kind.lower())


def make_image(obj, kind, settings):
    size = int(settings.resolution)
    base = safe_name(obj.name)
    suffix = image_suffix(kind)
    image_name = f"{base}_{suffix}_{size}"
    filename = f"{image_name}.png"

    old = bpy.data.images.get(image_name)
    if old and settings.overwrite_existing_images:
        bpy.data.images.remove(old)

    image = bpy.data.images.new(
        name=image_name,
        width=size,
        height=size,
        alpha=True,
        float_buffer=False,
    )

    if kind in {"NORMAL", "ROUGHNESS", "METALLIC", "SPECULAR", "ALPHA", "SHADOW", "UV", "AO_TEMP"}:
        set_colorspace(image, "Non-Color")
    else:
        set_colorspace(image, "sRGB")

    if kind == "NORMAL":
        try:
            pixels = [0.5, 0.5, 1.0, 1.0] * (size * size)
            image.pixels.foreach_set(pixels)
            image.update()
        except Exception:
            pass

    folder = ensure_folder(settings.output_dir)
    filepath = os.path.join(folder, filename)
    return image, filepath


def save_image(image, filepath, settings):
    image.filepath_raw = filepath
    image.file_format = "PNG"

    if settings.save_files:
        image.save()

    if settings.pack_images:
        try:
            image.pack()
        except Exception:
            pass


# ============================================================
# Render / bake state
# ============================================================

def store_scene_bake_state(scene):
    old = {
        "engine": scene.render.engine,
    }

    if hasattr(scene, "cycles"):
        old["samples"] = scene.cycles.samples
        if hasattr(scene.cycles, "use_denoising"):
            old["use_denoising"] = scene.cycles.use_denoising

    bake = scene.render.bake
    for attr in [
        "target",
        "use_clear",
        "margin",
        "use_selected_to_active",
        "cage_extrusion",
        "use_pass_direct",
        "use_pass_indirect",
        "use_pass_color",
        "use_pass_diffuse",
        "use_pass_glossy",
        "use_pass_transmission",
        "use_pass_emit",
        "normal_space",
    ]:
        if hasattr(bake, attr):
            old[f"bake_{attr}"] = getattr(bake, attr)

    return old


def apply_scene_bake_state(scene, settings):
    scene.render.engine = "CYCLES"

    if hasattr(scene, "cycles"):
        scene.cycles.samples = settings.samples
        if hasattr(scene.cycles, "use_denoising"):
            scene.cycles.use_denoising = False

    bake = scene.render.bake
    set_if_exists(bake, "target", "IMAGE_TEXTURES")
    set_if_exists(bake, "use_clear", True)
    set_if_exists(bake, "margin", settings.margin)
    set_if_exists(bake, "use_selected_to_active", False)
    set_if_exists(bake, "cage_extrusion", 0.0)


def restore_scene_bake_state(scene, old):
    if not old:
        return

    if "engine" in old:
        scene.render.engine = old["engine"]

    if hasattr(scene, "cycles"):
        if "samples" in old:
            scene.cycles.samples = old["samples"]
        if "use_denoising" in old:
            scene.cycles.use_denoising = old["use_denoising"]

    bake = scene.render.bake
    for key, value in old.items():
        if key.startswith("bake_"):
            attr = key[5:]
            set_if_exists(bake, attr, value)


def clear_bake_pass_flags(scene):
    bake = scene.render.bake
    for attr in [
        "use_pass_direct",
        "use_pass_indirect",
        "use_pass_color",
        "use_pass_diffuse",
        "use_pass_glossy",
        "use_pass_transmission",
        "use_pass_emit",
    ]:
        set_if_exists(bake, attr, False)


def enable_all_surface_passes(scene):
    bake = scene.render.bake
    set_if_exists(bake, "use_pass_diffuse", True)
    set_if_exists(bake, "use_pass_glossy", True)
    set_if_exists(bake, "use_pass_transmission", True)


def configure_bake_for_kind(scene, kind, settings):
    bake = scene.render.bake
    clear_bake_pass_flags(scene)

    if kind == "BASE_COLOR":
        # Albedo only, no lighting.
        set_if_exists(bake, "use_pass_color", True)

    elif kind == "LIT":
        # Full scene lighting + shadows + material color + emission.
        set_if_exists(bake, "use_pass_direct", True)
        set_if_exists(bake, "use_pass_indirect", True)
        set_if_exists(bake, "use_pass_color", True)
        set_if_exists(bake, "use_pass_emit", True)
        enable_all_surface_passes(scene)

    elif kind == "DIFFUSE":
        set_if_exists(bake, "use_pass_direct", True)
        set_if_exists(bake, "use_pass_indirect", True)
        set_if_exists(bake, "use_pass_color", True)

    elif kind == "GLOSSY":
        set_if_exists(bake, "use_pass_direct", True)
        set_if_exists(bake, "use_pass_indirect", True)
        set_if_exists(bake, "use_pass_color", True)

    elif kind == "TRANSMISSION":
        set_if_exists(bake, "use_pass_direct", True)
        set_if_exists(bake, "use_pass_indirect", True)
        set_if_exists(bake, "use_pass_color", True)

    elif kind == "LIGHT_DIRECT":
        set_if_exists(bake, "use_pass_direct", True)
        set_if_exists(bake, "use_pass_color", True)
        enable_all_surface_passes(scene)

    elif kind == "LIGHT_INDIRECT":
        set_if_exists(bake, "use_pass_indirect", True)
        set_if_exists(bake, "use_pass_color", True)
        enable_all_surface_passes(scene)

    elif kind == "NORMAL":
        set_if_exists(bake, "normal_space", "TANGENT")

    # AO, SHADOW, ROUGHNESS, EMIT, ENVIRONMENT, UV, and custom EMIT
    # channels do not need extra pass flags here.


def bake_type_for_kind(kind):
    if kind == "BASE_COLOR":
        return "DIFFUSE"
    if kind == "LIT":
        return "COMBINED"
    if kind == "NORMAL":
        return "NORMAL"
    if kind == "EMISSION_ONLY":
        return "EMIT"
    if kind == "DIFFUSE":
        return "DIFFUSE"
    if kind == "GLOSSY":
        return "GLOSSY"
    if kind == "TRANSMISSION":
        return "TRANSMISSION"
    if kind == "ROUGHNESS":
        return "ROUGHNESS"
    if kind == "SHADOW":
        return "SHADOW"
    if kind == "ENVIRONMENT":
        return "ENVIRONMENT"
    if kind == "UV":
        return "UV"
    if kind == "AO_TEMP":
        return "AO"
    if kind in {"LIGHT_DIRECT", "LIGHT_INDIRECT"}:
        return "COMBINED"
    if kind in CUSTOM_INPUT_CHANNELS:
        return "EMIT"
    return "COMBINED"


# ============================================================
# UV handling
# ============================================================

def get_uv_index(mesh, uv_name):
    for i, uv in enumerate(mesh.uv_layers):
        if uv.name == uv_name:
            return i
    return -1


def get_active_uv_name(mesh):
    if mesh.uv_layers and mesh.uv_layers.active:
        return mesh.uv_layers.active.name
    return ""


def set_active_uv(mesh, uv_name):
    idx = get_uv_index(mesh, uv_name)
    if idx < 0:
        raise RuntimeError(f"UV map not found: {uv_name}")

    try:
        mesh.uv_layers.active_index = idx
    except Exception:
        pass

    try:
        mesh.uv_layers.active = mesh.uv_layers[idx]
    except Exception:
        pass

    try:
        mesh.uv_layers.active_render = mesh.uv_layers[idx]
    except Exception:
        pass


def new_or_get_uv(mesh, uv_name, replace_existing=False):
    old = mesh.uv_layers.get(uv_name)
    if old and replace_existing:
        mesh.uv_layers.remove(old)
        old = None

    if old:
        return old

    return mesh.uv_layers.new(name=uv_name)


def smart_project_to_uv(context, obj, uv_name, settings, replace_existing=False):
    mesh = obj.data
    uv = new_or_get_uv(mesh, uv_name, replace_existing=replace_existing)
    set_active_uv(mesh, uv.name)

    old_active_obj = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    old_mode = obj.mode

    try:
        force_object_mode(context)

        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj

        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")

        bpy.ops.uv.smart_project(
            angle_limit=settings.smart_uv_angle,
            island_margin=settings.smart_uv_margin,
            area_weight=settings.smart_uv_area_weight,
        )

        bpy.ops.object.mode_set(mode="OBJECT")

    finally:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

        bpy.ops.object.select_all(action="DESELECT")
        for old in old_selected:
            if old and old.name in bpy.data.objects:
                old.select_set(True)

        if old_active_obj and old_active_obj.name in bpy.data.objects:
            context.view_layer.objects.active = old_active_obj

        if obj and obj.name in bpy.data.objects:
            try:
                context.view_layer.objects.active = obj
                if old_mode != "OBJECT":
                    bpy.ops.object.mode_set(mode=old_mode)
            except Exception:
                pass

    return uv.name


def ensure_bake_uv(context, obj, settings):
    mesh = obj.data

    if settings.uv_mode == "ACTIVE":
        if not mesh.uv_layers.active:
            if not settings.auto_uv_if_missing:
                raise RuntimeError("No active UV map. Enable Auto UV or create a UV map first.")
            return smart_project_to_uv(
                context,
                obj,
                settings.bake_uv_name.strip() or "BLGM_BAKE_UV",
                settings,
                replace_existing=False,
            )
        return mesh.uv_layers.active.name

    if settings.uv_mode == "NAMED":
        wanted = settings.existing_uv_name.strip()
        if not wanted:
            raise RuntimeError("Existing UV Name is blank.")
        if not mesh.uv_layers.get(wanted):
            if not settings.auto_uv_if_missing:
                raise RuntimeError(f"Could not find UV map named '{wanted}'.")
            return smart_project_to_uv(
                context,
                obj,
                wanted,
                settings,
                replace_existing=False,
            )
        set_active_uv(mesh, wanted)
        return wanted

    bake_uv = settings.bake_uv_name.strip() or "BLGM_BAKE_UV"

    if settings.rebuild_bake_uv_each_bake:
        return smart_project_to_uv(
            context,
            obj,
            bake_uv,
            settings,
            replace_existing=True,
        )

    if mesh.uv_layers.get(bake_uv):
        set_active_uv(mesh, bake_uv)
        return bake_uv

    return smart_project_to_uv(
        context,
        obj,
        bake_uv,
        settings,
        replace_existing=False,
    )


# ============================================================
# Temporary material nodes
# ============================================================

def find_material_output(nodes):
    active = None
    fallback = None

    for node in nodes:
        if node.bl_idname == "ShaderNodeOutputMaterial":
            if fallback is None:
                fallback = node
            if getattr(node, "is_active_output", False):
                active = node
                break

    return active or fallback


def find_principled(nodes):
    for node in nodes:
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
    return None


def make_default_value_node(nodes, kind, default_value):
    node = nodes.new("ShaderNodeValue")
    node.name = f"{TEMP_OVERRIDE_NODE_PREFIX}_{kind}_VALUE"
    node.label = f"TEMP {kind} Value"
    node.location = (-900, -900)

    try:
        if isinstance(default_value, (tuple, list)):
            node.outputs[0].default_value = float(default_value[0])
        else:
            node.outputs[0].default_value = float(default_value)
    except Exception:
        node.outputs[0].default_value = 0.0

    return node, node.outputs[0]


def get_custom_input_socket(mat, kind):
    if not mat or not mat.use_nodes:
        return None, None

    nodes = mat.node_tree.nodes
    principled = find_principled(nodes)
    if not principled:
        return None, None

    if kind == "METALLIC":
        socket = find_input(principled, ["Metallic"])
    elif kind == "SPECULAR":
        socket = find_input(principled, ["Specular IOR Level", "Specular", "Specular Tint"])
    elif kind == "ALPHA":
        socket = find_input(principled, ["Alpha"])
    else:
        socket = None

    return principled, socket


def add_custom_channel_override(rec, kind):
    """
    Temporarily converts a material input such as Metallic/Alpha/Specular
    into an emission shader, then Blender's EMIT bake captures it.
    The original material output surface link is restored after baking.
    """
    mat = rec["mat"]
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    output = find_material_output(nodes)
    if not output:
        output = nodes.new("ShaderNodeOutputMaterial")
        output.name = f"{TEMP_OVERRIDE_NODE_PREFIX}_OUTPUT"
        output.label = "TEMP Material Output"
        output.location = (300, -900)
        rec["override_nodes"].append(output)

    surface_input = output.inputs.get("Surface")
    old_surface_links = []
    if surface_input:
        for link in list(surface_input.links):
            old_surface_links.append((link.from_node, link.from_socket, link.to_node, link.to_socket))
            links.remove(link)

    rec["old_surface_links"] = old_surface_links

    source_socket = None

    if kind == "ALPHA":
        source_socket, helper_node = find_alpha_source_socket(mat)
        if helper_node:
            rec["override_nodes"].append(helper_node)
    else:
        principled, input_socket = get_custom_input_socket(mat, kind)

        if input_socket:
            link = first_socket_link(input_socket)
            if link:
                source_socket = link.from_socket
            else:
                default_value = getattr(input_socket, "default_value", 0.0)
                value_node, source_socket = make_default_value_node(nodes, kind, default_value)
                rec["override_nodes"].append(value_node)
        else:
            default_value = 0.0
            value_node, source_socket = make_default_value_node(nodes, kind, default_value)
            rec["override_nodes"].append(value_node)

    emission = nodes.new("ShaderNodeEmission")
    emission.name = f"{TEMP_OVERRIDE_NODE_PREFIX}_{kind}_EMISSION"
    emission.label = f"TEMP Bake {kind}"
    emission.location = (-300, -900)
    rec["override_nodes"].append(emission)

    try:
        links.new(source_socket, emission.inputs["Color"])
    except Exception:
        # Value to Color usually works, but keep a safe fallback.
        try:
            emission.inputs["Color"].default_value = (1, 1, 1, 1)
        except Exception:
            pass

    try:
        emission.inputs["Strength"].default_value = 1.0
    except Exception:
        pass

    if surface_input:
        links.new(emission.outputs["Emission"], surface_input)


def prepare_materials_for_bake(obj, image, uv_name, kind=None):
    """
    Adds temporary bake target Image Texture nodes to each material.
    Also adds a temporary UV Map node linked to the image Vector input so
    Blender uses the bake/lightmap UV, not whatever UV the material uses.
    Original material slots and nodes are restored afterward.
    """
    records = []

    if not obj.material_slots:
        mat = bpy.data.materials.new(f"{safe_name(obj.name)}_TEMP_BAKE_MAT")
        mat.use_nodes = True
        obj.data.materials.append(mat)

    for index, slot in enumerate(obj.material_slots):
        original_mat = slot.material

        if original_mat is None:
            mat = bpy.data.materials.new(f"{safe_name(obj.name)}_TEMP_BAKE_MAT_{index}")
            mat.use_nodes = True
            slot.material = mat
            created_temp_mat = True
        else:
            mat = original_mat
            created_temp_mat = False

        old_use_nodes = mat.use_nodes
        if not mat.use_nodes:
            mat.use_nodes = True

        nt = mat.node_tree
        nodes = nt.nodes
        links = nt.links

        old_active = nodes.active
        old_selected = [n for n in nodes if n.select]

        for node in nodes:
            node.select = False

        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.name = TEMP_UV_NODE_NAME
        uv_node.label = "TEMP Bake UV"
        uv_node.uv_map = uv_name
        uv_node.location = (-600, -600)

        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.name = TEMP_TEX_NODE_NAME
        tex_node.label = "TEMP Bake Target"
        tex_node.image = image
        tex_node.location = (-350, -600)

        try:
            links.new(uv_node.outputs["UV"], tex_node.inputs["Vector"])
        except Exception:
            pass

        tex_node.select = True
        nodes.active = tex_node

        rec = {
            "slot": slot,
            "mat": mat,
            "original_mat": original_mat,
            "created_temp_mat": created_temp_mat,
            "old_use_nodes": old_use_nodes,
            "old_active": old_active,
            "old_selected": old_selected,
            "temp_nodes": [uv_node, tex_node],
            "override_nodes": [],
            "old_surface_links": [],
        }

        if kind in CUSTOM_INPUT_CHANNELS:
            add_custom_channel_override(rec, kind)

        records.append(rec)

    return records


def restore_materials_after_bake(records):
    for rec in records:
        mat = rec["mat"]

        if mat and mat.name in bpy.data.materials and mat.use_nodes:
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            # Remove temporary override links/nodes first.
            for node in rec.get("override_nodes", []):
                try:
                    if node and node.name in nodes:
                        nodes.remove(node)
                except Exception:
                    pass

            # Restore previous material output surface links.
            for from_node, from_socket, to_node, to_socket in rec.get("old_surface_links", []):
                try:
                    if from_node.name in nodes and to_node.name in nodes:
                        links.new(from_socket, to_socket)
                except Exception:
                    pass

            # Remove temporary bake target and UV nodes.
            for temp in rec["temp_nodes"]:
                try:
                    if temp and temp.name in nodes:
                        nodes.remove(temp)
                except Exception:
                    pass

            for node in nodes:
                node.select = False

            for node in rec["old_selected"]:
                try:
                    if node and node.name in nodes:
                        node.select = True
                except Exception:
                    pass

            try:
                if rec["old_active"] and rec["old_active"].name in nodes:
                    nodes.active = rec["old_active"]
            except Exception:
                pass

            mat.use_nodes = rec["old_use_nodes"]

        if rec["created_temp_mat"]:
            try:
                rec["slot"].material = rec["original_mat"]
            except Exception:
                pass

            if mat and mat.name in bpy.data.materials and mat.users == 0:
                bpy.data.materials.remove(mat)


# ============================================================
# AO compositing
# ============================================================

def apply_ao_to_image(target_img, ao_img, strength=1.0):
    """
    Multiplies AO into RGB channels. Alpha is preserved.
    strength 0 = no AO, 1 = full AO.
    """
    if not target_img or not ao_img:
        return

    if target_img.size[0] != ao_img.size[0] or target_img.size[1] != ao_img.size[1]:
        raise RuntimeError("AO image size does not match target image size.")

    target_pixels = list(target_img.pixels[:])
    ao_pixels = list(ao_img.pixels[:])
    strength = max(0.0, min(1.0, float(strength)))

    for i in range(0, len(target_pixels), 4):
        ao = (ao_pixels[i] + ao_pixels[i + 1] + ao_pixels[i + 2]) / 3.0
        factor = 1.0 - ((1.0 - ao) * strength)

        target_pixels[i] *= factor
        target_pixels[i + 1] *= factor
        target_pixels[i + 2] *= factor
        # target_pixels[i + 3] stays unchanged

    target_img.pixels.foreach_set(target_pixels)
    target_img.update()


# ============================================================
# Bake operation
# ============================================================

def bake_kind_to_image(context, obj, kind, image, filepath, uv_name, settings, operator=None, save_result=True, store_result=True):
    scene = context.scene
    old_active_obj = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    old_mode = obj.mode
    old_scene_state = None
    records = []

    try:
        force_object_mode(context)

        old_scene_state = store_scene_bake_state(scene)
        apply_scene_bake_state(scene, settings)

        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj

        records = prepare_materials_for_bake(obj, image, uv_name, kind=kind)
        configure_bake_for_kind(scene, kind, settings)

        bpy.ops.object.bake(type=bake_type_for_kind(kind))

        if save_result:
            save_image(image, filepath, settings)

        if store_result:
            obj[f"BLGM_{kind}_IMAGE"] = image.name
            obj[f"BLGM_{kind}_PATH"] = filepath

        if operator:
            label = CHANNEL_LABELS.get(kind, kind.replace("_", " ").title())
            operator.report({"INFO"}, f"Baked {label} using UV '{uv_name}'")

        return image, filepath

    finally:
        restore_materials_after_bake(records)
        restore_scene_bake_state(scene, old_scene_state)

        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

        bpy.ops.object.select_all(action="DESELECT")
        for sel in old_selected:
            if sel and sel.name in bpy.data.objects:
                sel.select_set(True)

        if old_active_obj and old_active_obj.name in bpy.data.objects:
            context.view_layer.objects.active = old_active_obj

        if obj and obj.name in bpy.data.objects:
            try:
                context.view_layer.objects.active = obj
                if old_mode != "OBJECT":
                    bpy.ops.object.mode_set(mode=old_mode)
            except Exception:
                pass


def bake_single_channel(context, kind, operator=None):
    scene = context.scene
    settings = scene.blgm_settings
    obj = get_selected_mesh(context)
    mesh = obj.data

    old_active_uv = get_active_uv_name(mesh)

    try:
        uv_name = ensure_bake_uv(context, obj, settings)
        set_active_uv(mesh, uv_name)

        image, filepath = make_image(obj, kind, settings)
        bake_kind_to_image(context, obj, kind, image, filepath, uv_name, settings, operator=operator)

        obj["BLGM_BAKE_UV"] = uv_name
        return image, filepath, uv_name

    finally:
        if settings.restore_original_active_uv and old_active_uv and mesh.uv_layers.get(old_active_uv):
            try:
                set_active_uv(mesh, old_active_uv)
            except Exception:
                pass


def get_checked_channels(settings):
    channels = []
    for prop, kind in CHANNEL_PROP_MAP:
        if getattr(settings, prop, False):
            channels.append(kind)
    return channels


def bake_checked_channels_for_object(context, obj, operator=None):
    scene = context.scene
    settings = scene.blgm_settings
    mesh = obj.data

    channels = get_checked_channels(settings)
    use_ao = settings.bake_ambient_occlusion

    if not channels and not use_ao:
        raise RuntimeError("No bake channels are checked.")

    old_active_uv = get_active_uv_name(mesh)
    results = {}

    try:
        uv_name = ensure_bake_uv(context, obj, settings)
        set_active_uv(mesh, uv_name)
        obj["BLGM_BAKE_UV"] = uv_name

        for kind in channels:
            image, filepath = make_image(obj, kind, settings)
            bake_kind_to_image(context, obj, kind, image, filepath, uv_name, settings, operator=operator)
            results[kind] = (image, filepath)

        if use_ao:
            targets = []
            if settings.ao_into_base_color:
                if "BASE_COLOR" in results:
                    targets.append("BASE_COLOR")
                elif settings.ao_may_use_last_baked and obj.get("BLGM_BASE_COLOR_IMAGE", "") in bpy.data.images:
                    img = bpy.data.images[obj["BLGM_BASE_COLOR_IMAGE"]]
                    path = obj.get("BLGM_BASE_COLOR_PATH", "")
                    if path:
                        targets.append("BASE_COLOR_LAST")
                        results["BASE_COLOR_LAST"] = (img, path)

            if settings.ao_into_lit_emission:
                if "LIT" in results:
                    targets.append("LIT")
                elif settings.ao_may_use_last_baked and obj.get("BLGM_LIT_IMAGE", "") in bpy.data.images:
                    img = bpy.data.images[obj["BLGM_LIT_IMAGE"]]
                    path = obj.get("BLGM_LIT_PATH", "")
                    if path:
                        targets.append("LIT_LAST")
                        results["LIT_LAST"] = (img, path)

            if not targets:
                if operator:
                    operator.report({"WARNING"}, f"AO was checked for {obj.name}, but Base Color/Lit were not baked this run.")
            else:
                ao_image, ao_path = make_image(obj, "AO_TEMP", settings)
                bake_kind_to_image(
                    context,
                    obj,
                    "AO_TEMP",
                    ao_image,
                    ao_path,
                    uv_name,
                    settings,
                    operator=None,
                    save_result=False,
                    store_result=False,
                )

                for target_kind in targets:
                    target_image, target_path = results[target_kind]
                    apply_ao_to_image(target_image, ao_image, settings.ao_strength)
                    save_image(target_image, target_path, settings)

                try:
                    bpy.data.images.remove(ao_image)
                except Exception:
                    pass

                if operator:
                    operator.report({"INFO"}, f"Baked AO into selected maps for {obj.name}.")

        return results

    finally:
        if settings.restore_original_active_uv and old_active_uv and mesh.uv_layers.get(old_active_uv):
            try:
                set_active_uv(mesh, old_active_uv)
            except Exception:
                pass


def duplicate_and_join_for_combined_bake(context, objects, combined_name):
    """
    Creates a real duplicate joined mesh for atlas baking.
    Source objects/materials stay untouched.
    """
    if not objects:
        raise RuntimeError("No mesh objects to combine.")

    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)

    duplicates = []
    target_collection = context.collection or context.scene.collection

    try:
        force_object_mode(context)
        bpy.ops.object.select_all(action="DESELECT")

        for obj in objects:
            dup = obj.copy()
            dup.data = obj.data.copy()
            dup.animation_data_clear()
            dup.name = f"{obj.name}_BLGM_TMP_JOIN"
            target_collection.objects.link(dup)
            duplicates.append(dup)

        bpy.ops.object.select_all(action="DESELECT")
        for dup in duplicates:
            dup.select_set(True)

        context.view_layer.objects.active = duplicates[0]
        bpy.ops.object.join()

        joined = context.view_layer.objects.active
        joined.name = combined_name
        joined.data.name = f"{combined_name}_Mesh"

        # Joined object contains copied material slots from all source objects.
        return joined

    except Exception:
        for dup in duplicates:
            if dup and dup.name in bpy.data.objects:
                bpy.data.objects.remove(dup, do_unlink=True)
        raise

    finally:
        for obj in old_selected:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        if old_active and old_active.name in bpy.data.objects:
            context.view_layer.objects.active = old_active


def set_objects_hidden(objects, hidden=True):
    state = []
    for obj in objects:
        state.append((obj, obj.hide_viewport, obj.hide_render))
        obj.hide_viewport = hidden
        obj.hide_render = hidden
    return state


def restore_objects_hidden(state):
    for obj, hide_viewport, hide_render in state:
        if obj and obj.name in bpy.data.objects:
            obj.hide_viewport = hide_viewport
            obj.hide_render = hide_render


def bake_combined_atlas(context, objects, operator=None):
    settings = context.scene.blgm_settings
    base_name = active_target_name(context, objects)
    combined_name = f"{base_name}_GAME_ATLAS"

    joined = duplicate_and_join_for_combined_bake(context, objects, combined_name)

    hidden_state = []
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)

    try:
        if settings.hide_originals_during_combined_bake:
            hidden_state = set_objects_hidden(objects, True)

        bpy.ops.object.select_all(action="DESELECT")
        joined.select_set(True)
        context.view_layer.objects.active = joined

        # For one-atlas mode we always need a fresh atlas UV.
        old_mode = settings.uv_mode
        old_rebuild = settings.rebuild_bake_uv_each_bake
        old_bake_uv_name = settings.bake_uv_name

        settings.uv_mode = "DEDICATED"
        settings.rebuild_bake_uv_each_bake = True
        settings.bake_uv_name = "BLGM_COMBINED_ATLAS_UV"

        try:
            results = bake_checked_channels_for_object(context, joined, operator=operator)
        finally:
            settings.uv_mode = old_mode
            settings.rebuild_bake_uv_each_bake = old_rebuild
            settings.bake_uv_name = old_bake_uv_name

        if settings.combined_apply_game_material:
            uv_name = joined.get("BLGM_BAKE_UV", "BLGM_COMBINED_ATLAS_UV")
            uv_name = ensure_uv_layer_for_export(joined, uv_name, source_obj=joined)
            if settings.export_material_mode == "SHADELESS_PREVIEW":
                mat = create_preview_emission_material(joined, uv_name)
            else:
                mat = create_game_pbr_material(joined, uv_name)

            assign_export_material(joined, mat, uv_name, clear_existing=True)

        bpy.ops.object.select_all(action="DESELECT")
        joined.select_set(True)
        context.view_layer.objects.active = joined

        if operator:
            operator.report({"INFO"}, f"Baked {len(objects)} objects into one atlas object: {joined.name}")

        return {joined.name: results}

    finally:
        restore_objects_hidden(hidden_state)

        # Keep the combined atlas selected because that is the useful result.
        if joined and joined.name in bpy.data.objects:
            bpy.ops.object.select_all(action="DESELECT")
            joined.select_set(True)
            context.view_layer.objects.active = joined


def bake_checked_channels(context, operator=None):
    settings = context.scene.blgm_settings
    objects = get_target_mesh_objects(context, settings)

    if settings.multi_object_material_mode == "ONE_ATLAS":
        return bake_combined_atlas(context, objects, operator=operator)

    results = {}
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)

    try:
        for obj in objects:
            if obj and obj.name in bpy.data.objects:
                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                context.view_layer.objects.active = obj
                results[obj.name] = bake_checked_channels_for_object(context, obj, operator=operator)

        if operator:
            operator.report({"INFO"}, f"Baked checked channels for {len(objects)} object(s).")

        return results

    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in old_selected:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        if old_active and old_active.name in bpy.data.objects:
            context.view_layer.objects.active = old_active


# ============================================================
# Game/export copy material
# ============================================================

def get_image_from_obj_prop(obj, kind):
    image_name = obj.get(f"BLGM_{kind}_IMAGE", "")
    if image_name and image_name in bpy.data.images:
        return bpy.data.images[image_name]
    return None


def make_tex_node(nodes, image, label, x, y, colorspace, uv_node=None, links=None):
    tex = nodes.new("ShaderNodeTexImage")
    tex.name = label
    tex.label = label
    tex.image = image
    tex.location = (x, y)

    if image:
        set_colorspace(image, colorspace)

    if uv_node and links:
        try:
            links.new(uv_node.outputs["UV"], tex.inputs["Vector"])
        except Exception:
            pass

    return tex


def clear_nodes(nodes):
    for node in list(nodes):
        nodes.remove(node)


def create_preview_emission_material(obj, uv_name):
    """
    Shadeless preview material:
    - Lit bake displays the baked scene lighting at strength 1 by default.
    - Emission-only bake is added on top with a boost, so original glowing
      signs/windows/lights stay bright without making the whole map overbright.
    - Alpha bake mixes the result against Transparent BSDF for fences/cards.
    """
    settings = bpy.context.scene.blgm_settings

    lit_img = get_image_from_obj_prop(obj, "LIT")
    emit_img = get_image_from_obj_prop(obj, "EMISSION_ONLY")
    alpha_img = get_image_from_obj_prop(obj, "ALPHA") if settings.export_use_alpha else None

    if not lit_img and not emit_img:
        raise RuntimeError("No baked lit/emission images found. Bake Lit and/or Emission Only first.")

    mat = bpy.data.materials.new(f"{safe_name(obj.name)}_GAME_PREVIEW_SHADELESS")
    mat.use_nodes = True
    if alpha_img:
        set_material_alpha_settings(mat)

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    clear_nodes(nodes)

    uv = nodes.new("ShaderNodeUVMap")
    uv.name = "Baked UV"
    uv.label = "Baked UV"
    uv.uv_map = uv_name
    uv.location = (-1050, 0)

    shader_outputs = []

    if lit_img:
        lit_tex = make_tex_node(nodes, lit_img, "Baked Lit Texture", -780, 170, "sRGB", uv, links)

        lit_emit = nodes.new("ShaderNodeEmission")
        lit_emit.name = "Baked Lighting Shadeless"
        lit_emit.label = "Baked Lighting Shadeless"
        lit_emit.location = (-470, 170)

        links.new(lit_tex.outputs["Color"], lit_emit.inputs["Color"])
        lit_emit.inputs["Strength"].default_value = settings.preview_baked_lighting_strength
        shader_outputs.append(lit_emit.outputs["Emission"])

    if emit_img:
        glow_tex = make_tex_node(nodes, emit_img, "Emission Glow Boost Map", -780, -70, "sRGB", uv, links)

        glow_emit = nodes.new("ShaderNodeEmission")
        glow_emit.name = "Original Emission Glow Boost"
        glow_emit.label = "Original Emission Glow Boost"
        glow_emit.location = (-470, -70)

        links.new(glow_tex.outputs["Color"], glow_emit.inputs["Color"])
        glow_emit.inputs["Strength"].default_value = settings.preview_emissive_boost_strength
        shader_outputs.append(glow_emit.outputs["Emission"])

    if len(shader_outputs) == 1:
        final_shader = shader_outputs[0]
    else:
        add_shader = nodes.new("ShaderNodeAddShader")
        add_shader.name = "Baked Lit + Emission Boost"
        add_shader.label = "Baked Lit + Emission Boost"
        add_shader.location = (-170, 50)
        links.new(shader_outputs[0], add_shader.inputs[0])
        links.new(shader_outputs[1], add_shader.inputs[1])
        final_shader = add_shader.outputs["Shader"]

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (360, 0)

    if alpha_img:
        alpha_tex = make_tex_node(nodes, alpha_img, "Baked Alpha", -780, -330, "Non-Color", uv, links)

        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.name = "Alpha Transparent"
        transparent.label = "Alpha Transparent"
        transparent.location = (-170, -260)

        mix = nodes.new("ShaderNodeMixShader")
        mix.name = "Alpha Mix"
        mix.label = "Alpha Mix"
        mix.location = (90, 0)

        # Fac 0 = transparent, Fac 1 = visible baked shader.
        links.new(alpha_tex.outputs["Color"], mix.inputs[0])
        links.new(transparent.outputs["BSDF"], mix.inputs[1])
        links.new(final_shader, mix.inputs[2])
        links.new(mix.outputs["Shader"], out.inputs["Surface"])
    else:
        links.new(final_shader, out.inputs["Surface"])

    mat["BLGM_NOTE"] = "Preview material. Lit bake is shadeless; emission-only is boosted; alpha map drives transparency."
    return mat


def create_game_pbr_material(obj, uv_name):
    settings = bpy.context.scene.blgm_settings
    base_img = get_image_from_obj_prop(obj, "BASE_COLOR")
    lit_img = get_image_from_obj_prop(obj, "LIT")
    normal_img = get_image_from_obj_prop(obj, "NORMAL")
    emit_img = get_image_from_obj_prop(obj, "EMISSION_ONLY")
    alpha_img = get_image_from_obj_prop(obj, "ALPHA") if settings.export_use_alpha else None

    if not base_img and not lit_img and not normal_img and not emit_img and not alpha_img:
        raise RuntimeError("No baked maps found on this object. Bake maps first.")

    mat = bpy.data.materials.new(f"{safe_name(obj.name)}_GAME_MAPS_MATERIAL")
    mat.use_nodes = True
    if alpha_img:
        set_material_alpha_settings(mat)

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    clear_nodes(nodes)

    uv = nodes.new("ShaderNodeUVMap")
    uv.name = "Baked UV"
    uv.label = "Baked UV"
    uv.uv_map = uv_name
    uv.location = (-1000, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (-150, 0)

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (250, 0)

    try:
        links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    except Exception:
        pass

    if base_img:
        base_tex = make_tex_node(nodes, base_img, "Baked Base Color", -700, 250, "sRGB", uv, links)
        inp = find_input(bsdf, ["Base Color"])
        if inp:
            links.new(base_tex.outputs["Color"], inp)

    chosen_emission = lit_img or emit_img
    if chosen_emission:
        e_tex = make_tex_node(nodes, chosen_emission, "Baked Lit / Emission", -700, 0, "sRGB", uv, links)
        inp = find_input(bsdf, ["Emission Color", "Emission", "Emission Base Color"])
        if inp:
            links.new(e_tex.outputs["Color"], inp)

        strength = find_input(bsdf, ["Emission Strength", "Emission Weight"])
        if strength:
            strength.default_value = settings.game_slot_emission_strength

    if alpha_img:
        alpha_tex = make_tex_node(nodes, alpha_img, "Baked Alpha", -900, -520, "Non-Color", uv, links)
        alpha_input = find_input(bsdf, ["Alpha"])
        if alpha_input:
            links.new(alpha_tex.outputs["Color"], alpha_input)

    if normal_img:
        n_tex = make_tex_node(nodes, normal_img, "Baked Normal", -900, -300, "Non-Color", uv, links)

        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.name = "Baked Normal Map"
        normal_map.label = "Baked Normal Map"
        normal_map.location = (-500, -300)
        normal_map.space = "TANGENT"

        links.new(n_tex.outputs["Color"], normal_map.inputs["Color"])

        inp = find_input(bsdf, ["Normal"])
        if inp:
            links.new(normal_map.outputs["Normal"], inp)

    mat["BLGM_NOTE"] = "Texture-slot material. In Blender this can look double-lit if scene lights are also active."
    return mat


def make_export_copy_for_object(context, obj, operator=None):
    settings = context.scene.blgm_settings

    uv_name = obj.get("BLGM_BAKE_UV", "")
    if not uv_name or not obj.data.uv_layers.get(uv_name):
        uv_name = ensure_bake_uv(context, obj, settings)
        obj["BLGM_BAKE_UV"] = uv_name

    # Copy the object/mesh FIRST, then build the material for the copy.
    # Older versions built the shadeless material from the source object first,
    # which could leave the copy looking at the wrong/default UV in some cases.
    new_obj = obj.copy()
    new_obj.data = obj.data.copy()
    new_obj.animation_data_clear()
    new_obj.name = f"{obj.name}_GAME_BAKED"

    context.collection.objects.link(new_obj)
    copy_custom_bake_props(obj, new_obj)

    uv_name = ensure_uv_layer_for_export(new_obj, uv_name, source_obj=obj)

    if settings.export_material_mode == "SHADELESS_PREVIEW":
        mat = create_preview_emission_material(new_obj, uv_name)
    else:
        mat = create_game_pbr_material(new_obj, uv_name)

    assign_export_material(new_obj, mat, uv_name, clear_existing=True)

    if settings.offset_export_copy:
        new_obj.location.x += obj.dimensions.x * 1.25 if obj.dimensions.x else 1.0

    if operator:
        operator.report({"INFO"}, f"Created export copy using UV '{uv_name}': {new_obj.name}")

    return new_obj


def make_export_copy(context, operator=None):
    settings = context.scene.blgm_settings
    objects = get_target_mesh_objects(context, settings)

    if settings.multi_object_material_mode == "ONE_ATLAS":
        # If active object is already a combined atlas with baked maps, copy that.
        active = context.object
        if active and active.type == "MESH" and active.get("BLGM_BAKE_UV", ""):
            objects = [active]
        else:
            bake_combined_atlas(context, objects, operator=operator)
            objects = [context.object]

    copies = []
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)

    try:
        for obj in objects:
            if obj and obj.name in bpy.data.objects:
                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                context.view_layer.objects.active = obj
                copies.append(make_export_copy_for_object(context, obj, operator=operator))

        bpy.ops.object.select_all(action="DESELECT")
        for obj in copies:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        if copies:
            context.view_layer.objects.active = copies[-1]

        if operator:
            operator.report({"INFO"}, f"Created {len(copies)} export copy object(s).")

        return copies

    finally:
        # Leave copies selected when successful.
        if not copies:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in old_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                context.view_layer.objects.active = old_active


# ============================================================
# Properties
# ============================================================

class BLGM_Settings(bpy.types.PropertyGroup):
    target_scope: EnumProperty(
        name="Bake Target",
        description="What objects the bake buttons operate on",
        items=[
            ("ACTIVE", "Active Object", "Bake only the active selected mesh"),
            ("SELECTED", "Selected Objects", "Bake all selected mesh objects"),
            ("ACTIVE_COLLECTION", "Active Collection", "Bake mesh objects in the active viewport collection"),
            ("CHOSEN_COLLECTION", "Chosen Collection", "Bake mesh objects in the collection assigned below"),
        ],
        default="ACTIVE",
    )

    target_collection: PointerProperty(
        name="Collection",
        description="Collection to bake when Bake Target is Chosen Collection",
        type=bpy.types.Collection,
    )

    include_collection_children: BoolProperty(
        name="Include Child Collections",
        description="When baking a collection, include mesh objects inside child collections too",
        default=True,
    )

    multi_object_material_mode: EnumProperty(
        name="Multi-Object Mode",
        description="How to bake when the target has multiple mesh objects",
        items=[
            ("KEEP_SEPARATE", "Keep Separate Materials", "Bake every object separately and preserve separate baked texture sets/materials"),
            ("ONE_ATLAS", "Bake All Into One", "Duplicate the targets, join them into one object, create one bake UV atlas, and bake one texture set/material"),
        ],
        default="KEEP_SEPARATE",
    )

    combined_apply_game_material: BoolProperty(
        name="Apply One Baked Material",
        description="When baking all into one, replace the combined duplicate's copied materials with one baked preview/export material",
        default=True,
    )

    hide_originals_during_combined_bake: BoolProperty(
        name="Hide Originals While Baking",
        description="When baking all into one, hide the source objects during the bake so duplicate geometry does not double-shadow itself",
        default=True,
    )

    resolution: EnumProperty(
        name="Resolution",
        description="Texture size for baked maps",
        items=[
            ("512", "512", ""),
            ("1024", "1024", ""),
            ("2048", "2048", ""),
            ("4096", "4096", ""),
            ("8192", "8192", ""),
        ],
        default="2048",
    )

    samples: IntProperty(
        name="Samples",
        description="Cycles samples used for baking",
        default=128,
        min=1,
        max=8192,
    )

    margin: IntProperty(
        name="Bake Margin",
        description="Pixel padding around UV islands",
        default=32,
        min=0,
        max=512,
    )

    output_dir: StringProperty(
        name="Output Folder",
        description="Folder where baked PNG files are saved",
        default="//baked_textures",
        subtype="DIR_PATH",
    )

    save_files: BoolProperty(
        name="Save PNG Files",
        description="Save baked images as PNG files",
        default=True,
    )

    pack_images: BoolProperty(
        name="Pack Into Blend",
        description="Also pack baked images into the .blend file",
        default=False,
    )

    overwrite_existing_images: BoolProperty(
        name="Overwrite Same-Name Images",
        description="Remove old in-memory baked images with the same name before baking",
        default=True,
    )

    uv_mode: EnumProperty(
        name="Bake UV Mode",
        description="Which UV map to use for the baked textures",
        items=[
            ("DEDICATED", "Dedicated Bake UV", "Create/use a dedicated non-overlapping bake UV. Best for lightmaps"),
            ("ACTIVE", "Active UV", "Use the object's currently active UV map"),
            ("NAMED", "Named UV", "Use a specific existing UV map by name"),
        ],
        default="DEDICATED",
    )

    bake_uv_name: StringProperty(
        name="Bake UV Name",
        description="Name of the dedicated bake/lightmap UV",
        default="BLGM_BAKE_UV",
    )

    existing_uv_name: StringProperty(
        name="Existing UV Name",
        description="Use this UV map when Bake UV Mode is set to Named UV",
        default="UVMap",
    )

    auto_uv_if_missing: BoolProperty(
        name="Auto UV If Missing",
        description="If the selected UV does not exist, create one with Smart UV Project",
        default=True,
    )

    rebuild_bake_uv_each_bake: BoolProperty(
        name="Rebuild Bake UV Each Bake",
        description="Recreate the dedicated bake UV every time you bake",
        default=True,
    )

    restore_original_active_uv: BoolProperty(
        name="Restore Original Active UV",
        description="After baking, restore the original active UV on the original object",
        default=True,
    )

    smart_uv_angle: FloatProperty(
        name="Smart UV Angle",
        description="Angle limit for Smart UV Project",
        default=math.radians(66.0),
        min=math.radians(1.0),
        max=math.radians(89.0),
        subtype="ANGLE",
    )

    smart_uv_margin: FloatProperty(
        name="UV Island Margin",
        description="Island margin for Smart UV Project",
        default=0.003,
        min=0.0,
        max=1.0,
    )

    smart_uv_area_weight: FloatProperty(
        name="UV Area Weight",
        description="Area weight for Smart UV Project",
        default=0.0,
        min=0.0,
        max=1.0,
    )

    # Checked channel list
    bake_base_color: BoolProperty(name="Base Color", default=True)
    bake_lit_emission: BoolProperty(name="Lit / Emission", default=True)
    bake_normal: BoolProperty(name="Normal", default=True)
    bake_emission_only: BoolProperty(name="Emission Only", default=True)

    bake_ambient_occlusion: BoolProperty(
        name="Ambient Occlusion",
        description="AO does not save a separate map; it multiplies into Base Color and/or Lit / Emission",
        default=False,
    )

    ao_into_base_color: BoolProperty(
        name="AO Into Base Color",
        description="Multiply AO into the baked Base Color map",
        default=True,
    )

    ao_into_lit_emission: BoolProperty(
        name="AO Into Lit / Emission",
        description="Multiply AO into the baked Lit / Emission map",
        default=True,
    )

    ao_strength: FloatProperty(
        name="AO Strength",
        description="How strongly the AO is multiplied into the target maps",
        default=1.0,
        min=0.0,
        max=2.0,
    )

    ao_may_use_last_baked: BoolProperty(
        name="AO Can Use Last Baked Maps",
        description="If Base/Lit is not checked this run, allow AO to multiply into the last baked Base/Lit images on this object",
        default=False,
    )

    bake_diffuse: BoolProperty(name="Diffuse", default=False)
    bake_glossy: BoolProperty(name="Glossy", default=False)
    bake_transmission: BoolProperty(name="Transmission", default=False)
    bake_roughness: BoolProperty(name="Roughness", default=False)
    bake_metallic: BoolProperty(name="Metallic", default=False)
    bake_specular: BoolProperty(name="Specular", default=False)
    bake_alpha: BoolProperty(name="Alpha", default=False)
    bake_shadow: BoolProperty(name="Shadow", default=False)
    bake_environment: BoolProperty(name="Environment", default=False)
    bake_direct_light: BoolProperty(name="Direct Light", default=False)
    bake_indirect_light: BoolProperty(name="Indirect Light", default=False)
    bake_uv_layout: BoolProperty(name="UV Layout", default=False)

    export_material_mode: EnumProperty(
        name="Export Copy Material",
        description="Material type used on the duplicate",
        items=[
            ("SHADELESS_PREVIEW", "Shadeless Baked Preview", "Use the baked maps as shadeless emission so Blender does not double-light them"),
            ("GAME_SLOTS", "Game Texture Slots", "Use Base Color, Lit/Emission, and Normal maps on a Principled material"),
        ],
        default="SHADELESS_PREVIEW",
    )

    preview_baked_lighting_strength: FloatProperty(
        name="Preview Lit Strength",
        description="Strength for the full baked lighting map on the shadeless preview copy",
        default=1.0,
        min=0.0,
        max=100.0,
    )

    preview_emissive_boost_strength: FloatProperty(
        name="Preview Emissive Boost",
        description="Extra strength for the Emission Only map on the shadeless preview copy. This brings back bright glow without brightening the whole level",
        default=5.0,
        min=0.0,
        max=100.0,
    )

    game_slot_emission_strength: FloatProperty(
        name="Game Slot Emission Strength",
        description="Emission strength used by the optional Game Texture Slots preview material",
        default=1.0,
        min=0.0,
        max=100.0,
    )

    export_use_alpha: BoolProperty(
        name="Use Alpha On Export Copy",
        description="Apply the baked Alpha map to the preview/export material. Leave off unless this object actually needs cutout transparency",
        default=False,
    )

    offset_export_copy: BoolProperty(
        name="Offset Export Copy",
        description="Move the generated copy to the side",
        default=True,
    )


# ============================================================
# Operators
# ============================================================

class BLGM_OT_create_bake_uv(bpy.types.Operator):
    bl_idname = "blgm.create_bake_uv"
    bl_label = "Create / Refresh Bake UV"
    bl_description = "Create a dedicated non-overlapping UV map for baking lightmaps"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            obj = get_selected_mesh(context)
            settings = context.scene.blgm_settings
            uv_name = settings.bake_uv_name.strip() or "BLGM_BAKE_UV"
            smart_project_to_uv(
                context,
                obj,
                uv_name,
                settings,
                replace_existing=True,
            )
            obj["BLGM_BAKE_UV"] = uv_name
            self.report({"INFO"}, f"Created/refreshed bake UV: {uv_name}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BLGM_OT_bake_checked(bpy.types.Operator):
    bl_idname = "blgm.bake_checked"
    bl_label = "Bake Checked Channels"
    bl_description = "Bake the checked channels for the selected object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            bake_checked_channels(context, self)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BLGM_OT_channel_preset(bpy.types.Operator):
    bl_idname = "blgm.channel_preset"
    bl_label = "Bake Channel Preset"
    bl_description = "Set checkboxes for a bake channel preset"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        items=[
            ("GAME", "Game Set", "Base Color, Lit/Emission, Emission Only, Normal"),
            ("ALL", "All Channels", "Enable every non-AO output channel"),
            ("NONE", "None", "Disable all channels"),
        ],
        default="GAME",
    )

    def execute(self, context):
        s = context.scene.blgm_settings

        for prop, kind in CHANNEL_PROP_MAP:
            setattr(s, prop, False)

        s.bake_ambient_occlusion = False

        if self.preset == "GAME":
            s.bake_base_color = True
            s.bake_lit_emission = True
            s.bake_emission_only = True
            s.bake_normal = True
        elif self.preset == "ALL":
            for prop, kind in CHANNEL_PROP_MAP:
                setattr(s, prop, True)
            s.bake_ambient_occlusion = True

        return {"FINISHED"}


class BLGM_OT_create_export_copy(bpy.types.Operator):
    bl_idname = "blgm.create_export_copy"
    bl_label = "Create Game Export Copy"
    bl_description = "Duplicate the selected object and apply baked texture material. Original object/materials stay untouched"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            make_export_copy(context, self)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BLGM_OT_open_output_folder(bpy.types.Operator):
    bl_idname = "blgm.open_output_folder"
    bl_label = "Open Output Folder"
    bl_description = "Open the bake output folder"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.blgm_settings
        folder = ensure_folder(settings.output_dir)

        try:
            if os.name == "nt":
                os.startfile(folder)
            elif os.name == "posix":
                import subprocess
                subprocess.Popen(["xdg-open", folder])
            else:
                self.report({"INFO"}, folder)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        return {"FINISHED"}


# ============================================================
# UI
# ============================================================

class BLGM_PT_panel(bpy.types.Panel):
    bl_label = "Bake Game Maps"
    bl_idname = "BLGM_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Bake Maps"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.blgm_settings
        obj = context.object

        box = layout.box()
        box.label(text="Active Object")

        if obj and obj.type == "MESH":
            box.label(text=obj.name, icon="MESH_DATA")
            if obj.data.uv_layers.active:
                box.label(text=f"Active UV: {obj.data.uv_layers.active.name}", icon="GROUP_UVS")
            else:
                box.label(text="No active UV map", icon="ERROR")

            bake_uv = obj.get("BLGM_BAKE_UV", "")
            if bake_uv:
                box.label(text=f"Bake UV Used: {bake_uv}", icon="UV")
        else:
            box.label(text="Select a mesh object", icon="ERROR")

        box = layout.box()
        box.label(text="Targets")
        box.prop(settings, "target_scope")
        if settings.target_scope == "CHOSEN_COLLECTION":
            box.prop(settings, "target_collection")
        if settings.target_scope in {"ACTIVE_COLLECTION", "CHOSEN_COLLECTION"}:
            box.prop(settings, "include_collection_children")
        box.prop(settings, "multi_object_material_mode")
        if settings.multi_object_material_mode == "ONE_ATLAS":
            box.prop(settings, "combined_apply_game_material")
            box.prop(settings, "hide_originals_during_combined_bake")

        box = layout.box()
        box.label(text="Bake Settings")
        box.prop(settings, "resolution")
        box.prop(settings, "samples")
        box.prop(settings, "margin")
        box.prop(settings, "output_dir")
        box.prop(settings, "save_files")
        box.prop(settings, "pack_images")
        box.prop(settings, "overwrite_existing_images")

        box = layout.box()
        box.label(text="Bake UV / Lightmap UV")
        box.prop(settings, "uv_mode")

        if settings.uv_mode == "DEDICATED":
            box.prop(settings, "bake_uv_name")
            box.prop(settings, "rebuild_bake_uv_each_bake")
            box.operator("blgm.create_bake_uv", text="Create / Refresh Bake UV", icon="UV")
        elif settings.uv_mode == "NAMED":
            box.prop(settings, "existing_uv_name")

        box.prop(settings, "auto_uv_if_missing")
        box.prop(settings, "restore_original_active_uv")
        box.prop(settings, "smart_uv_margin")
        box.prop(settings, "smart_uv_angle")
        box.prop(settings, "smart_uv_area_weight")

        box = layout.box()
        box.label(text="Bake Channel List")

        row = box.row(align=True)
        op = row.operator("blgm.channel_preset", text="Game Set")
        op.preset = "GAME"
        op = row.operator("blgm.channel_preset", text="All")
        op.preset = "ALL"
        op = row.operator("blgm.channel_preset", text="None")
        op.preset = "NONE"

        col = box.column(align=True)
        col.prop(settings, "bake_base_color")
        col.prop(settings, "bake_lit_emission")
        col.prop(settings, "bake_normal")
        col.prop(settings, "bake_alpha")
        col.prop(settings, "bake_emission_only")
        col.prop(settings, "bake_ambient_occlusion")

        if settings.bake_ambient_occlusion:
            sub = box.box()
            sub.label(text="AO Multiply Targets")
            sub.prop(settings, "ao_into_base_color")
            sub.prop(settings, "ao_into_lit_emission")
            sub.prop(settings, "ao_strength")
            sub.prop(settings, "ao_may_use_last_baked")

        advanced = box.box()
        advanced.label(text="Extra Bake Channels")
        col = advanced.column(align=True)
        col.prop(settings, "bake_diffuse")
        col.prop(settings, "bake_glossy")
        col.prop(settings, "bake_transmission")
        col.prop(settings, "bake_roughness")
        col.prop(settings, "bake_metallic")
        col.prop(settings, "bake_specular")
        col.prop(settings, "bake_shadow")
        col.prop(settings, "bake_environment")
        col.prop(settings, "bake_direct_light")
        col.prop(settings, "bake_indirect_light")
        col.prop(settings, "bake_uv_layout")

        box.separator()
        box.operator("blgm.bake_checked", text="Bake Checked Channels", icon="RENDER_STILL")

        box = layout.box()
        box.label(text="Optional Export Copy")
        box.prop(settings, "export_material_mode")
        if settings.export_material_mode == "SHADELESS_PREVIEW":
            box.prop(settings, "preview_baked_lighting_strength")
            box.prop(settings, "preview_emissive_boost_strength")
        else:
            box.prop(settings, "game_slot_emission_strength")
        box.prop(settings, "export_use_alpha")
        box.prop(settings, "offset_export_copy")
        box.operator("blgm.create_export_copy", text="Create Game Export Copy", icon="DUPLICATE")

        box = layout.box()
        box.operator("blgm.open_output_folder", text="Open Output Folder", icon="FILE_FOLDER")

        if obj and obj.type == "MESH":
            paths = []
            for kind, label in CHANNEL_LABELS.items():
                path = obj.get(f"BLGM_{kind}_PATH", "")
                if path:
                    paths.append((label, path))

            if paths:
                info = layout.box()
                info.label(text="Last Baked")
                for label, path in paths:
                    row = info.row()
                    row.label(text=f"{label}: {os.path.basename(path)}")


# ============================================================
# Registration
# ============================================================

classes = (
    BLGM_Settings,
    BLGM_OT_create_bake_uv,
    BLGM_OT_bake_checked,
    BLGM_OT_channel_preset,
    BLGM_OT_create_export_copy,
    BLGM_OT_open_output_folder,
    BLGM_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.blgm_settings = PointerProperty(type=BLGM_Settings)


def unregister():
    if hasattr(bpy.types.Scene, "blgm_settings"):
        del bpy.types.Scene.blgm_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
