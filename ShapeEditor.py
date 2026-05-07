import sys
import json, os
import uuid
import copy
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk, filedialog
from typing import List, Optional
import math
import time
import re

# ------------------------------------------------------------
# Units and scaling
# ------------------------------------------------------------

PX_TO_MM        = 1.0
SCALE           = 1000
GRID_SPACING_PX = 10

# ------------------------------------------------------------
# Workflow message strings
# ------------------------------------------------------------

CIRCLE_WORKFLOW_DETAILS = {
    "profile_plus": (
        "Circle  —  Profile Cut  (+offset)\n"
        "─────────────────────────────────\n"
        "• Tool runs OUTSIDE the circle line\n"
        "• Single .nc file produced\n"
        "• No tool change required\n\n"
        "Enter the tool offset (mm) below.\n"
        "Click the canvas ONCE to place the centre."
    ),
    "profile_minus": (
        "Circle  —  Profile Cut  (−offset)\n"
        "─────────────────────────────────\n"
        "• Tool runs INSIDE the circle line\n"
        "• Single .nc file produced\n"
        "• No tool change required\n\n"
        "Enter the tool offset (mm) below.\n"
        "Click the canvas ONCE to place the centre."
    ),
    "pocket_minus": (
        "Circle  —  Pocket Cut  (−offset)\n"
        "────────────────────────────────\n"
        "• Tool clears material inside the circle\n"
        "• Single .nc file produced\n"
        "• No tool change required\n\n"
        "Enter the tool offset (mm) below.\n"
        "Click the canvas ONCE to place the centre."
    ),
    "edge_plus": (
        "Circle  —  Edge Shaped  (+offset)\n"
        "──────────────────────────────────\n"
        "• TWO .nc profile files will be produced:\n"
        "    shapexA.nc  →  Pass 1  (profile / waste removal)\n"
        "    shapexB.nc  →  Pass 2  (edge / moulding tool)\n"
        "• A TOOL CHANGE is required between the two files\n"
        "• Diagnostic shapexA.json and shapexB.json are saved\n"
        "  for inspection before G-code generation\n\n"
        "Enter the tool offset (mm) below.\n"
        "Click the canvas ONCE to place the centre."
    ),
    "edge_minus": (
        "Circle  —  Edge Shaped  (−offset)\n"
        "──────────────────────────────────\n"
        "• TWO .nc profile files will be produced:\n"
        "    shapexA.nc  →  Pass 1  (profile / waste removal)\n"
        "    shapexB.nc  →  Pass 2  (edge / moulding tool)\n"
        "• A TOOL CHANGE is required between the two files\n"
        "• Diagnostic shapexA.json and shapexB.json are saved\n"
        "  for inspection before G-code generation\n\n"
        "Enter the tool offset (mm) below.\n"
        "Click the canvas ONCE to place the centre."
    ),
}

BULGE_WORKFLOW_MSG = (
    "Bulge Edge Shape  —  Workflow\n"
    "──────────────────────────────────────────\n"
    "No offset is applied to this shape type.\n"
    "The S/B trace overlays an existing Rtrace or Btrace.\n\n"
    "Step 1 ▶  Load the base Rtrace or Btrace\n"
    "          (File → Load Shape  or  Add Shape button)\n\n"
    "Step 2 ▶  The S/B shape is now overlaid on the canvas.\n\n"
    "Step 3 ▶  Select the original base trace in the shape\n"
    "          list and press  Clear Trace  to delete it.\n\n"
    "Step 4 ▶  Save the remaining S/B trace to its own file\n"
    "          (Save Selected).\n\n"
    "─── Output ───────────────────────────────────────────\n"
    "• TWO .nc profile files will be produced\n"
    "• A TOOL CHANGE is required between the two files\n"
    "• Diagnostic shapexA.json and shapexB.json will be\n"
    "  saved before G-code generation"
)

# ------------------------------------------------------------
# Core model
# ------------------------------------------------------------

class Point:
    def __init__(self, x, y):
        self.x = float(x)
        self.y = float(y)


class Trace:
    def __init__(self, kind, points=None, closed=False, tid=None, sub=None):
        self.id     = tid or str(uuid.uuid4())
        self.kind   = kind
        self.closed = bool(closed)
        self.sub    = sub
        self._pending_radius = None

        if points is None:
            self.points = []
        else:
            built = []
            for item in points:
                if isinstance(item, Point):
                    built.append(item)
                elif isinstance(item, (list, tuple)):
                    built.append(Point(item[0], item[1]))
                else:
                    built.append(item)
            self.points = built

        self._sync_tr()

    def _sync_tr(self):
        n = len(self.points)
        if not hasattr(self, "tr"):
            self.tr = [None] * n
        elif len(self.tr) < n:
            self.tr += [None] * (n - len(self.tr))
        elif len(self.tr) > n:
            self.tr = self.tr[:n]

    def add_point(self, x, y):
        self.points.append(Point(x, y))
        self.tr.append(None)

    def remove_point(self, idx):
        self.points.pop(idx)
        self.tr.pop(idx)

    def reopen(self):
        if self.closed:
            self.closed = False

    def get_tr(self, seg_index):
        self._sync_tr()
        if 0 <= seg_index < len(self.tr):
            return self.tr[seg_index]
        return None

    def set_tr(self, seg_index: int, value, points=None):
        self._sync_tr()
        if not (0 <= seg_index < len(self.tr)):
            return

        if value is None:
            self.tr[seg_index] = None
            return

        pts = points if points is not None else self.points
        n   = len(pts)

        if seg_index < n - 1:
            p1, p2 = pts[seg_index], pts[seg_index + 1]
        elif self.closed and n > 1:
            p1, p2 = pts[n - 1], pts[0]
        else:
            self.tr[seg_index] = None
            return

        try:
            s = float(value)
        except (TypeError, ValueError):
            self.tr[seg_index] = None
            return

        if not sagitta_valid_for_segment(s, p1, p2):
            self.tr[seg_index] = None
            return

        flip = _kind_flips_arc(self.kind)
        self.tr[seg_index] = sagitta_to_radius(s, p1, p2, flip=flip)

    @property
    def num_segments(self):
        n = len(self.points)
        if n < 2:
            return 0
        return n if self.closed else n - 1


class Shape:
    def __init__(self, id, role, trace):
        self.id       = id
        self.role     = role
        self.trace    = trace
        self.workflow = None
        self.offset   = None


class EditorState:
    def __init__(self):
        self.shapes:            List[Shape]   = []
        self.selected_shape_id: Optional[str] = None
        self.selected_point_id: Optional[int] = None
        self.undo_stack:        List[dict]    = []
        self.redo_stack:        List[dict]    = []

    def get_shape(self, shape_id: str) -> Optional[Shape]:
        for s in self.shapes:
            if s.id == shape_id:
                return s
        return None


# ------------------------------------------------------------
# Geometry helpers
# ------------------------------------------------------------

def dist(x1, y1, x2, y2):
    return ((x2 - x1)**2 + (y2 - y1)**2) ** 0.5


def point_to_segment_distance(px, py, x1, y1, x2, y2):
    if x1 == x2 and y1 == y2:
        return dist(px, py, x1, y1)
    t = ((px - x1)*(x2 - x1) + (py - y1)*(y2 - y1)) / \
        ((x2 - x1)**2 + (y2 - y1)**2)
    t = max(0, min(1, t))
    return dist(px, py, x1 + t*(x2-x1), y1 + t*(y2-y1))


def chord_length(p1: Point, p2: Point) -> float:
    return dist(p1.x, p1.y, p2.x, p2.y)

def sagitta_valid_for_segment(sagitta: float, p1: Point, p2: Point) -> bool:
    if sagitta is None or sagitta == 0:
        return False
    return chord_length(p1, p2) > 1e-9

def sagitta_to_radius(sagitta: float, p1: Point, p2: Point,
                      flip: bool = False) -> float:
    c = chord_length(p1, p2)
    h = c / 2.0
    s = abs(float(sagitta))
    r = (h * h + s * s) / (2.0 * s)
    if sagitta < 0:
        r = -r
    return -r if flip else r


def _kind_flips_arc(kind: str) -> bool:
    return kind in ("S", "B", "R")

def radius_valid_for_segment(radius: float, p1: Point, p2: Point) -> bool:
    return radius is not None and radius != 0


def arc_points(x1, y1, x2, y2, radius, steps=20):
    cx_mid = (x1 + x2) / 2.0
    cy_mid = (y1 + y2) / 2.0
    dx     = x2 - x1
    dy     = y2 - y1
    half_c = math.sqrt(dx*dx + dy*dy) / 2.0
    abs_r  = abs(radius)

    if half_c < 1e-9 or abs_r < half_c:
        return [(x1, y1), (x2, y2)]

    h      = math.sqrt(abs_r * abs_r - half_c * half_c)
    length = math.sqrt(dx*dx + dy*dy)
    perp_x = -dy / length
    perp_y =  dx / length

    side = 1.0 if radius > 0 else -1.0
    ocx  = cx_mid + side * h * perp_x
    ocy  = cy_mid + side * h * perp_y

    a_start = math.atan2(y1 - ocy, x1 - ocx)
    a_end   = math.atan2(y2 - ocy, x2 - ocx)

    diff = a_end - a_start
    while diff >  math.pi: diff -= 2 * math.pi
    while diff < -math.pi: diff += 2 * math.pi

    pts = []
    for i in range(steps + 1):
        t = i / steps
        a = a_start + t * diff
        pts.append((ocx + abs_r * math.cos(a),
                    ocy + abs_r * math.sin(a)))
    return pts


# ------------------------------------------------------------
# Hit-testing
# ------------------------------------------------------------

def hit_test_point(state: EditorState, x: float, y: float, threshold=6):
    for shape_index, shape in enumerate(state.shapes):
        trace = shape.trace
        if trace is None:
            continue
        for point_index, p in enumerate(trace.points):
            if abs(p.x - x) < threshold and abs(p.y - y) < threshold:
                return shape_index, point_index
    return None, None


def hit_test_shape(state: EditorState, x: float, y: float, threshold=3):
    best_shape = None
    best_dist  = float("inf")
    for shape in state.shapes:
        trace = shape.trace
        if trace is None:
            continue
        pts = trace.points
        for i in range(len(pts) - 1):
            d = point_to_segment_distance(x, y,
                    pts[i].x, pts[i].y, pts[i+1].x, pts[i+1].y)
            if d < best_dist and d <= threshold:
                best_dist  = d
                best_shape = shape.id
    return best_shape


