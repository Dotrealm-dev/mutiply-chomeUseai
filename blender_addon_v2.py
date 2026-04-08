"""
blender_addon_v2.py — DepthMap Bridge v2.1
แก้ไข: สร้าง Plane ทำงานได้จริง + แสดง Status ชัดเจน

วิธีติดตั้ง:
  Edit → Preferences → Add-ons → Install → เลือกไฟล์นี้ → ✓ เปิด
  กด N ใน Viewport → แท็บ "DepthMap"
"""

bl_info = {
    "name":        "DepthMap Bridge v2",
    "author":      "DepthProject",
    "version":     (2, 1, 0),
    "blender":     (3, 0, 0),
    "location":    "View3D › N-Panel › DepthMap",
    "description": "ส่งรูปไปเจน Depth Map + ลบ Watermark แล้วสร้าง Plane อัตโนมัติ",
    "category":    "Render",
}

import bpy
import os
import json
import subprocess
import threading
from pathlib import Path
from bpy.props import StringProperty, BoolProperty
from bpy.types import Panel, Operator


# ─────────────────────────────────────────────
#  Properties
# ─────────────────────────────────────────────
class DepthMapPropsV2(bpy.types.PropertyGroup):
    bot_script: StringProperty(
        name="Bot Script",
        description="เลือกไฟล์ main_bot_v2.py",
        default="",
        subtype="FILE_PATH",
    )
    output_folder: StringProperty(
        name="Output Folder",
        description="โฟลเดอร์ที่รับผลลัพธ์สุดท้าย (Workspace/Output)",
        default="",
        subtype="DIR_PATH",
    )
    watching: BoolProperty(default=False)
    status: StringProperty(default="⬜ พร้อมทำงาน")
    seen_files: StringProperty(default="[]")   # JSON list ของไฟล์ที่เคยเห็น


# ─────────────────────────────────────────────
#  Helper: หา Active Image
# ─────────────────────────────────────────────
def get_active_image(context):
    """หา Image จาก Image Editor หรือ active Material ของ Object"""
    # 1. Image Editor
    for area in context.screen.areas:
        if area.type == "IMAGE_EDITOR":
            img = area.spaces.active.image
            if img:
                return img
    # 2. Active Object → Material → Texture Node
    obj = context.active_object
    if obj and obj.active_material and obj.active_material.use_nodes:
        for node in obj.active_material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image:
                return node.image
    return None


# ─────────────────────────────────────────────
#  Helper: สร้าง Plane + Material
# ─────────────────────────────────────────────
def create_depth_plane(image_path: Path):
    """
    สร้าง Plane ที่มี Aspect Ratio ตรงกับรูป
    แล้วแปะ Material + Depth Map texture
    """
    # โหลด Image
    img = bpy.data.images.load(str(image_path), check_existing=True)
    img.reload()                          # บังคับโหลดใหม่จากดิสก์
    w, h = img.size

    if w == 0 or h == 0:
        print(f"[DepthBot] ⚠️  ขนาดภาพ 0 ข้าม: {image_path.name}")
        return None

    # คำนวณ Aspect Ratio
    aspect = w / h
    pw     = 2.0            # ความกว้าง Plane (หน่วย Blender)
    ph     = pw / aspect    # ความสูง

    # สร้าง Plane
    bpy.ops.mesh.primitive_plane_add(size=1, enter_editmode=False, location=(0, 0, 0))
    plane      = bpy.context.active_object
    plane.name = f"DepthPlane_{image_path.stem}"
    plane.scale = (pw, ph, 1.0)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    # ── สร้าง Material ──
    mat = bpy.data.materials.new(name=f"DepthMat_{image_path.stem}")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # Node: Output
    n_out = nodes.new("ShaderNodeOutputMaterial")
    n_out.location = (400, 0)

    # Node: Emission (แสดงรูปตรงๆ ไม่มีแสงส่งผล)
    n_emit = nodes.new("ShaderNodeEmission")
    n_emit.location = (200, 0)

    # Node: Image Texture
    n_tex = nodes.new("ShaderNodeTexImage")
    n_tex.location = (-100, 0)
    n_tex.image = img
    n_tex.image.colorspace_settings.name = "Non-Color"   # Depth map = linear

    links.new(n_tex.outputs["Color"],     n_emit.inputs["Color"])
    links.new(n_emit.outputs["Emission"], n_out.inputs["Surface"])

    # ผูก Material
    if plane.data.materials:
        plane.data.materials[0] = mat
    else:
        plane.data.materials.append(mat)

    # UV Unwrap
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0.001)
    bpy.ops.object.mode_set(mode="OBJECT")

    print(f"[DepthBot] ✅ Plane สร้างแล้ว: {plane.name}  ({w}×{h}px, ratio={aspect:.3f})")
    return plane