def hit_test(state: EditorState, x: float, y: float):
    shape_id, point_id = hit_test_point(state, x, y)
    if shape_id is not None:
        return ("point", shape_id, point_id)
    shape_id = hit_test_shape(state, x, y)
    if shape_id is not None:
        return ("shape", shape_id, None)
    return (None, None, None)


# ------------------------------------------------------------
# Undo / Redo
# ------------------------------------------------------------

def snapshot_state(state: EditorState):
    return copy.deepcopy({
        "shapes":            state.shapes,
        "selected_shape_id": state.selected_shape_id,
        "selected_point_id": state.selected_point_id,
    })


def restore_state(state: EditorState, snap: dict):
    state.shapes            = snap["shapes"]
    state.selected_shape_id = snap["selected_shape_id"]
    state.selected_point_id = snap["selected_point_id"]


def begin_action(state: EditorState):
    state.undo_stack.append(snapshot_state(state))
    state.redo_stack.clear()


def undo(state: EditorState):
    if not state.undo_stack:
        return
    snap = state.undo_stack.pop()
    state.redo_stack.append(snapshot_state(state))
    restore_state(state, snap)


# ------------------------------------------------------------
# Machine settings
# ------------------------------------------------------------

DEFAULT_MACHINE = {
    "feed":           800.0,
    "plunge":         200.0,
    "target_depth":   3.0,
    "stepover":       2.0,
    "num_passes":     3,
    "tow":            10.0,
    "spindle_rpm":    12000.0,
    "tool_id":        1,
    "safe_clearance": 5.0,
    "emit_spindle":   1,
}


class MachineSettingsPopup(tk.Toplevel):
    def __init__(self, root, shape_path):
        super().__init__(root)
        self.title("Machine Settings")
        self.shape_path = shape_path
        self.job_path   = "shapex.json"

        try:
            with open(self.shape_path, "r") as f:
                self.shape_data = json.load(f)
        except Exception:
            self.shape_data = None

        try:
            with open(self.job_path, "r") as f:
                existing      = json.load(f)
                machine_block = existing.get("machine", {})
        except Exception:
            machine_block = {}

        if not isinstance(machine_block, dict):
            machine_block = {}

        fields = [
            ("Feed Rate (mm/min)",        "feed"),
            ("Plunge Rate (mm/min)",       "plunge"),
            ("Target Depth (mm)",          "target_depth"),
            ("Step Over (mm)",             "stepover"),
            ("Number of Passes",           "num_passes"),
            ("Top of material (mm)",       "tow"),
            ("Spindle RPM",                "spindle_rpm"),
            ("Tool ID",                    "tool_id"),
            ("Safe Clearance (mm)",        "safe_clearance"),
            ("Emit Spindle Command (0/1)", "emit_spindle"),
        ]

        self.entries = {}
        for row, (label, key) in enumerate(fields):
            ttk.Label(self, text=label).grid(
                row=row, column=0, sticky="w", padx=5, pady=3)
            entry = ttk.Entry(self)
            entry.grid(row=row, column=1, padx=5, pady=3)
            value = machine_block.get(key, DEFAULT_MACHINE.get(key, ""))
            entry.insert(0, str(value))
            self.entries[key] = entry

        ttk.Button(self, text="Save", command=self.save_settings).grid(
            row=len(fields), column=0, columnspan=2, pady=10)

    def save_settings(self):
        machine = {}
        for key, entry in self.entries.items():
            text = entry.get().strip()
            if text == "":
                machine[key] = None
                continue
            try:
                machine[key] = int(text) \
                    if key in ["tool_id", "num_passes", "emit_spindle"] \
                    else float(text)
            except ValueError:
                machine[key] = text

        with open(self.job_path, "w") as f:
            json.dump({"shape": self.shape_data, "machine": machine},
                      f, indent=4)
        self.destroy()


# ------------------------------------------------------------
# Sub-type dialog
# ------------------------------------------------------------

class SubTypeDialog(tk.Toplevel):
    def __init__(self, parent, kind, callback):
        super().__init__(parent)
        self.title("Shape Sub-Type")
        self.resizable(False, False)
        self.callback = callback
        self.kind     = kind
        self.result   = None

        tk.Label(self, text=f"Configure shape type: {kind}",
                 font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2,
            sticky="ew", padx=10, pady=(10, 2))

        self.sub_var    = tk.StringVar()
        self.radius_var = tk.StringVar(value="10")

        if kind in ("R", "B"):
            self._build_rb_body()
        elif kind == "S":
            self._build_s_body()

        btn_frame = tk.Frame(self)
        btn_frame.grid(row=20, column=0, columnspan=2,
                       pady=(6, 10), padx=10, sticky="e")
        tk.Button(btn_frame, text="OK",
                  command=self._ok,     width=8).pack(side="left", padx=4)
        tk.Button(btn_frame, text="Cancel",
                  command=self._cancel, width=8).pack(side="left", padx=4)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build_rb_body(self):
        tk.Label(self, text="Cut role:",
                 font=("Segoe UI", 9)).grid(
            row=1, column=0, columnspan=2,
            sticky="w", padx=14, pady=(4, 2))

        options = [
            ("profile", "Profile  (cut on outside of line)"),
            ("pocket",  "Pocket   (cut on inside of line)"),
        ]
        self.sub_var.set("profile")
        for i, (val, label) in enumerate(options):
            tk.Radiobutton(self, text=label,
                           variable=self.sub_var, value=val).grid(
                row=2 + i, column=0, columnspan=2,
                sticky="w", padx=24, pady=2)

    def _build_s_body(self):
        tk.Label(self, text="Special shape sub-type:",
                 font=("Segoe UI", 9)).grid(
            row=1, column=0, columnspan=2,
            sticky="w", padx=14, pady=(4, 2))

        options = [
            ("circle",   "Circle          (centre + radius)"),
            ("bulge",    "Bulge edge      (overlays Rtrace / Btrace)"),
            ("drill",    "Drilled hole    (single point)"),
            ("moulding", "Moulding        (open polygon, facetted tool)"),
        ]
        self.sub_var.set("circle")
        for i, (val, label) in enumerate(options):
            tk.Radiobutton(self, text=label,
                           variable=self.sub_var, value=val,
                           command=self._on_sub_change).grid(
                row=2 + i, column=0, columnspan=2,
                sticky="w", padx=24, pady=2)

        self.radius_label = tk.Label(self, text="Radius (mm):")
        self.radius_label.grid(row=7, column=0, sticky="w", padx=14, pady=4)
        self.radius_entry = tk.Entry(
            self, textvariable=self.radius_var, width=10)
        self.radius_entry.grid(row=7, column=1, sticky="w", padx=4, pady=4)

    def _on_sub_change(self):
        if self.kind != "S":
            return
        show_radius = self.sub_var.get() == "circle"
        if show_radius:
            self.radius_label.grid()
            self.radius_entry.grid()
        else:
            self.radius_label.grid_remove()
            self.radius_entry.grid_remove()

    def _ok(self):
        sub    = self.sub_var.get()
        radius = None

        if self.kind == "S" and sub == "circle":
            try:
                radius = float(self.radius_var.get())
            except ValueError:
                messagebox.showerror("Invalid", "Radius must be a number.")
                return

        self.result = (sub, radius)
        self.destroy()
        self.callback(self.kind, sub, radius)

    def _cancel(self):
        self.result = None
        self.destroy()
        self.callback(None, None, None)


# ------------------------------------------------------------
# Circle workflow dialog
# ------------------------------------------------------------

class CircleWorkflowDialog(tk.Toplevel):
    WORKFLOWS = [
        ("profile_plus",  "Profile cut   (+offset)  —  single .nc  |  no tool change"),
        ("profile_minus", "Profile cut   (−offset)  —  single .nc  |  no tool change"),
        ("pocket_minus",  "Pocket cut    (−offset)  —  single .nc  |  no tool change"),
        ("edge_plus",     "Edge shaped   (+offset)  —  TWO .nc files  |  TOOL CHANGE"),
        ("edge_minus",    "Edge shaped   (−offset)  —  TWO .nc files  |  TOOL CHANGE"),
    ]

    def __init__(self, parent, callback):
        super().__init__(parent)
        self.title("Circle — Machining Workflow")
        self.resizable(False, False)
        self.callback   = callback
        self.wf_var     = tk.StringVar(value="profile_plus")
        self.offset_var = tk.StringVar(value="3.0")

        tk.Label(self,
                 text="Select machining workflow for this circle:",
                 font=("Segoe UI", 10, "bold")
                 ).grid(row=0, column=0, columnspan=2,
                        sticky="ew", padx=14, pady=(12, 4))

        for i, (val, label) in enumerate(self.WORKFLOWS):
            fg = "#8B0000" if "TOOL CHANGE" in label else "#003366"
            tk.Radiobutton(
                self, text=label, variable=self.wf_var, value=val,
                fg=fg, font=("Segoe UI", 9),
                command=self._on_wf_change
            ).grid(row=1 + i, column=0, columnspan=2,
                   sticky="w", padx=28, pady=2)

        ttk.Separator(self, orient="horizontal").grid(
            row=10, column=0, columnspan=2,
            sticky="ew", padx=10, pady=6)

        tk.Label(self, text="Tool offset (mm):",
                 font=("Segoe UI", 9, "bold")
                 ).grid(row=11, column=0, sticky="w", padx=14, pady=4)

        self.offset_entry = tk.Entry(
            self, textvariable=self.offset_var, width=10)
        self.offset_entry.grid(row=11, column=1, sticky="w", padx=6, pady=4)

        self.hint_label = tk.Label(
            self, text=self._offset_hint("profile_plus"),
            font=("Segoe UI", 8), fg="#555555",
            justify="left", wraplength=340)
        self.hint_label.grid(row=12, column=0, columnspan=2,
                             sticky="w", padx=14, pady=(0, 6))

        self.info_text = tk.Text(
            self, width=52, height=9,
            font=("Courier New", 8),
            bg="#f5f5f5", relief="sunken",
            state="disabled", wrap="word")
        self.info_text.grid(row=13, column=0, columnspan=2,
                            padx=10, pady=(0, 6))
        self._update_info("profile_plus")

        btn_frame = tk.Frame(self)
        btn_frame.grid(row=14, column=0, columnspan=2,
                       pady=(4, 12), padx=10, sticky="e")
        tk.Button(btn_frame, text="OK",
                  command=self._ok,     width=9).pack(side="left", padx=4)
        tk.Button(btn_frame, text="Cancel",
                  command=self._cancel, width=9).pack(side="left", padx=4)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    @staticmethod
    def _offset_hint(wf_key):
        hints = {
            "profile_plus":  "Positive value → tool centre offset outside the circle.",
            "profile_minus": "Positive value entered; applied as negative (inside).",
            "pocket_minus":  "Positive value entered; applied as negative (inside pocket).",
            "edge_plus":     "Positive value → outside offset for both passes.",
            "edge_minus":    "Positive value entered; applied as negative for both passes.",
        }
        return hints.get(wf_key, "")

    def _on_wf_change(self):
        wf = self.wf_var.get()
        self.hint_label.config(text=self._offset_hint(wf))
        self._update_info(wf)

    def _update_info(self, wf_key):
        text = CIRCLE_WORKFLOW_DETAILS.get(wf_key, "")
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", "end")
        self.info_text.insert("end", text)
        self.info_text.config(state="disabled")

    def _ok(self):
        raw = self.offset_entry.get().strip()
        try:
            offset_val = float(raw)
        except ValueError:
            messagebox.showerror(
                "Invalid Offset",
                "Tool offset must be a number (e.g. 3.0).")
            return

        if offset_val <= 0:
            messagebox.showerror(
                "Invalid Offset",
                "Enter a positive offset value.\n"
                "The sign is applied automatically\n"
                "according to the selected workflow.")
            return

        wf = self.wf_var.get()
        if wf in ("profile_minus", "pocket_minus", "edge_minus"):
            signed_offset = -offset_val
        else:
            signed_offset = offset_val

        self.destroy()
        self.callback(wf, signed_offset)

    def _cancel(self):
        self.destroy()
        self.callback(None, None)


# ------------------------------------------------------------
# Bulge workflow dialog
# ------------------------------------------------------------

class BulgeWorkflowDialog(tk.Toplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.title("Bulge Edge Shape — Workflow")
        self.resizable(False, False)
        self.callback = callback

        tk.Label(
            self,
            text="S / B  —  Bulge Edge Shape",
            font=("Segoe UI", 11, "bold"),
            fg="#003366"
        ).pack(padx=16, pady=(14, 4))

        text_box = tk.Text(
            self, width=58, height=18,
            font=("Courier New", 8),
            bg="#f0f4ff", relief="sunken",
            state="normal", wrap="word")
        text_box.insert("end", BULGE_WORKFLOW_MSG)
        text_box.config(state="disabled")
        text_box.pack(padx=12, pady=(0, 6))

        tk.Label(
            self,
            text="No offset is stored for this shape type.\n"
                 "The S/B trace geometry IS the toolpath.",
            font=("Segoe UI", 9, "italic"),
            fg="#555555", justify="left"
        ).pack(padx=16, pady=(0, 6))

        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=(2, 14))
        tk.Button(btn_frame, text="OK — I understand",
                  command=self._ok,     width=18).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Cancel",
                  command=self._cancel, width=9).pack(side="left", padx=6)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _ok(self):
        self.destroy()
        self.callback(True)

    def _cancel(self):
        self.destroy()
        self.callback(False)


# ------------------------------------------------------------
# ShapeEditor UI
# ------------------------------------------------------------