# ─────────────────────────────────────────────
#  Operator 1 — Export + Run Bot
# ─────────────────────────────────────────────
class DEPTHMAP_OT_Process(Operator):
    """Export รูปที่ active → รัน Bot → สร้าง Plane เมื่อเสร็จ"""
    bl_idname = "depthmap.process_v2"
    bl_label  = "🚀 Send to Bot & Process"

    def execute(self, context):
        props = context.scene.dmp_v2

        # ── ตรวจสอบ Bot Script ──
        bot_path = Path(bpy.path.abspath(props.bot_script))
        if not bot_path.exists():
            self.report({"ERROR"}, f"ไม่พบ Bot Script: {bot_path}")
            return {"CANCELLED"}

        # ── หา Image ──
        img = get_active_image(context)
        if img is None:
            self.report({"ERROR"}, "ไม่พบภาพ — เปิดใน Image Editor หรือ เลือก Object ที่มี Texture")
            return {"CANCELLED"}

        # ── กำหนด Input Dir ──
        input_dir = bot_path.parent / "Workspace" / "Input"
        input_dir.mkdir(parents=True, exist_ok=True)

        # ── Export PNG ──
        stem      = Path(img.name).stem or "blender_export"
        dest_path = input_dir / f"{stem}.png"

        # บันทึกผ่าน Blender API
        scene      = context.scene
        orig_path  = img.filepath_raw
        orig_fmt   = scene.render.image_settings.file_format

        img.filepath_raw = str(dest_path)
        img.file_format  = "PNG"
        img.save()

        img.filepath_raw = orig_path   # คืนค่าเดิม
        scene.render.image_settings.file_format = orig_fmt

        props.status = f"⚙️  กำลังประมวลผล: {dest_path.name}..."
        self.report({"INFO"}, f"ส่งรูปแล้ว: {dest_path.name}")

        # ── รัน Bot ใน Thread แยก (ไม่บล็อก Blender) ──
        output_folder = Path(bpy.path.abspath(props.output_folder)) if props.output_folder else bot_path.parent / "Workspace" / "Output"

        def run_bot():
            try:
                result = subprocess.run(
                    ["python", str(bot_path), str(dest_path)],
                    capture_output=True, text=True, encoding="utf-8", timeout=600
                )
                ok = result.returncode == 0
                # Schedule งานใน Main Thread
                bpy.app.timers.register(
                    lambda: _after_bot(context, output_folder, dest_path.stem, ok, result.stderr),
                    first_interval=0.5
                )
            except subprocess.TimeoutExpired:
                bpy.app.timers.register(
                    lambda: _set_status(context, "❌ Bot timeout (>10 นาที)"),
                    first_interval=0.5
                )
            except Exception as e:
                bpy.app.timers.register(
                    lambda: _set_status(context, f"❌ {str(e)[:60]}"),
                    first_interval=0.5
                )

        threading.Thread(target=run_bot, daemon=True).start()
        return {"FINISHED"}


def _set_status(context, text: str):
    """อัปเดต status label (เรียกจาก Timer)"""
    context.scene.dmp_v2.status = text
    # Force redraw N-Panel
    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()
    return None   # หยุด timer


def _after_bot(context, output_folder: Path, stem: str, success: bool, stderr: str):
    """เรียกหลัง Bot เสร็จ — หาไฟล์ผลลัพธ์แล้วสร้าง Plane"""
    if not success:
        _set_status(context, f"❌ Bot ล้มเหลว: {stderr[:60]}")
        return None

    # หาไฟล์ _final_ ล่าสุดใน Output Folder
    if not output_folder.exists():
        _set_status(context, "❌ ไม่พบ Output Folder")
        return None

    # หา _final_ ที่ชื่อขึ้นต้นด้วย stem ของไฟล์ต้นฉบับ
    candidates = sorted(
        [p for p in output_folder.iterdir()
         if "_final_" in p.name and p.suffix.lower() in {".png", ".jpg"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if not candidates:
        _set_status(context, "⚠️  Bot เสร็จแต่ไม่พบไฟล์ผลลัพธ์")
        return None

    newest = candidates[0]
    plane  = create_depth_plane(newest)

    if plane:
        _set_status(context, f"✅ สร้าง Plane แล้ว: {newest.name}")
    else:
        _set_status(context, f"⚠️  โหลดรูปไม่ได้: {newest.name}")

    return None


# ─────────────────────────────────────────────
#  Operator 2/3 — Manual Watch (สำหรับ Watch Mode)
# ─────────────────────────────────────────────
_WATCH_INTERVAL = 3.0

def _watch_timer():
    """Timer ตรวจ Output Folder ทุก 3 วิ (Watch Mode)"""
    try:
        scene = bpy.context.scene
        props = scene.dmp_v2
    except Exception:
        return None

    if not props.watching:
        return None

    output_dir = Path(bpy.path.abspath(props.output_folder))
    if not output_dir.exists():
        return _WATCH_INTERVAL

    supported = {".png", ".jpg", ".jpeg"}
    current   = {str(p) for p in output_dir.iterdir() if p.suffix.lower() in supported}

    try:
        seen = set(json.loads(props.seen_files))
    except Exception:
        seen = set()

    new_files = sorted(current - seen)
    for fp in new_files:
        p = Path(fp)
        # สร้าง Plane เฉพาะไฟล์ _final_
        if "_final_" in p.name:
            plane = create_depth_plane(p)
            if plane:
                props.status = f"✅ Auto-import: {p.name}"

    props.seen_files = json.dumps(list(current))

    # Redraw
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()

    return _WATCH_INTERVAL


class DEPTHMAP_OT_StartWatch(Operator):
    bl_idname = "depthmap.start_watch_v2"
    bl_label  = "▶ Start Auto-Import"

    def execute(self, context):
        props = context.scene.dmp_v2
        out   = Path(bpy.path.abspath(props.output_folder))
        if not out.exists():
            self.report({"ERROR"}, f"ไม่พบ Output Folder: {out}")
            return {"CANCELLED"}

        # Snapshot ไฟล์ปัจจุบัน (ไม่ import ไฟล์เก่า)
        existing = [str(p) for p in out.iterdir()]
        props.seen_files = json.dumps(existing)
        props.watching   = True
        bpy.app.timers.register(_watch_timer, first_interval=_WATCH_INTERVAL)
        props.status = f"👁  กำลัง Watch: {out.name}"
        self.report({"INFO"}, "เริ่ม Watch Output Folder แล้ว")
        return {"FINISHED"}


class DEPTHMAP_OT_StopWatch(Operator):
    bl_idname = "depthmap.stop_watch_v2"
    bl_label  = "⏹ Stop Auto-Import"

    def execute(self, context):
        context.scene.dmp_v2.watching = False
        context.scene.dmp_v2.status   = "⬜ หยุด Watch แล้ว"
        self.report({"INFO"}, "หยุด Watch แล้ว")
        return {"FINISHED"}


# ─────────────────────────────────────────────
#  Operator 4 — Manual Import (กดเอง)
# ─────────────────────────────────────────────
class DEPTHMAP_OT_ManualImport(Operator):
    """นำเข้าไฟล์ล่าสุดใน Output Folder มาสร้าง Plane ทันที"""
    bl_idname = "depthmap.manual_import_v2"
    bl_label  = "📥 Import Latest Result"

    def execute(self, context):
        props = context.scene.dmp_v2
        out   = Path(bpy.path.abspath(props.output_folder))

        if not out.exists():
            self.report({"ERROR"}, "ไม่พบ Output Folder")
            return {"CANCELLED"}

        candidates = sorted(
            [p for p in out.iterdir()
             if "_final_" in p.name and p.suffix.lower() in {".png", ".jpg"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        if not candidates:
            self.report({"WARNING"}, "ไม่พบไฟล์ _final_ ใน Output Folder")
            return {"CANCELLED"}

        plane = create_depth_plane(candidates[0])
        if plane:
            props.status = f"✅ Import แล้ว: {candidates[0].name}"
            self.report({"INFO"}, f"สร้าง Plane: {plane.name}")
        else:
            self.report({"ERROR"}, "โหลดรูปไม่สำเร็จ")
        return {"FINISHED"}


# ─────────────────────────────────────────────
#  N-Panel UI
# ─────────────────────────────────────────────
class DEPTHMAP_PT_PanelV2(Panel):
    bl_label       = "DepthMap Bridge v2"
    bl_idname      = "DEPTHMAP_PT_v2"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "DepthMap"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.dmp_v2

        # ── Settings ──
        box = layout.box()
        box.label(text="⚙️  Settings", icon="PREFERENCES")
        box.prop(props, "bot_script",    text="Bot (.py)")
        box.prop(props, "output_folder", text="Output")

        layout.separator()

        # ── Status ──
        status_box = layout.box()
        status_box.label(text=props.status, icon="INFO")

        layout.separator()

        # ── Main Action ──
        layout.operator("depthmap.process_v2", icon="PLAY")

        layout.separator()

        # ── Manual Import ──
        layout.operator("depthmap.manual_import_v2", icon="IMPORT")

        layout.separator()

        # ── Auto Watch ──
        box2 = layout.box()
        box2.label(text="👁  Auto-Import (Watch Mode)", icon="RESTRICT_VIEW_OFF")
        if not props.watching:
            box2.operator("depthmap.start_watch_v2", icon="PLAY")
        else:
            row = box2.row()
            row.label(text="● กำลัง Watch...", icon="RADIOBUT_ON")
            box2.operator("depthmap.stop_watch_v2", icon="PAUSE")


# ─────────────────────────────────────────────
#  Register
# ─────────────────────────────────────────────
classes = [
    DepthMapPropsV2,
    DEPTHMAP_OT_Process,
    DEPTHMAP_OT_StartWatch,
    DEPTHMAP_OT_StopWatch,
    DEPTHMAP_OT_ManualImport,
    DEPTHMAP_PT_PanelV2,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dmp_v2 = bpy.props.PointerProperty(type=DepthMapPropsV2)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.dmp_v2

if __name__ == "__main__":
    register()