class ShapeEditor:
    MODE_POINTS = "points"
    MODE_TRACES = "traces"

    def __init__(self, root):
        self.root = root
        self.root.title("Shape Editor")

        self.view_scale    = 1.0
        self.view_offset_x = 0.0
        self.view_offset_y = 0.0

        self.state                 = EditorState()
        self.current_shape_index   = None
        self.selected_point_index  = None
        self.dragging              = False
        self.dragging_shape        = False
        self.dragging_center       = False
        self.dragging_shape_id     = None
        self.shape_drag_start      = None
        self._suppress_type_change = False
        self.show_grid             = True
        self.shape_original_points = None
        self.last_mouse            = (0.0, 0.0)
        self.alert_message         = ""
        self.alert_visible         = False
        self.current_shape_path    = None
        self.moulding_click_time   = 0.0
        self.table_mode            = self.MODE_POINTS

        # NC toolpath state
        self.show_toolpath   = False
        self.toolpath_points = []   # list of (x, y, is_rapid)

        self.trace_type_var   = tk.StringVar(value="")
        self.angle_var        = tk.DoubleVar(value=90.0)
        self.rotate_angle_var = tk.DoubleVar(value=0.0)

        top_frame    = tk.Frame(self.root)
        top_frame.pack(side="top", fill="both", expand=True)
        bottom_frame = tk.Frame(self.root)
        bottom_frame.pack(side="bottom", fill="x")

        self.trace_type_menu = ttk.Combobox(
            bottom_frame, textvariable=self.trace_type_var,
            values=["", "R", "B", "S"], state="readonly", width=10)
        self.trace_type_menu.pack(side="left", padx=5)
        self.trace_type_menu.set("")

        tk.Label(bottom_frame, text="Angle").pack(side="left", padx=5)
        self.angle_entry = tk.Entry(
            bottom_frame, textvariable=self.angle_var,
            width=6, state="disabled")
        self.angle_entry.pack(side="left", padx=5)

        tk.Label(bottom_frame, text="Rotate").pack(side="left", padx=5)
        self.rotate_angle_entry = tk.Entry(
            bottom_frame, textvariable=self.rotate_angle_var,
            width=6, state="disabled")
        self.rotate_angle_entry.pack(side="left", padx=5)

        self.canvas_width  = 600
        self.canvas_height = 400
        self.canvas = tk.Canvas(
            top_frame, bg="white",
            highlightthickness=1, highlightbackground="black")
        self.canvas.pack(side="left", fill="both",
                         expand=True, padx=5, pady=5)

        shapes_frame = tk.Frame(top_frame)
        shapes_frame.pack(side="right", fill="y", padx=5, pady=5)
        self.shapes_list = tk.Listbox(shapes_frame, height=10)
        self.shapes_list.pack(fill="y", expand=False)
        self.shapes_list.bind("<<ListboxSelect>>", self.on_shape_select)

        table_panel = tk.Frame(top_frame, width=320)
        table_panel.pack(side="right", fill="y", expand=True)
        table_panel.pack_propagate(False)

        self.table_heading_btn = tk.Label(
            table_panel,
            text=self._table_heading_text(),
            relief="raised", cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            bg="#d0d8e8", anchor="center")
        self.table_heading_btn.pack(fill="x", padx=2, pady=(2, 0))
        self.table_heading_btn.bind("<Button-1>", self.toggle_table_mode)

        scrollbar = ttk.Scrollbar(table_panel, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        self.xy_table = ttk.Treeview(
            table_panel,
            columns=("c0", "c1", "c2", "c3"),
            show="headings",
            yscrollcommand=scrollbar.set)
        self.xy_table.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.xy_table.yview)

        self._configure_table_columns()

        menubar  = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Load",  command=self.load)
        filemenu.add_command(label="Save",  command=self.save)
        filemenu.add_command(label="Save Selected Shape...",
                             command=self.save_selected_shape)
        filemenu.add_command(label="Load Shape (Add to Canvas)",
                             command=self.load_additional_shape_dialog)
        filemenu.add_separator()
        filemenu.add_command(label="Exit",  command=self.root.quit)
        menubar.add_cascade(label="File",   menu=filemenu)
        self.root.config(menu=menubar)

        for text, cmd in [
            ("Delete Point",     self.delete_point),
            ("Clear Trace",      self.clear_trace),
            ("New Shape",        self.new_shape),
            ("Save As...",       self.save),
            ("Save Selected",    self.save_selected_shape),
            ("Load Shape...",    self.load),
            ("Add Shape",        self.load_additional_shape_dialog),
            ("Join",             self.join),
            ("Machine Settings", self.open_machine_settings),
            ("Generate",         self.generate_gcode_file),
            ("Show NC Path",     self.toggle_nc_toolpath),
        ]:
            tk.Button(bottom_frame, text=text, command=cmd).pack(
                side="left", padx=5)

        self.canvas.bind("<Button-1>",        self.on_click)
        self.canvas.bind("<B1-Motion>",       self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Configure>",       self.on_canvas_resize)
        self.canvas.bind("<Button-1>",
                         lambda e: self.canvas.focus_set(), add="+")
        self.canvas.bind("<Button-3>",        self.on_right_click)
        self.canvas.bind("<MouseWheel>",      self.on_mousewheel)
        self.canvas.bind("<ButtonPress-2>",   self.start_pan)
        self.canvas.bind("<B2-Motion>",       self.do_pan)

        self.rotate_angle_entry.bind("<Return>", self.on_rotate_enter)

        self.xy_table.bind("<Double-1>", self.on_table_edit)
        self.xy_table.bind("<Up>",       self.on_table_nav)
        self.xy_table.bind("<Down>",     self.on_table_nav)
        self.xy_table.bind("<Left>",     self.on_table_nav)
        self.xy_table.bind("<Right>",    self.on_table_nav)

        self.root.bind("<MouseWheel>",     self.on_mousewheel)
        self.root.bind("<KeyPress-Up>",    self.on_key)
        self.root.bind("<KeyPress-Down>",  self.on_key)
        self.root.bind("<KeyPress-Left>",  self.on_key)
        self.root.bind("<KeyPress-Right>", self.on_key)
        self.root.bind("<KeyPress-r>",     lambda e: self.reset_view())

        self.trace_type_var.trace_add("write", self.on_trace_type_changed)

        self.refresh_shapes_list()
        self.refresh_xy_table()
        self.redraw()
        self.canvas.focus_set()

    # ============================================================
    # Y-axis flip helpers
    # ============================================================

    def _sx(self, wx):
        return wx * self.view_scale + self.view_offset_x

    def _sy(self, wy):
        return self.canvas_height - (wy * self.view_scale + self.view_offset_y)

    def _wx(self, sx):
        return (sx - self.view_offset_x) / self.view_scale

    def _wy(self, sy):
        return (self.canvas_height - sy - self.view_offset_y) / self.view_scale

    # ============================================================
    # Table mode toggle
    # ============================================================

    def _table_heading_text(self):
        if self.table_mode == self.MODE_POINTS:
            return "▶  Points  (click to show Traces)"
        else:
            return "▶  Traces  (click to show Points)"

    def _configure_table_columns(self):
        if self.table_mode == self.MODE_POINTS:
            self.xy_table.column("c0", width=45,  anchor="center",
                                  stretch=False)
            self.xy_table.column("c1", width=80,  anchor="center",
                                  stretch=False)
            self.xy_table.column("c2", width=80,  anchor="center",
                                  stretch=False)
            self.xy_table.column("c3", width=0,   anchor="center",
                                  stretch=False, minwidth=0)
            self.xy_table.heading("c0", text="Ref")
            self.xy_table.heading("c1", text="X")
            self.xy_table.heading("c2", text="Y")
            self.xy_table.heading("c3", text="")
        else:
            self.xy_table.column("c0", width=55,  anchor="center",
                                  stretch=False)
            self.xy_table.column("c1", width=60,  anchor="center",
                                  stretch=False)
            self.xy_table.column("c2", width=60,  anchor="center",
                                  stretch=False)
            self.xy_table.column("c3", width=75,  anchor="center",
                                  stretch=False)
            self.xy_table.heading("c0", text="Trace")
            self.xy_table.heading("c1", text="From")
            self.xy_table.heading("c2", text="To")
            self.xy_table.heading("c3", text="Apex")

    def toggle_table_mode(self, event=None):
        if self.table_mode == self.MODE_POINTS:
            self.table_mode = self.MODE_TRACES
        else:
            self.table_mode = self.MODE_POINTS
        self.table_heading_btn.config(text=self._table_heading_text())
        self._configure_table_columns()
        self.refresh_xy_table()

    # ============================================================
    # Shape management
    # ============================================================

    def new_shape(self):
        new_id     = f"shape_{len(self.state.shapes) + 1}"
        trace_kind = self.trace_type_var.get() or "R"

        shape = Shape(
            id=new_id,
            role="profile",
            trace=Trace(kind=trace_kind, points=[], closed=False)
        )
        self.state.shapes.append(shape)
        self.current_shape_index  = len(self.state.shapes) - 1
        self.selected_point_index = None
        self.dragging             = False

        self.refresh_shapes_list()
        self.shapes_list.selection_clear(0, "end")
        self.shapes_list.selection_set(self.current_shape_index)
        self.shapes_list.activate(self.current_shape_index)
        self.refresh_xy_table()
        self.redraw()
        return shape

    def get_current_shape(self) -> Optional[Shape]:
        if not self.state.shapes:
            return None
        if self.current_shape_index is None:
            return None
        if self.current_shape_index >= len(self.state.shapes):
            return None
        return self.state.shapes[self.current_shape_index]

    def ensure_trace(self, shape: Shape):
        if shape.trace is None:
            shape.trace = Trace(
                kind=self.trace_type_var.get() or "R",
                points=[], closed=False)
        return shape.trace

    def prune_empty_shapes(self):
        cleaned = []
        for s in self.state.shapes:
            if s is None:
                continue
            if s.trace is None:
                s.trace = Trace(kind="R", closed=False, points=[])
            if not hasattr(s.trace, "points") or s.trace.points is None:
                s.trace.points = []
            s.trace._sync_tr()
            cleaned.append(s)
        self.state.shapes = cleaned

    def normalize_shape_index(self):
        if not self.state.shapes:
            self.current_shape_index = 0
            return
        if self.current_shape_index is None:
            self.current_shape_index = 0
            return
        if self.current_shape_index >= len(self.state.shapes):
            self.current_shape_index = len(self.state.shapes) - 1

    def get_shape_centroid(self, shape):
        trace = shape.trace
        if trace is None or not trace.points:
            return None, None
        xs = [p.x for p in trace.points]
        ys = [p.y for p in trace.points]
        return sum(xs)/len(xs), sum(ys)/len(ys)

    def rotate_shape(self, shape, angle_degrees):
        cx, cy = self.get_shape_centroid(shape)
        if cx is None:
            return
        angle = math.radians(angle_degrees)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        for p in shape.trace.points:
            dx, dy = p.x - cx, p.y - cy
            p.x = cx + dx*cos_a - dy*sin_a
            p.y = cy + dx*sin_a + dy*cos_a

    def can_merge(self, shape1, shape2):
        if shape1.role != shape2.role:
            messagebox.showerror(
                "Merge Error",
                f"Cannot merge shapes with different roles:\n"
                f"{shape1.role} vs {shape2.role}")
            return False
        return True

    def validate_shapes_before_save(self):
        for shape in self.state.shapes:
            trace = shape.trace
            if trace is None:
                continue
            if shape.role == "hole" and trace.kind == "R":
                messagebox.showerror(
                    "Invalid Shape",
                    f"Shape {shape.id} is red but marked as a hole.")
                return False
        return True

    def find_nearest_point(self, wx, wy):
        hit_radius = 6 / self.view_scale
        nearest, best = None, None
        for s in self.state.shapes:
            if s.trace is None:
                continue
            for p in s.trace.points:
                d = abs(p.x - wx) + abs(p.y - wy)
                if d < hit_radius and (best is None or d < best):
                    best, nearest = d, p
        return nearest

    # ============================================================
    # JOIN
    # ============================================================

    def join(self):
        n = len(self.state.shapes)
        if n == 2:
            self.join_two_shapes()
        elif n == 1:
            self.close_single_shape()
        else:
            self.show_alert(
                "Join requires either 1 shape (close) or 2 shapes (merge).")

    def join_two_shapes(self):
        if len(self.state.shapes) != 2:
            self.show_alert("Join aborted: need exactly two shapes.")
            return
        self.merge_1(0, 1)
        self.refresh_xy_table()
        self.redraw()

    def close_single_shape(self):
        shape = self.state.shapes[0]
        pts   = shape.trace.points

        if len(pts) < 3:
            self.show_alert("Cannot close: need at least 3 points.")
            return

        if pts[1].x < pts[0].x:
            pts.reverse()
            shape.trace._sync_tr()

        first = pts[0]
        last  = pts[-1]
        if abs(first.x - last.x) < 1e-6 and abs(first.y - last.y) < 1e-6:
            pts.pop()
            shape.trace.tr.pop()

        shape.trace.closed = True
        shape.trace._sync_tr()

        self.selected_point_index = None
        self.dragging = False
        self.refresh_xy_table()
        self.redraw()
        self.show_alert("Shape closed.")

    def merge_1(self, a_idx, b_idx):
        shapeA = self.state.shapes[a_idx]
        shapeB = self.state.shapes[b_idx]
        ptsA   = shapeA.trace.points
        ptsB   = shapeB.trace.points

        candidates = [
            (iA, iB)
            for iA, pA in enumerate(ptsA)
            for iB, pB in enumerate(ptsB)
            if pA.x == pB.x and pA.y == pB.y
        ]

        if not candidates:
            self.show_alert(
                "Join aborted: shapes must share a coincident point.")
            return

        def is_endpoint(i, n):
            return i == 0 or i == n - 1

        chosen = next(
            ((iA, iB) for iA, iB in candidates
             if is_endpoint(iA, len(ptsA)) or is_endpoint(iB, len(ptsB))),
            candidates[0])

        iA, iB = chosen
        newA = ptsA if iA == len(ptsA) - 1 else list(reversed(ptsA))
        newB = ptsB if iB == 0             else list(reversed(ptsB))
        newB = newB[1:]

        unified = newA + newB
        unified.reverse()

        merged_trace = Trace(kind=shapeA.trace.kind, points=unified)
        self.state.shapes = [Shape(
            id=f"{shapeA.id}_{shapeB.id}",
            role=shapeA.role,
            trace=merged_trace
        )]
        self.current_shape_index = 0
        self.refresh_xy_table()
        self.redraw()
        self.show_alert("Join complete.")

    # ============================================================
    # Hit testing (world coords)
    # ============================================================

    def hit_test_shape_world(self, wx, wy, threshold=6):
        thr = threshold / self.view_scale
        for shape in self.state.shapes:
            trace = shape.trace
            if trace is None or len(trace.points) < 2:
                continue
            pts = trace.points
            for i in range(len(pts) - 1):
                d = point_to_segment_distance(
                    wx, wy,
                    pts[i].x, pts[i].y, pts[i+1].x, pts[i+1].y)
                if d <= thr:
                    return shape.id
        return None

    # ============================================================
    # Mouse events
    # ============================================================

    def on_click(self, event):
        wx = self._wx(event.x)
        wy = self._wy(event.y)

        if self.alert_visible:
            self.hide_alert()

        if not self.state.shapes:
            if self.trace_type_var.get() == "":
                self.show_alert(
                    "Select a shape type (R / B / S) before drawing.")
                return
            shape = self.new_shape()
        else:
            if self.current_shape_index is None:
                self.current_shape_index = len(self.state.shapes) - 1
            shape = self.state.shapes[self.current_shape_index]

        trace = shape.trace

        if event.state & 0x0001:
            s = self.get_current_shape()
            if s:
                self.rotate_shape(s, 10)
                self.redraw()
            return

        if trace is not None and trace.kind == "S":
            sub = getattr(trace, "sub", None)

            if sub == "circle":
                if len(trace.points) == 0:
                    trace.add_point(wx, wy)
                    pending = getattr(trace, "_pending_radius", None)
                    if pending is not None:
                        trace.tr[0] = pending
                    self.refresh_xy_table()
                    self.redraw()
                return

            if sub == "drill":
                if len(trace.points) == 0:
                    trace.add_point(wx, wy)
                    self.refresh_xy_table()
                    self.redraw()
                return

            if sub == "moulding":
                now = time.time()
                if now - self.moulding_click_time < 0.2:
                    self.moulding_click_time = now
                    return
                self.moulding_click_time = now
                trace.add_point(wx, wy)
                self.selected_point_index = len(trace.points) - 1
                self.refresh_xy_table()
                self.redraw()
                return

            return

        hit_radius = 6 / self.view_scale
        for s in self.state.shapes:
            t = s.trace
            if t is None:
                continue
            for i, p in enumerate(t.points):
                if abs(p.x - wx) < hit_radius and abs(p.y - wy) < hit_radius:
                    self.current_shape_index  = self.state.shapes.index(s)
                    self.last_mouse           = (wx, wy)
                    self.selected_point_index = i
                    self.dragging             = True
                    self.dragging_shape       = False
                    self.refresh_shapes_list()
                    self.update_xy_table()
                    self.redraw()
                    return

        shape_id = self.hit_test_shape_world(wx, wy)
        if shape_id is not None:
            self.dragging          = False
            self.dragging_shape    = True
            self.dragging_shape_id = shape_id
            self.shape_drag_start  = (wx, wy)
            s = next(s for s in self.state.shapes if s.id == shape_id)
            self.shape_original_points = [(p.x, p.y) for p in s.trace.points]
            return

        trace.add_point(wx, wy)
        self.selected_point_index = len(trace.points) - 1
        self.dragging             = False
        self.dragging_shape       = False
        self.last_mouse           = (wx, wy)
        self.refresh_xy_table()
        self.redraw()

    def on_right_click(self, event):
        shape = self.get_current_shape()
        if shape is None:
            return
        trace = shape.trace
        if trace is None or len(trace.points) < 3:
            return

        if trace.kind == "S" and getattr(trace, "sub", None) in (
                "circle", "drill", "bulge"):
            return

        first = trace.points[0]
        last  = trace.points[-1]
        if abs(first.x - last.x) < 1e-6 and abs(first.y - last.y) < 1e-6:
            trace.points.pop()
            trace.tr.pop()

        trace.closed = True
        trace._sync_tr()

        self.selected_point_index = None
        self.refresh_shapes_list()
        self.refresh_xy_table()
        self.redraw()

    def on_drag(self, event):
        wx = self._wx(event.x)
        wy = self._wy(event.y)

        if self.dragging and self.selected_point_index is not None:
            shape = self.get_current_shape()
            if shape is None:
                return
            trace = shape.trace
            if trace is None:
                return

            dx = wx - self.last_mouse[0]
            dy = wy - self.last_mouse[1]

            p = trace.points[self.selected_point_index]
            p.x += dx
            p.y += dy

            i = self.selected_point_index
            trace.set_tr(i, None)
            if i > 0:
                trace.set_tr(i - 1, None)
            elif trace.closed:
                trace.set_tr(len(trace.points) - 1, None)

            self.last_mouse = (wx, wy)
            self.refresh_xy_table()
            self.redraw()
            return

        if self.dragging_shape and self.dragging_shape_id is not None:
            s = next((s for s in self.state.shapes
                      if s.id == self.dragging_shape_id), None)
            if s is None:
                return
            dx = wx - self.shape_drag_start[0]
            dy = wy - self.shape_drag_start[1]
            for (ox, oy), p in zip(self.shape_original_points, s.trace.points):
                p.x = ox + dx
                p.y = oy + dy
            self.refresh_xy_table()
            self.redraw()
            return

        if self.dragging_center:
            dx = wx - self.last_mouse[0]
            dy = wy - self.last_mouse[1]
            self.last_mouse = (wx, wy)
            self.on_drag_center(dx, dy)

    def on_drag_center(self, dx, dy):
        pass

    def on_release(self, event):
        self.dragging              = False
        self.dragging_shape        = False
        self.dragging_shape_id     = None
        self.shape_drag_start      = None
        self.shape_original_points = None
        self.dragging_center       = False

    def on_key(self, event):
        mapping = {
            "Up":    (0,  1),
            "Down":  (0, -1),
            "Left":  (-1, 0),
            "Right": (1,  0),
        }
        if event.keysym in mapping:
            self.nudge_selected_point(*mapping[event.keysym])
        return "break"

    def nudge_selected_point(self, dx, dy):
        shape = self.get_current_shape()
        if not shape or self.selected_point_index is None:
            return
        trace = shape.trace
        if trace is None or self.selected_point_index >= len(trace.points):
            return
        i = self.selected_point_index
        p = trace.points[i]
        p.x += dx
        p.y += dy
        trace.set_tr(i, None)
        if i > 0:
            trace.set_tr(i - 1, None)
        elif trace.closed:
            trace.set_tr(len(trace.points) - 1, None)
        self.refresh_xy_table()
        self.redraw()

    def on_mousewheel(self, event):
        factor = 1.1 if event.delta > 0 else 0.9
        wx = self._wx(event.x)
        wy = self._wy(event.y)
        self.view_scale *= factor
        self.view_offset_x += event.x - \
            (wx * self.view_scale + self.view_offset_x)
        self.view_offset_y += (self.canvas_height - event.y) - \
                              (wy * self.view_scale + self.view_offset_y)
        self.redraw()

    def start_pan(self, event):
        self.pan_start_x = event.x
        self.pan_start_y = event.y

    def do_pan(self, event):
        self.view_offset_x += event.x - self.pan_start_x
        self.view_offset_y -= event.y - self.pan_start_y
        self.pan_start_x    = event.x
        self.pan_start_y    = event.y
        self.redraw()

    def reset_view(self):
        self.view_scale    = 1.0
        self.view_offset_x = 0.0
        self.view_offset_y = 0.0
        self.redraw()

    def on_canvas_resize(self, event):
        self.canvas_width  = event.width
        self.canvas_height = event.height
        self.redraw()

    # ============================================================
    # Table events
    # ============================================================

    def on_table_edit(self, event):
        row_id = self.xy_table.identify_row(event.y)
        col_id = self.xy_table.identify_column(event.x)

        if not row_id:
            return

        self.xy_table.selection_set(row_id)

        shape = self.get_current_shape()
        if shape is None:
            return
        trace = shape.trace

        if self.table_mode == self.MODE_TRACES:
            if col_id != "#4":
                return
            is_moulding = (trace.kind == "S" and
                           getattr(trace, "sub", None) == "moulding")
            if not trace.closed and not is_moulding:
                self.show_alert(
                    "Apex can only be set on a closed shape or moulding.")
                return
            seg_index = self.xy_table.index(row_id)
            old_value = self.xy_table.set(row_id, col_id)
            self._inline_edit(row_id, col_id, old_value,
                              lambda v: self._commit_radius(
                                  trace, seg_index, v))
            return

        if col_id not in ("#2", "#3"):
            return

        pts         = trace.points
        table_index = self.xy_table.index(row_id)
        point_index = table_index

        if point_index >= len(pts):
            return

        old_value = self.xy_table.set(row_id, col_id)
        self._inline_edit(row_id, col_id, old_value,
                          lambda v: self._commit_xy(
                              trace, point_index, col_id, v))

    def _inline_edit(self, row_id, col_id, old_value, commit_fn):
        entry = tk.Entry(self.xy_table)
        entry.insert(0, old_value)
        entry.select_range(0, "end")
        entry.focus()

        bbox = self.xy_table.bbox(row_id, col_id)
        if not bbox:
            entry.destroy()
            return
        entry.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])

        def commit(ev=None):
            commit_fn(entry.get().strip())
            entry.destroy()

        entry.bind("<Return>",   commit)
        entry.bind("<Escape>",   lambda e: entry.destroy())
        entry.bind("<FocusOut>", commit)

    def _commit_radius(self, trace, seg_index, raw):
        if raw == "" or raw.lower() == "none":
            trace.set_tr(seg_index, None)
            self.refresh_xy_table()
            self.redraw()
            return

        try:
            s = float(raw)
        except ValueError:
            self.show_alert("Apex height must be a number.")
            return

        if s == 0:
            self.show_alert(
                "Apex height must not be zero. Segment kept straight.")
            trace.set_tr(seg_index, None)
            self.refresh_xy_table()
            self.redraw()
            return

        pts = trace.points
        n   = len(pts)
        if seg_index < n - 1:
            p1, p2 = pts[seg_index], pts[seg_index + 1]
        elif trace.closed and n > 1:
            p1, p2 = pts[n - 1], pts[0]
        else:
            p1, p2 = None, None

        if p1 and p2 and chord_length(p1, p2) < 1e-9:
            self.show_alert("Chord length is zero — segment kept straight.")
            trace.set_tr(seg_index, None)
            self.refresh_xy_table()
            self.redraw()
            return

        trace.set_tr(seg_index, s)
        self.refresh_xy_table()
        self.redraw()

    def _commit_xy(self, trace, point_index, col_id, raw):
        try:
            new_float = float(raw)
        except ValueError:
            return
        p = trace.points[point_index]
        if col_id == "#2":
            p.x = new_float
        else:
            p.y = new_float

        trace.set_tr(point_index, None)
        if point_index > 0:
            trace.set_tr(point_index - 1, None)
        elif trace.closed:
            trace.set_tr(len(trace.points) - 1, None)

        self.selected_point_index = point_index
        self.refresh_xy_table()
        self.redraw()

    def on_table_nav(self, event):
        sel = self.xy_table.selection()
        if not sel:
            return
        rows    = self.xy_table.get_children()
        idx     = list(rows).index(sel[0])
        max_idx = len(rows) - 1

        if   event.keysym == "Up"   and idx > 0:
            idx -= 1
        elif event.keysym == "Down" and idx < max_idx:
            idx += 1

        new_row = rows[idx]
        self.xy_table.selection_set(new_row)
        self.xy_table.focus(new_row)
        self.xy_table.see(new_row)

    # ============================================================
    # Shapes list
    # ============================================================

    def refresh_shapes_list(self):
        self.shapes_list.delete(0, "end")
        for shape in self.state.shapes:
            role_tag = f" [{shape.role}]" if shape.role else ""
            wf_tag   = ""
            if getattr(shape, "workflow", None):
                wf_tag = f" <{shape.workflow}>"
            self.shapes_list.insert(
                "end", f"{shape.id}{role_tag}{wf_tag}")

    def on_shape_select(self, event):
        sel = self.shapes_list.curselection()
        if not sel:
            return
        index = sel[0]
        self.current_shape_index = index
        shape = self.state.shapes[index]

        if shape.trace is not None:
            self._suppress_type_change = True
            self.trace_type_var.set(shape.trace.kind)
            self._suppress_type_change = False

        self.update_rotate_angle_state()
        self.update_arc_angle_state()
        self.moulding_click_time = 0.0
        self.refresh_xy_table()
        self.redraw()

    # ============================================================
    # Type-change callback
    # ============================================================

    def on_trace_type_changed(self, *args):
        if self._suppress_type_change:
            return
        new_kind = self.trace_type_var.get()
        if new_kind == "":
            self.angle_entry.config(state="disabled")
            return
        self.angle_entry.config(state="disabled")
        SubTypeDialog(self.root, new_kind, callback=self._apply_subtype)

    def _apply_subtype(self, kind, sub, radius):
        if kind is None:
            self._suppress_type_change = True
            self.trace_type_var.set("")
            self._suppress_type_change = False
            return

        if kind in ("R", "B"):
            role = sub
        else:
            role = "special"

        if kind == "S" and sub == "circle":
            CircleWorkflowDialog(
                self.root,
                callback=lambda wf, off: self._finalise_circle(
                    wf, off, radius))
            self._suppress_type_change = True
            self.trace_type_var.set("")
            self._suppress_type_change = False
            return

        if kind == "S" and sub == "bulge":
            BulgeWorkflowDialog(
                self.root,
                callback=lambda ok: self._finalise_bulge(ok))
            self._suppress_type_change = True
            self.trace_type_var.set("")
            self._suppress_type_change = False
            return

        trace = Trace(kind=kind, points=[], closed=False,
                      sub=(sub if kind == "S" else None))
        self.moulding_click_time = 0.0

        new_id = f"shape_{len(self.state.shapes) + 1}"
        shape  = Shape(id=new_id, role=role, trace=trace)
        self.state.shapes.append(shape)
        self.current_shape_index  = len(self.state.shapes) - 1
        self.selected_point_index = None

        self.refresh_shapes_list()
        self.shapes_list.selection_clear(0, "end")
        self.shapes_list.selection_set(self.current_shape_index)
        self.shapes_list.activate(self.current_shape_index)
        self.refresh_xy_table()
        self.redraw()

        hints = {
            ("R", "profile"):  "R-Profile ready — click to add points.  Right-click to close.",
            ("R", "pocket"):   "R-Pocket ready — click to add points.  Right-click to close.",
            ("B", "profile"):  "B-Profile ready — click to add points.  Right-click to close.",
            ("B", "pocket"):   "B-Pocket ready — click to add points.  Right-click to close.",
            ("S", "drill"):    "Click canvas ONCE to place drill point.",
            ("S", "moulding"): "Click canvas to add moulding profile points.",
        }
        self.show_alert(hints.get((kind, sub), "Shape ready."))

        self._suppress_type_change = True
        self.trace_type_var.set("")
        self._suppress_type_change = False

    # ============================================================
    # Circle / Bulge finalisation
    # ============================================================

    def _finalise_circle(self, wf_key, signed_offset, radius):
        if wf_key is None:
            self.show_alert("Circle workflow cancelled.")
            return

        trace = Trace(kind="S", points=[], closed=False, sub="circle")
        if radius is not None:
            trace._pending_radius = radius

        new_id = f"shape_{len(self.state.shapes) + 1}"
        shape  = Shape(id=new_id, role="special", trace=trace)
        shape.workflow = wf_key
        shape.offset   = signed_offset

        self.state.shapes.append(shape)
        self.current_shape_index  = len(self.state.shapes) - 1
        self.selected_point_index = None
        self.moulding_click_time  = 0.0

        self.refresh_shapes_list()
        self.shapes_list.selection_clear(0, "end")
        self.shapes_list.selection_set(self.current_shape_index)
        self.shapes_list.activate(self.current_shape_index)
        self.refresh_xy_table()
        self.redraw()

        if wf_key in ("edge_plus", "edge_minus"):
            self._offer_diagnostic_saves(shape)
        else:
            sign_str = f"{signed_offset:+.3f} mm"
            self.show_alert(
                f"Circle ({wf_key.replace('_', ' ')})  offset={sign_str}"
                "  — click canvas once to place centre.")

    def _finalise_bulge(self, confirmed):
        if not confirmed:
            self.show_alert("Bulge workflow cancelled.")
            return

        trace  = Trace(kind="S", points=[], closed=False, sub="bulge")
        new_id = f"shape_{len(self.state.shapes) + 1}"
        shape  = Shape(id=new_id, role="special", trace=trace)
        shape.workflow = "bulge"
        shape.offset   = None

        self.state.shapes.append(shape)
        self.current_shape_index  = len(self.state.shapes) - 1
        self.selected_point_index = None
        self.moulding_click_time  = 0.0

        self.refresh_shapes_list()
        self.shapes_list.selection_clear(0, "end")
        self.shapes_list.selection_set(self.current_shape_index)
        self.shapes_list.activate(self.current_shape_index)
        self.refresh_xy_table()
        self.redraw()
        self._offer_diagnostic_saves(shape)

    # ============================================================
    # Diagnostic saves
    # ============================================================

    def _offer_diagnostic_saves(self, shape):
        wf = getattr(shape, "workflow", "")
        ans = messagebox.askyesno(
            "Save Diagnostic Files?",
            f"Workflow:  {wf.replace('_', ' ')}\n\n"
            "This workflow produces TWO .nc files and requires\n"
            "a TOOL CHANGE between passes.\n\n"
            "Save diagnostic files now?\n"
            "  •  shapexA.json  (Pass 1 — profile / waste)\n"
            "  •  shapexB.json  (Pass 2 — edge / moulding)\n\n"
            "Recommended before G-code generation."
        )
        if not ans:
            self.show_alert(
                f"{wf.replace('_', ' ')} ready — "
                "place geometry then use Save Selected for each pass.")
            return
        self._write_diagnostic_json(shape)

    def _write_diagnostic_json(self, shape):
        t_data = self._serialise_trace(shape.trace) \
                 if shape.trace is not None else None
        off = getattr(shape, "offset",   None)
        wf  = getattr(shape, "workflow", None)

        out_a = {"shapes": [{"id":       shape.id + "_A",
                              "role":     shape.role,
                              "workflow": wf,
                              "offset":   off,
                              "pass":     "A",
                              "trace":    t_data}]}
        out_b = {"shapes": [{"id":       shape.id + "_B",
                              "role":     shape.role,
                              "workflow": wf,
                              "offset":   off,
                              "pass":     "B",
                              "trace":    t_data}]}

        saved  = []
        errors = []
        for filename, payload in [("shapexA.json", out_a),
                                   ("shapexB.json", out_b)]:
            try:
                with open(filename, "w") as f:
                    json.dump(payload, f, indent=2)
                saved.append(filename)
            except Exception as exc:
                errors.append(f"{filename}: {exc}")

        if errors:
            messagebox.showerror("Diagnostic Save Error",
                                 "Could not write:\n" + "\n".join(errors))
        else:
            self.show_alert(
                f"Diagnostic files saved: {', '.join(saved)}  "
                "— place geometry then generate G-code.")

    # ============================================================
    # Misc callbacks
    # ============================================================

    def on_rotate_enter(self, event):
        shape = self.get_current_shape()
        if not shape:
            return
        try:
            angle = float(self.rotate_angle_var.get())
        except ValueError:
            return
        self.rotate_shape(shape, angle)
        self.rotate_angle_var.set(0)
        self.redraw()

    def update_arc_angle_state(self):
        self.angle_entry.config(state="disabled")

    def update_rotate_angle_state(self):
        state = "normal" if self.get_current_shape() is not None \
                else "disabled"
        self.rotate_angle_entry.config(state=state)

    # ============================================================
    # Point editing
    # ============================================================

    def delete_point(self):
        shape = self.get_current_shape()
        if shape is None:
            return
        trace = shape.trace
        if trace is None or self.selected_point_index is None:
            return

        idx = self.selected_point_index
        if 0 <= idx < len(trace.points):
            trace.remove_point(idx)

        if not trace.points:
            trace.closed = False
            self.selected_point_index = None
        else:
            self.selected_point_index = min(idx, len(trace.points) - 1)

        self.prune_empty_shapes()
        self.refresh_shapes_list()
        self.refresh_xy_table()
        self.redraw()

    def delete_selected_point(self):
        self.delete_point()

    def clear_trace(self):
        shape = self.get_current_shape()
        if shape is None:
            return

        shape.trace.points = []
        shape.trace.tr     = []
        shape.trace.closed = False
        self.selected_point_index = None
        self.dragging             = False

        self.state.shapes.pop(self.current_shape_index)
        self.normalize_shape_index()

        if not self.state.shapes:
            self.current_shape_index = None

        self.refresh_shapes_list()
        self.refresh_xy_table()
        self.redraw()

    # ============================================================
    # Load / Save
    # ============================================================

    def _serialise_trace(self, trace):
        return {
            "id":     trace.id,
            "kind":   trace.kind,
            "sub":    getattr(trace, "sub", None),
            "closed": trace.closed,
            "points": [[p.x, p.y] for p in trace.points],
            "tr":     [r for r in trace.tr],
        }

    def _deserialise_trace(self, t):
        pts = [Point(item[0], item[1]) for item in t.get("points", [])]
        tr  = Trace(
            kind   = t.get("kind",   "R"),
            closed = t.get("closed", False),
            points = pts,
            tid    = t.get("id"),
            sub    = t.get("sub",    None))
        saved_tr = t.get("tr", None)
        if saved_tr is not None and len(saved_tr) == len(pts):
            tr.tr = list(saved_tr)
        else:
            tr.tr = [None] * len(pts)
        return tr

    def load(self):
        path = filedialog.askopenfilename(
            title="Load Shape File",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")])
        if not path:
            return

        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load Error", f"Could not load file:\n{e}")
            return

        self.state.shapes.clear()
        stem = os.path.splitext(os.path.basename(path))[0]

        for i, s in enumerate(data.get("shapes", [])):
            t     = s.get("trace")
            trace = self._deserialise_trace(t) if t else \
                    Trace(kind="R", closed=False, points=[])
            shape_id = stem if i == 0 else f"{stem}_{i+1}"
            shape = Shape(id=shape_id,
                          role=s.get("role", "profile"),
                          trace=trace)
            shape.workflow = s.get("workflow", None)
            shape.offset   = s.get("offset",   None)
            self.state.shapes.append(shape)

        self.current_shape_index  = 0
        self.selected_point_index = None
        self.current_shape_path   = path
        self.refresh_shapes_list()
        self.refresh_xy_table()
        self.redraw()

    def save(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")])
        if not path:
            return

        out = {"shapes": []}
        for s in self.state.shapes:
            t = None if s.trace is None else self._serialise_trace(s.trace)
            out["shapes"].append({
                "id":       s.id,
                "role":     s.role,
                "workflow": getattr(s, "workflow", None),
                "offset":   getattr(s, "offset",   None),
                "trace":    t,
            })

        with open(path, "w") as f:
            json.dump(out, f, indent=2)
        self.current_shape_path = path

    def save_selected_shape(self):
        shape = self.get_current_shape()
        if shape is None:
            messagebox.showwarning(
                "No Selection",
                "No shape is currently selected.\n"
                "Click a shape in the list first.")
            return

        path = filedialog.asksaveasfilename(
            title="Save Selected Shape",
            initialfile=f"{shape.id}.json",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")])
        if not path:
            return

        t   = None if shape.trace is None \
              else self._serialise_trace(shape.trace)
        out = {"shapes": [{"id":       shape.id,
                            "role":     shape.role,
                            "workflow": getattr(shape, "workflow", None),
                            "offset":   getattr(shape, "offset",   None),
                            "trace":    t}]}

        with open(path, "w") as f:
            json.dump(out, f, indent=2)
        self.show_alert(
            f"Saved selected shape → {os.path.basename(path)}")

    def save_job(self):
        out = {"shapes": []}
        for s in self.state.shapes:
            t = None if s.trace is None else self._serialise_trace(s.trace)
            out["shapes"].append({
                "id":       s.id,
                "role":     s.role,
                "workflow": getattr(s, "workflow", None),
                "offset":   getattr(s, "offset",   None),
                "trace":    t,
            })
        with open("job.json", "w") as f:
            json.dump(out, f, indent=2)

    def load_additional_shape_dialog(self):
        path = filedialog.askopenfilename(
            filetypes=[("Shape JSON", "*.json")])
        if path:
            self.load_additional_shape(path)

    def load_additional_shape(self, path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load Error", f"Could not load file:\n{e}")
            return

        stem         = os.path.splitext(os.path.basename(path))[0]
        existing_ids = {s.id for s in self.state.shapes}

        for i, s in enumerate(data.get("shapes", [])):
            t     = s.get("trace")
            trace = self._deserialise_trace(t) if t else None

            candidate = stem if i == 0 else f"{stem}_{i+1}"
            suffix    = 1
            unique    = candidate
            while unique in existing_ids:
                suffix += 1
                unique  = f"{candidate}_{suffix}"

            shape = Shape(id=unique,
                          role=s.get("role", "profile"),
                          trace=trace)
            shape.workflow = s.get("workflow", None)
            shape.offset   = s.get("offset",   None)
            existing_ids.add(unique)
            self.state.shapes.append(shape)

        self.normalize_shape_index()
        self.refresh_shapes_list()
        self.refresh_xy_table()
        self.redraw()

    # ============================================================
    # Machine settings / G-code
    # ============================================================

    def open_machine_settings(self):
        shape_path = getattr(self, "current_shape_path", None)
        if not shape_path or not os.path.exists(shape_path):
            messagebox.showerror(
                "Shape Missing",
                "Save the shape before entering machine settings.")
            return
        MachineSettingsPopup(self.root, shape_path)

    def generate_gcode_file(self):
        try:
            import generator
        except ImportError:
            messagebox.showerror("Missing Module", "generator.py not found.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Save G-code As",
            defaultextension=".nc",
            filetypes=[("NC Files", "*.nc"), ("All Files", "*.*")])
        if not out_path:
            return

        try:
            model = generator.load_shapex()
            rings = generator.build_toolpaths(model)
            P     = model["params"]
            gcode = generator.generate_gcode(
                rings,
                target_depth = P["target_depth"],
                num_passes   = P["num_passes"],
                feed_xy      = P["feed_xy"],
                feed_z       = P["feed_z"],
                safe         = P["safe"],
                spindle      = P["spindle"],
                emit_spindle = P["emit_spindle"],
                stepover_mm  = P["stepover"],
                tow          = P["tow"],
                tool_id      = P["tool_id"])

            with open(out_path, "w") as f:
                f.write(gcode)
            self.show_alert(
                f"G-code generated → {os.path.basename(out_path)}")
        except Exception as e:
            messagebox.showerror("Generator Error", str(e))

    # ============================================================
    # NC Toolpath Visualization
    # ============================================================

    def toggle_nc_toolpath(self):
        """
        Toggle the NC toolpath overlay on/off.
        If no .nc file has been loaded yet, prompt the user to pick one.
        """
        if not self.show_toolpath:
            if not self.toolpath_points:
                self.load_nc_file()
                if not self.toolpath_points:
                    return
            self.show_toolpath = True
            self.show_alert(
                "NC toolpath overlay ON  —  click 'Show NC Path' to hide.")
        else:
            self.show_toolpath = False
            self.show_alert("NC toolpath overlay OFF.")
        self.redraw()

    def load_nc_file(self):
        """
        Ask the user to pick a .nc / G-code file, parse it, and
        store the resulting move list in self.toolpath_points.
        Each entry is (x_mm, y_mm, is_rapid) where is_rapid is
        True for G0 moves and False for G1/G2/G3 cutting moves.
        """
        path = filedialog.askopenfilename(
            title="Load NC / G-code File",
            filetypes=[("NC Files",     "*.nc"),
                       ("G-code Files", "*.gcode *.nc *.tap"),
                       ("All Files",    "*.*")])
        if not path:
            return

        try:
            points = self._parse_nc_file(path)
        except Exception as e:
            messagebox.showerror("NC Parse Error",
                                 f"Could not parse NC file:\n{e}")
            return

        if not points:
            messagebox.showwarning(
                "NC File Empty",
                "No G0/G1/G2/G3 moves found in the file.")
            return

        self.toolpath_points = points
        self.show_alert(
            f"NC file loaded: {os.path.basename(path)}  "
            f"({len(points)} moves)")

    def _parse_nc_file(self, path):
        """
        Minimal G-code parser.
        Handles G0, G1 (linear), G2, G3 (arc) moves.
        Returns a list of (x, y, is_rapid) tuples representing
        the tool centre path projected onto the XY plane.
        Arc moves (G2/G3) are approximated as polylines.
        """
        points   = []
        cx = cy  = 0.0
        modal    = 0        # 0=rapid, 1=feed, 2=cw arc, 3=ccw arc

        token_re = re.compile(r'([A-Za-z])([-+]?\d*\.?\d+)')

        def get_val(tokens, letter, default=None):
            return tokens.get(letter.upper(), default)

        with open(path, "r", errors="replace") as f:
            for raw_line in f:
                line = raw_line.split(";")[0].split(
                    "(")[0].strip().upper()
                if not line:
                    continue

                tokens = {m.group(1): float(m.group(2))
                          for m in token_re.finditer(line)}

                if "G" in tokens:
                    g = int(tokens["G"])
                    if g in (0, 1, 2, 3):
                        modal = g

                has_xy = "X" in tokens or "Y" in tokens
                if not has_xy:
                    continue

                nx = get_val(tokens, "X", cx)
                ny = get_val(tokens, "Y", cy)

                if modal in (0, 1):
                    is_rapid = (modal == 0)
                    points.append((nx, ny, is_rapid))
                    cx, cy = nx, ny

                elif modal in (2, 3):
                    i_off    = get_val(tokens, "I", 0.0)
                    j_off    = get_val(tokens, "J", 0.0)
                    arc_pts  = self._arc_move_points(
                        cx, cy, nx, ny, i_off, j_off,
                        clockwise=(modal == 2))
                    for px, py in arc_pts:
                        points.append((px, py, False))
                    cx, cy = nx, ny

        return points

    def _arc_move_points(self, x1, y1, x2, y2,
                         i_off, j_off, clockwise, steps=24):
        """
        Convert a G2/G3 arc move to a list of (x, y) polyline points.
        Centre is at (x1+i_off, y1+j_off).
        """
        ocx = x1 + i_off
        ocy = y1 + j_off

        r       = math.sqrt((x1 - ocx)**2 + (y1 - ocy)**2)
        a_start = math.atan2(y1 - ocy, x1 - ocx)
        a_end   = math.atan2(y2 - ocy, x2 - ocx)

        if clockwise:
            if a_end >= a_start:
                a_end -= 2 * math.pi
        else:
            if a_end <= a_start:
                a_end += 2 * math.pi

        pts = []
        for i in range(1, steps + 1):
            t = i / steps
            a = a_start + t * (a_end - a_start)
            pts.append((ocx + r * math.cos(a),
                        ocy + r * math.sin(a)))
        return pts

    def draw_nc_toolpath(self):
        """
        Draw the loaded NC toolpath onto the canvas.
        Rapid moves  (G0) → thin red dashed line
        Feed moves   (G1/G2/G3) → solid green line  width=2
        Start marker → blue circle
        End marker   → red filled circle
        """
        pts = self.toolpath_points
        if not pts:
            return

        # Start marker
        sx0 = self._sx(pts[0][0])
        sy0 = self._sy(pts[0][1])
        self.canvas.create_oval(sx0-6, sy0-6, sx0+6, sy0+6,
                                outline="blue", width=2,
                                tags="nc_toolpath")
        self.canvas.create_text(sx0 + 8, sy0,
                                text="START", anchor="w",
                                fill="blue",
                                font=("Segoe UI", 7),
                                tags="nc_toolpath")

        prev_x, prev_y, _ = pts[0]
        for (nx, ny, is_rapid) in pts[1:]:
            sx1 = self._sx(prev_x)
            sy1 = self._sy(prev_y)
            sx2 = self._sx(nx)
            sy2 = self._sy(ny)

            if is_rapid:
                self.canvas.create_line(
                    sx1, sy1, sx2, sy2,
                    fill="red", width=1, dash=(4, 4),
                    tags="nc_toolpath")
            else:
                self.canvas.create_line(
                    sx1, sy1, sx2, sy2,
                    fill="#00aa00", width=2,
                    tags="nc_toolpath")

            prev_x, prev_y = nx, ny

        # End marker
        sxe = self._sx(prev_x)
        sye = self._sy(prev_y)
        self.canvas.create_oval(sxe-5, sye-5, sxe+5, sye+5,
                                fill="red",
                                tags="nc_toolpath")
        self.canvas.create_text(sxe + 8, sye,
                                text="END", anchor="w",
                                fill="red",
                                font=("Segoe UI", 7),
                                tags="nc_toolpath")

    def draw_nc_legend(self):
        """
        Draw a small legend in the top-left corner explaining
        the NC toolpath colours.
        """
        x0, y0 = 12, 12
        self.canvas.create_rectangle(
            x0, y0, x0+150, y0+50,
            fill="#ffffff", outline="#aaaaaa",
            tags="nc_toolpath")
        self.canvas.create_line(
            x0+6,  y0+16, x0+26, y0+16,
            fill="#00aa00", width=2,
            tags="nc_toolpath")
        self.canvas.create_text(
            x0+30, y0+16,
            text="Feed move (G1/G2/G3)",
            anchor="w", font=("Segoe UI", 7),
            tags="nc_toolpath")
        self.canvas.create_line(
            x0+6,  y0+34, x0+26, y0+34,
            fill="red", width=1, dash=(4, 4),
            tags="nc_toolpath")
        self.canvas.create_text(
            x0+30, y0+34,
            text="Rapid move (G0)",
            anchor="w", font=("Segoe UI", 7),
            tags="nc_toolpath")

    # ============================================================
    # Drawing helpers
    # ============================================================

    def draw_grid(self):
        spacing = 10
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()

        wx0 = self._wx(0)
        wx1 = self._wx(w)
        x   = int(wx0 // spacing) * spacing
        while x <= wx1:
            sx = self._sx(x)
            self.canvas.create_line(sx, 0, sx, h,
                                    fill="#e0e0e0", tags="grid")
            x += spacing

        wy_bottom = self._wy(h)
        wy_top    = self._wy(0)
        y         = int(wy_bottom // spacing) * spacing
        while y <= wy_top:
            sy = self._sy(y)
            self.canvas.create_line(0, sy, w, sy,
                                    fill="#e0e0e0", tags="grid")
            y += spacing

    def draw_alert(self, canvas):
        if not self.alert_visible:
            return
        x0 = 10
        y0 = self.canvas_height - 30
        x1 = self.canvas_width  - 10
        y1 = self.canvas_height - 10
        canvas.create_rectangle(x0, y0, x1, y1,
                                 fill="#ffe9a8", outline="#d4b45f")
        canvas.create_text(x0 + 10, y0 + 10,
                           text=self.alert_message,
                           anchor="w", fill="#5a4a1f",
                           font=("Segoe UI", 10, "bold"))

    def show_alert(self, message):
        self.alert_message = message
        self.alert_visible = True
        self.redraw()

    def hide_alert(self):
        self.alert_visible = False
        self.redraw()

    def _draw_trace_segments(self, pts, trace, color,
                              closed=False, dash=None):
        n = len(pts)
        if n < 2:
            return

        seg_indices = list(range(n - 1))
        if closed and n > 1:
            seg_indices.append(n - 1)

        for seg in seg_indices:
            p1 = pts[seg]
            p2 = pts[(seg + 1) % n]
            r  = trace.get_tr(seg)

            if r is not None:
                world_pts  = arc_points(p1.x, p1.y, p2.x, p2.y, r)
                screen_pts = []
                for wx, wy in world_pts:
                    screen_pts.extend([self._sx(wx), self._sy(wy)])
                if len(screen_pts) >= 4:
                    kwargs = dict(fill=color, width=2, smooth=False)
                    if dash:
                        kwargs["dash"] = dash
                    self.canvas.create_line(*screen_pts, **kwargs)
            else:
                kwargs = dict(fill=color, width=1)
                if dash:
                    kwargs["dash"] = dash
                self.canvas.create_line(
                    self._sx(p1.x), self._sy(p1.y),
                    self._sx(p2.x), self._sy(p2.y),
                    **kwargs)

    # ============================================================
    # XY / Trace table population
    # ============================================================

    def refresh_xy_table(self):
        self.xy_table.delete(*self.xy_table.get_children())

        shape = self.get_current_shape()
        if not shape or not shape.trace:
            return

        trace = shape.trace
        trace._sync_tr()

        if trace.kind == "S":
            sub = getattr(trace, "sub", None)
            if sub == "moulding" and self.table_mode == self.MODE_TRACES:
                self._populate_trace_table(trace)
                return
            self._populate_special_table(trace, shape)
            return

        if self.table_mode == self.MODE_POINTS:
            self._populate_point_table(trace)
        else:
            self._populate_trace_table(trace)

    def _populate_special_table(self, trace, shape=None):
        sub = getattr(trace, "sub", "?")

        if sub == "circle":
            if trace.points:
                p = trace.points[0]
                r = trace.get_tr(0)
            else:
                p = None
                r = getattr(trace, "_pending_radius", None)

            r_display = round(abs(r), 3) if r is not None else "?"
            x_display = round(p.x, 3)   if p else "—"
            y_display = round(p.y, 3)   if p else "—"

            self.xy_table.insert("", "end", iid="0",
                values=("Centre", x_display, y_display, r_display))

            if shape is not None:
                wf      = getattr(shape, "workflow", None) or "—"
                off     = getattr(shape, "offset",   None)
                off_str = f"{off:+.3f}" if off is not None else "—"
                self.xy_table.insert("", "end", iid="wf",
                    values=("Workflow",
                            wf.replace("_", " "),
                            "Offset",
                            off_str))

        elif sub == "drill":
            if trace.points:
                p = trace.points[0]
                self.xy_table.insert("", "end", iid="0",
                    values=("Drill",
                            round(p.x, 3), round(p.y, 3), "—"))
            else:
                self.xy_table.insert("", "end", iid="0",
                    values=("Drill", "—", "—", "—"))

        elif sub == "bulge":
            for i, p in enumerate(trace.points):
                self.xy_table.insert("", "end", iid=str(i),
                    values=(f"P{i}",
                            round(p.x, 3), round(p.y, 3), "bulge"))

        elif sub == "moulding":
            for i, p in enumerate(trace.points):
                self.xy_table.insert("", "end", iid=str(i),
                    values=(f"P{i}",
                            round(p.x, 3), round(p.y, 3), ""))

    def _populate_point_table(self, trace):
        for i, p in enumerate(trace.points):
            self.xy_table.insert(
                "", "end", iid=str(i),
                values=(f"P{i}", round(p.x, 3), round(p.y, 3), ""))

    def _populate_trace_table(self, trace):
        pts = trace.points
        n   = len(pts)
        if n < 2:
            return

        for seg in range(trace.num_segments):
            p_from = seg
            p_to   = (seg + 1) % n
            r      = trace.get_tr(seg)

            if r is not None:
                abs_r = abs(r)
                p1    = pts[p_from]
                p2    = pts[p_to]
                c     = chord_length(p1, p2)
                h     = c / 2.0
                if abs_r >= h:
                    sagitta  = abs_r - math.sqrt(
                        abs_r*abs_r - h*h)
                    apex_str = str(round(sagitta, 4))
                else:
                    apex_str = ""
            else:
                apex_str = ""

            tag = "open_seg" if not trace.closed else ""
            self.xy_table.insert(
                "", "end", iid=str(seg),
                values=(f"tr{seg}",
                        f"P{p_from}", f"P{p_to}", apex_str),
                tags=(tag,))

        self.xy_table.tag_configure(
            "open_seg", foreground="#999999")

    def update_xy_table(self):
        self.refresh_xy_table()
        if self.selected_point_index is None:
            return
        rows = self.xy_table.get_children()
        if not rows:
            return
        idx = self.selected_point_index
        if 0 <= idx < len(rows):
            row_id = rows[idx]
            self.xy_table.selection_set(row_id)
            self.xy_table.see(row_id)

    # ============================================================
    # Redraw
    # ============================================================

    def redraw(self):
        self.canvas.delete("all")
        if self.show_grid:
            self.draw_grid()

        for si, shape in enumerate(self.state.shapes):
            trace = getattr(shape, "trace", None)
            if trace is None or not trace.points:
                continue

            pts   = trace.points
            color = "blue" if si == self.current_shape_index else "black"

            if trace.kind == "S":
                sub = getattr(trace, "sub", None)

                if sub == "circle" and len(pts) >= 1:
                    cx      = self._sx(pts[0].x)
                    cy      = self._sy(pts[0].y)
                    r_world = trace.get_tr(0)
                    if r_world is not None:
                        r_px = abs(r_world) * self.view_scale
                        self.canvas.create_oval(
                            cx - r_px, cy - r_px,
                            cx + r_px, cy + r_px,
                            outline="purple", width=2)
                        self.canvas.create_text(
                            cx + 6, cy - 10,
                            text=f"r={round(abs(r_world), 2)}",
                            anchor="w", fill="purple",
                            font=("Segoe UI", 8))
                        off = getattr(shape, "offset", None)
                        if off is not None:
                            r_off_px = (abs(r_world) + off) \
                                       * self.view_scale
                            if r_off_px > 0:
                                self.canvas.create_oval(
                                    cx - r_off_px, cy - r_off_px,
                                    cx + r_off_px, cy + r_off_px,
                                    outline="#cc6600",
                                    width=1, dash=(4, 3))
                    self.canvas.create_oval(
                        cx-4, cy-4, cx+4, cy+4, fill="purple")
                    wf = getattr(shape, "workflow", None)
                    if wf:
                        self.canvas.create_text(
                            cx + 6, cy + 8,
                            text=wf.replace("_", " "),
                            anchor="w", fill="#884400",
                            font=("Segoe UI", 7))
                    continue

                if sub == "drill" and len(pts) >= 1:
                    cx = self._sx(pts[0].x)
                    cy = self._sy(pts[0].y)
                    sz = 8
                    self.canvas.create_line(
                        cx-sz, cy, cx+sz, cy,
                        fill="green", width=2)
                    self.canvas.create_line(
                        cx, cy-sz, cx, cy+sz,
                        fill="green", width=2)
                    self.canvas.create_oval(
                        cx-3, cy-3, cx+3, cy+3, fill="green")
                    continue

                if sub == "bulge":
                    self._draw_trace_segments(
                        pts, trace, "magenta",
                        closed=trace.closed, dash=(6, 3))
                    for p in pts:
                        sx, sy = self._sx(p.x), self._sy(p.y)
                        self.canvas.create_oval(
                            sx-3, sy-3, sx+3, sy+3,
                            fill="magenta")
                    continue

                if sub == "moulding":
                    self._draw_trace_segments(
                        pts, trace, "orange", closed=False)
                    for p in pts:
                        sx, sy = self._sx(p.x), self._sy(p.y)
                        self.canvas.create_oval(
                            sx-3, sy-3, sx+3, sy+3,
                            fill="orange")
                    continue

            if trace.kind == "T":
                self._draw_trace_segments(
                    pts, trace, "teal",
                    closed=trace.closed, dash=(4, 3))
                for p in pts:
                    sx, sy = self._sx(p.x), self._sy(p.y)
                    self.canvas.create_oval(
                        sx-3, sy-3, sx+3, sy+3, fill="teal")
                continue

            # Standard R / B traces
            self._draw_trace_segments(
                pts, trace, color, closed=trace.closed)

            for p in pts:
                sx, sy = self._sx(p.x), self._sy(p.y)
                self.canvas.create_oval(
                    sx-3, sy-3, sx+3, sy+3, fill=color)

        # Selected point highlight
        cur_shape = self.get_current_shape()
        cur_trace = getattr(cur_shape, "trace", None)

        if cur_trace and self.selected_point_index is not None:
            idx = self.selected_point_index
            if 0 <= idx < len(cur_trace.points):
                p      = cur_trace.points[idx]
                sx, sy = self._sx(p.x), self._sy(p.y)
                self.canvas.create_oval(
                    sx-6, sy-6, sx+6, sy+6,
                    outline="red", width=2)

        # First-point marker
        if cur_trace and cur_trace.points:
            p      = cur_trace.points[0]
            sx, sy = self._sx(p.x), self._sy(p.y)
            self.canvas.create_oval(
                sx-6, sy-6, sx+6, sy+6,
                outline="cyan", width=2)

        # NC toolpath overlay
        if self.show_toolpath and self.toolpath_points:
            self.draw_nc_toolpath()
            self.draw_nc_legend()

        self.draw_alert(self.canvas)


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    app  = ShapeEditor(root)
    root.mainloop()
