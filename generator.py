import json
import math
import pyclipper

SCALE = 3000  # mm → integer units

# ---------------------------------------------------------
# BASIC GEOMETRY UTILITIES
# ---------------------------------------------------------

def scale_up(points):
    return [(int(round(x * SCALE)), int(round(y * SCALE))) for x, y in points]


def scale_down(points):
    return [(x / SCALE, y / SCALE) for x, y in points]


def ensure_orientation(poly, clockwise=True):
    if not poly:
        return poly
    is_cw = pyclipper.Orientation(poly)
    if clockwise and not is_cw:
        return list(reversed(poly))
    if not clockwise and is_cw:
        return list(reversed(poly))
    return poly


def ensure_closed(poly):
    if not poly:
        return poly
    if poly[0] != poly[-1]:
        return poly + [poly[0]]
    return poly


# ---------------------------------------------------------
# ARC INTERPOLATION
# Used to expand arc-bearing traces into dense polylines
# before scaling to integer clipper units.
# ---------------------------------------------------------

def arc_points_mm(x1, y1, x2, y2, radius, steps=32):
    """
    Return a list of (x, y) mm points along the arc defined by
    endpoints (x1,y1)→(x2,y2) and a signed radius.
    Positive radius → arc bulges left of the chord direction.
    Returns a straight-line pair when radius is None / 0 or too small.
    """
    if radius is None or radius == 0:
        return [(x1, y1), (x2, y2)]

    dx     = x2 - x1
    dy     = y2 - y1
    half_c = math.sqrt(dx * dx + dy * dy) / 2.0
    abs_r  = abs(radius)

    if half_c < 1e-9 or abs_r < half_c:
        return [(x1, y1), (x2, y2)]

    cx_mid = (x1 + x2) / 2.0
    cy_mid = (y1 + y2) / 2.0
    h      = math.sqrt(abs_r * abs_r - half_c * half_c)
    length = math.sqrt(dx * dx + dy * dy)
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


def expand_trace_to_polyline(trace_data):
    """
    Convert a trace dict (with optional 'tr' arc radii list) into a
    flat list of (x, y) mm points suitable for scale_up().
    Straight segments are kept as-is; arc segments are interpolated.
    """
    pts = trace_data.get("points", [])
    tr  = trace_data.get("tr", [])
    n   = len(pts)

    if n == 0:
        return []
    if n == 1:
        return [(pts[0][0], pts[0][1])]

    closed = trace_data.get("closed", False)
    result = []

    num_seg = n if closed else n - 1

    for seg in range(num_seg):
        p1 = pts[seg]
        p2 = pts[(seg + 1) % n]
        r  = tr[seg] if seg < len(tr) else None

        seg_pts = arc_points_mm(p1[0], p1[1], p2[0], p2[1], r)

        if seg == 0:
            result.extend(seg_pts)
        else:
            result.extend(seg_pts[1:])   # avoid duplicate junction point

    return result


# ---------------------------------------------------------
# CIRCLE TOOLPATH BUILDER
# Generates a closed circular polyline in mm, then scales up.
# ---------------------------------------------------------

def circle_polyline_mm(cx, cy, radius, steps=64):
    """Return a closed list of (x, y) mm points for a circle."""
    pts = []
    for i in range(steps):
        a = 2 * math.pi * i / steps
        pts.append((cx + radius * math.cos(a),
                    cy + radius * math.sin(a)))
    pts.append(pts[0])   # close
    return pts


# ---------------------------------------------------------
# POCKET REGION: OUTER MINUS INNER
# ---------------------------------------------------------

def compute_pocket_region(outer, inner):
    pc = pyclipper.Pyclipper()
    pc.AddPath(outer, pyclipper.PT_SUBJECT, True)
    pc.AddPath(inner, pyclipper.PT_CLIP,    True)
    result = pc.Execute(
        pyclipper.CT_DIFFERENCE,
        pyclipper.PFT_NONZERO,
        pyclipper.PFT_NONZERO
    )
    return [ensure_closed(p) for p in result]


def offset_polygon(polys, offset_int):
    """
    Offset a list of integer-unit closed polygons by offset_int units.
    Positive → expand, negative → shrink.
    Returns list of closed integer-unit polygons.
    """
    if not polys:
        return []
    co = pyclipper.PyclipperOffset()
    for p in polys:
        co.AddPath(p, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    result = co.Execute(offset_int)
    return [ensure_closed(p) for p in result]


def offset_inward(polys, offset):
    return offset_polygon(polys, -offset)


# ---------------------------------------------------------
# POCKET FILL — OUTER MINUS INNER WITH STEPOVER RINGS
# ---------------------------------------------------------

def generate_pocket_with_inner(outer, inner, stepover, min_stepover):
    rings   = []
    current = compute_pocket_region(outer, inner)
    if not current:
        return []
    rings.extend(current)

    while True:
        if stepover < min_stepover:
            break
        next_polys = offset_inward(current, stepover)
        if not next_polys:
            break
        rings.extend(next_polys)
        current = next_polys

    return rings


def generate_pocket_fill(outer_poly, stepover, min_stepover):
    """Fill a simple closed pocket (no islands) with inward offset rings."""
    current = [outer_poly]
    rings   = []
    while True:
        rings.extend(current)
        next_polys = offset_inward(current, stepover)
        if not next_polys:
            break
        current = next_polys
    return rings


# ---------------------------------------------------------
# SHAPEX LOADER — MULTI-OPERATION
# ---------------------------------------------------------

def load_shapex(path="shapex.json"):
    with open(path, "r") as f:
        data = json.load(f)

    # Support both legacy {"shape": {...}} and direct {"shapes": [...]}
    if "shape" in data:
        shapes_list = data["shape"]["shapes"]
    else:
        shapes_list = data.get("shapes", [])

    machine = data.get("machine", {})

    # ── Collected operation buckets ──────────────────────────────
    pockets        = []
    profiles       = []      # {"kind", "poly", "offset_int"}
    traces         = []      # raw integer-unit polylines (no offset)
    drills         = []      # [(cx_mm, cy_mm), ...]
    special_circle = []      # {"cx","cy","radius","workflow","offset_mm"}
    special_bulge  = []      # {"poly", "workflow"}   (overlay, no offset)
    special_mould  = []      # {"polyline"}           (open, no offset)

    pocket_outer   = None
    pocket_islands = []

    for s in shapes_list:
        role     = s.get("role", "profile")
        trace    = s.get("trace") or {}
        kind     = trace.get("kind", "R")
        sub      = trace.get("sub",  None)
        pts      = trace.get("points", [])
        workflow = s.get("workflow", None)
        offset   = s.get("offset",   None)   # signed mm from editor

        # ── SPECIAL SHAPES (kind == "S") ─────────────────────────
        if kind == "S":
            _handle_special(
                sub, pts, trace, workflow, offset,
                special_circle, special_bulge, special_mould, drills)
            continue

        # ── DRILL (legacy role) ───────────────────────────────────
        if role == "drill":
            if pts:
                drills.append((float(pts[0][0]), float(pts[0][1])))
            continue

        # ── TRACE (no offset) ─────────────────────────────────────
        if role == "trace":
            polyline = expand_trace_to_polyline(trace)
            if polyline:
                traces.append(scale_up(polyline))
            continue

        # ── Closed polygon shapes ─────────────────────────────────
        if not pts:
            continue

        polyline = expand_trace_to_polyline(trace)
        poly     = ensure_closed(
                       ensure_orientation(scale_up(polyline), True))

        if role == "pocket":
            if kind == "R":
                pocket_outer = poly
            elif kind == "B":
                pocket_islands.append(poly)
            continue

        if role in ("profile", "hole"):
            # Determine integer offset
            if offset is not None:
                offset_int = int(round(float(offset) * SCALE))
            else:
                # Legacy: R → outside (+), B → inside (−)
                offset_int = None   # resolved in build_toolpaths
            profiles.append({
                "kind":       kind,
                "poly":       poly,
                "offset_int": offset_int,
            })
            continue

    if pocket_outer is not None:
        pockets.append({
            "outer":   pocket_outer,
            "islands": pocket_islands,
        })

    return {
        "pockets":        pockets,
        "profiles":       profiles,
        "traces":         traces,
        "drills":         drills,
        "special_circle": special_circle,
        "special_bulge":  special_bulge,
        "special_mould":  special_mould,
        "params": {
            "target_depth": float(machine.get("target_depth",  3.0)),
            "num_passes":   int(  machine.get("num_passes",    3)),
            "feed_xy":      float(machine.get("feed",          800.0)),
            "feed_z":       float(machine.get("plunge",        200.0)),
            "safe":         float(machine.get("safe_clearance",5.0)),
            "spindle":      float(machine.get("spindle_rpm",   12000.0)),
            "emit_spindle": bool( machine.get("emit_spindle",  1)),
            "stepover":     float(machine.get("stepover",      2.0)),
            "tow":          float(machine.get("tow",           10.0)),
            "tool_id":      int(  machine.get("tool_id",       1)),
        }
    }


def _handle_special(sub, pts, trace, workflow, offset,
                    special_circle, special_bulge,
                    special_mould, drills):
    """
    Route a kind='S' shape into the correct special bucket.

    sub == 'circle'   → special_circle
    sub == 'bulge'    → special_bulge   (overlay, no offset)
    sub == 'drill'    → drills
    sub == 'moulding' → special_mould
    """
    if sub == "circle":
        if not pts:
            return
        cx = float(pts[0][0])
        cy = float(pts[0][1])
        # Radius is stored in trace['tr'][0] (the stored radius value)
        tr_list = trace.get("tr", [])
        radius  = abs(float(tr_list[0])) if tr_list and tr_list[0] is not None \
                  else None
        if radius is None or radius <= 0:
            return
        special_circle.append({
            "cx":       cx,
            "cy":       cy,
            "radius":   radius,
            "workflow": workflow,
            "offset_mm": float(offset) if offset is not None else 0.0,
        })

    elif sub == "bulge":
        # Bulge overlays a base trace — no offset applied.
        # The stored points ARE the toolpath.
        polyline = expand_trace_to_polyline(trace)
        if len(polyline) >= 2:
            special_bulge.append({
                "poly":     scale_up(polyline),
                "closed":   trace.get("closed", False),
                "workflow": workflow,
            })

    elif sub == "drill":
        if pts:
            drills.append((float(pts[0][0]), float(pts[0][1])))

    elif sub == "moulding":
        polyline = expand_trace_to_polyline(trace)
        if len(polyline) >= 2:
            special_mould.append({
                "polyline": scale_up(polyline),
                "workflow": workflow,
            })


# ---------------------------------------------------------
# DISPATCHER — BUILD TOOLPATHS FOR ALL OPERATIONS
# Returns a dict with separate lists for each pass type so
# the G-code stage can handle tool changes cleanly.
# ---------------------------------------------------------

def build_toolpaths(model):
    P        = model["params"]
    stepover = int(round(P["stepover"] * SCALE))
    min_so   = int(round(0.2 * SCALE))

    # Output containers
    pass_A_rings = []   # Pass A: profile / waste / single-pass
    pass_B_rings = []   # Pass B: edge / moulding (tool-change required)
    drill_pts    = list(model["drills"])   # [(cx_mm, cy_mm), ...]

    # ── POCKETS ──────────────────────────────────────────────────
    for pk in model["pockets"]:
        outer   = pk["outer"]
        islands = pk["islands"]
        if not islands:
            rings = generate_pocket_fill(outer, stepover, min_so)
        else:
            rings = generate_pocket_with_inner(
                outer, islands[0], stepover, min_so)
        pass_A_rings.extend(rings)

    # ── PROFILES ─────────────────────────────────────────────────
    for pr in model["profiles"]:
        poly       = pr["poly"]
        kind       = pr["kind"]
        offset_int = pr.get("offset_int", None)

        co = pyclipper.PyclipperOffset()
        co.AddPath(poly, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)

        if offset_int is not None:
            out = co.Execute(offset_int)
        else:
            # Legacy fallback
            out = co.Execute(+stepover if kind == "R" else -stepover)

        if out:
            pass_A_rings.extend([ensure_closed(p) for p in out])
        else:
            pass_A_rings.append(poly)

    # ── TRACES (no offset) ───────────────────────────────────────
    for tr in model["traces"]:
        pass_A_rings.append(tr)

    # ── SPECIAL CIRCLES ──────────────────────────────────────────
    for sc in model["special_circle"]:
        _build_circle_toolpaths(sc, stepover, pass_A_rings, pass_B_rings)

    # ── SPECIAL BULGE (overlay, no offset) ───────────────────────
    for sb in model["special_bulge"]:
        # Bulge is always a two-pass edge shape
        poly = sb["poly"]
        pass_A_rings.append(poly)   # Pass A: base profile line
        pass_B_rings.append(poly)   # Pass B: edge tool follows same line

    # ── SPECIAL MOULDING (open polyline, no offset) ──────────────
    for sm in model["special_mould"]:
        pass_A_rings.append(sm["polyline"])

    return {
        "pass_A":  pass_A_rings,
        "pass_B":  pass_B_rings,
        "drills":  drill_pts,
        "two_pass": len(pass_B_rings) > 0,
    }


def _build_circle_toolpaths(sc, stepover_int, pass_A, pass_B):
    """
    Route a special circle into pass_A and/or pass_B rings
    according to its workflow key.

    workflow         offset sign   passes   tool change
    ─────────────────────────────────────────────────────
    profile_plus     +             A only   no
    profile_minus    −             A only   no
    pocket_minus     −             A only   no  (fill rings)
    edge_plus        +             A + B    yes
    edge_minus       −             A + B    yes
    """
    cx       = sc["cx"]
    cy       = sc["cy"]
    radius   = sc["radius"]
    workflow = sc.get("workflow", "profile_plus") or "profile_plus"
    off_mm   = sc.get("offset_mm", 0.0)

    # ── Single-pass profiles ──────────────────────────────────────
    if workflow in ("profile_plus", "profile_minus"):
        r_tool = radius + off_mm          # off_mm already signed
        if r_tool > 0:
            poly_mm  = circle_polyline_mm(cx, cy, r_tool)
            poly_int = scale_up(poly_mm)
            pass_A.append(ensure_closed(poly_int))

    # ── Pocket (fill inward from offset circle) ───────────────────
    elif workflow == "pocket_minus":
        r_outer = radius + off_mm         # off_mm is negative → smaller
        if r_outer <= 0:
            return
        outer_mm  = circle_polyline_mm(cx, cy, r_outer)
        outer_int = ensure_closed(
                        ensure_orientation(scale_up(outer_mm), True))
        rings = generate_pocket_fill(outer_int, stepover_int,
                                     int(round(0.2 * SCALE)))
        pass_A.extend(rings)

    # ── Edge shaped — two passes ──────────────────────────────────
    elif workflow in ("edge_plus", "edge_minus"):
        r_tool = radius + off_mm          # off_mm already signed
        if r_tool <= 0:
            return

        # Pass A — profile cut at offset radius (waste removal)
        poly_mm  = circle_polyline_mm(cx, cy, r_tool)
        poly_int = ensure_closed(scale_up(poly_mm))
        pass_A.append(poly_int)

        # Pass B — edge tool follows the exact offset circle again
        # (the generator will emit this after a tool-change prompt)
        pass_B.append(poly_int)


# ---------------------------------------------------------
# GCODE GENERATOR
# Accepts the dict returned by build_toolpaths().
# Emits pass A, then (if two_pass) a tool-change block, then pass B,
# then drill cycles.
# ---------------------------------------------------------

def generate_gcode(
    toolpaths,
    target_depth  = 3.0,
    num_passes    = 3,
    feed_xy       = 800,
    feed_z        = 300,
    safe          = 5.0,
    spindle       = 12000,
    emit_spindle  = True,
    stepover_mm   = 1.0,
    tow           = 20.0,
    tool_id       = 1,
):
    """
    toolpaths — dict from build_toolpaths():
        pass_A   : list of closed integer-unit polygons
        pass_B   : list of closed integer-unit polygons (edge / moulding)
        drills   : list of (cx_mm, cy_mm)
        two_pass : bool
    """
    # Accept legacy list input (plain list of rings → all go to pass_A)
    if isinstance(toolpaths, list):
        toolpaths = {
            "pass_A":   toolpaths,
            "pass_B":   [],
            "drills":   [],
            "two_pass": False,
        }

    pass_A   = toolpaths.get("pass_A",   [])
    pass_B   = toolpaths.get("pass_B",   [])
    drills   = toolpaths.get("drills",   [])
    two_pass = toolpaths.get("two_pass", False)

    lines    = []
    Z_safe   = safe + tow
    stepdown = target_depth / num_passes
    depths   = [stepdown * (i + 1) for i in range(num_passes)]

    # ── Preamble ─────────────────────────────────────────────────
    lines.append("; === Shape Editor G-code output ===")
    lines.append("G21        ; mm mode")
    lines.append("G90        ; absolute")
    lines.append(f"G0 Z{Z_safe:.3f}  ; safe height")
    if emit_spindle:
        lines.append(f"M3 S{int(spindle)}  ; spindle on")
    lines.append(f"T{tool_id} M6  ; tool {tool_id}")
    lines.append("")

    # ── Helper: emit one closed ring for all depth passes ────────
    def emit_ring(poly, label=""):
        if not poly:
            return
        pts = scale_down(poly)
        if not pts:
            return
        if label:
            lines.append(f"; {label}")
        x0, y0 = pts[0]
        for depth in depths:
            Z_cut = tow - depth
            lines.append(f"G0 X{x0:.3f} Y{y0:.3f} Z{Z_safe:.3f}")
            lines.append(f"G1 Z{Z_cut:.3f} F{feed_z}")
            for x, y in pts[1:]:
                lines.append(f"G1 X{x:.3f} Y{y:.3f} F{feed_xy}")
            lines.append(f"G1 X{x0:.3f} Y{y0:.3f} F{feed_xy}  ; close")
            lines.append(f"G0 Z{Z_safe:.3f}")
        lines.append("")

    # ── Helper: emit an open polyline (moulding / trace) ─────────
    def emit_open(poly, label=""):
        if not poly:
            return
        pts = scale_down(poly)
        if not pts:
            return
        if label:
            lines.append(f"; {label}")
        x0, y0 = pts[0]
        for depth in depths:
            Z_cut = tow - depth
            lines.append(f"G0 X{x0:.3f} Y{y0:.3f} Z{Z_safe:.3f}")
            lines.append(f"G1 Z{Z_cut:.3f} F{feed_z}")
            for x, y in pts[1:]:
                lines.append(f"G1 X{x:.3f} Y{y:.3f} F{feed_xy}")
            lines.append(f"G0 Z{Z_safe:.3f}")
        lines.append("")

    # ── PASS A ───────────────────────────────────────────────────
    if pass_A:
        lines.append("; ---- Pass A ----")
        for i, ring in enumerate(pass_A):
            emit_ring(ring, label=f"Pass A ring {i+1}")

    # ── TOOL CHANGE (two-pass workflows) ─────────────────────────
    if two_pass and pass_B:
        lines.append("; ============================================")
        lines.append("; TOOL CHANGE REQUIRED before continuing")
        lines.append("; Install edge / moulding tool, then resume")
        lines.append("; ============================================")
        lines.append(f"G0 Z{Z_safe:.3f}")
        lines.append("G0 X0 Y0")
        if emit_spindle:
            lines.append("M5  ; spindle off for tool change")
        lines.append("M0  ; program stop — change tool now")
        if emit_spindle:
            lines.append(f"M3 S{int(spindle)}  ; spindle back on")
        lines.append("")

        lines.append("; ---- Pass B (edge / moulding tool) ----")
        for i, ring in enumerate(pass_B):
            emit_ring(ring, label=f"Pass B ring {i+1}")

    # ── DRILL CYCLES ─────────────────────────────────────────────
    if drills:
        lines.append("; ---- Drill cycles ----")
        for i, (cx, cy) in enumerate(drills):
            lines.append(f"; Drill {i+1}  X={cx:.3f} Y={cy:.3f}")
            lines.append(f"G0 X{cx:.3f} Y{cy:.3f} Z{Z_safe:.3f}")
            for depth in depths:
                Z_cut = tow - depth
                lines.append(f"G1 Z{Z_cut:.3f} F{feed_z}")
                lines.append(f"G0 Z{Z_safe:.3f}")
            lines.append("")

    # ── End of program ────────────────────────────────────────────
    if emit_spindle:
        lines.append("M5   ; spindle off")
    lines.append("G0 X0 Y0")
    lines.append("M2   ; end of program")

    return "\n".join(lines)


# ---------------------------------------------------------
# TWO-FILE WRITER
# For edge-shaped workflows, writes shapexA.nc and shapexB.nc
# as separate files so the operator loads them sequentially.
# ---------------------------------------------------------

def write_two_pass_files(
    toolpaths,
    base_name     = "shapex",
    target_depth  = 3.0,
    num_passes    = 3,
    feed_xy       = 800,
    feed_z        = 300,
    safe          = 5.0,
    spindle       = 12000,
    emit_spindle  = True,
    stepover_mm   = 1.0,
    tow           = 20.0,
    tool_id       = 1,
):
    """
    Write two separate .nc files when a tool change is required.
      {base_name}A.nc  — Pass A (profile / waste removal)
      {base_name}B.nc  — Pass B (edge / moulding tool)
    Returns (path_A, path_B).
    """
    common = dict(
        target_depth = target_depth,
        num_passes   = num_passes,
        feed_xy      = feed_xy,
        feed_z       = feed_z,
        safe         = safe,
        spindle      = spindle,
        emit_spindle = emit_spindle,
        stepover_mm  = stepover_mm,
        tow          = tow,
        tool_id      = tool_id,
    )

    # File A — pass_A rings only, no tool change block
    tp_A = {
        "pass_A":   toolpaths.get("pass_A", []),
        "pass_B":   [],
        "drills":   toolpaths.get("drills", []),
        "two_pass": False,
    }
    gcode_A = generate_gcode(tp_A, **common)
    path_A  = f"{base_name}A.nc"
    with open(path_A, "w") as f:
        f.write(gcode_A)

    # File B — pass_B rings only, no drill repeat
    tp_B = {
        "pass_A":   toolpaths.get("pass_B", []),
        "pass_B":   [],
        "drills":   [],
        "two_pass": False,
    }
    gcode_B = generate_gcode(tp_B, **common)
    path_B  = f"{base_name}B.nc"
    with open(path_B, "w") as f:
        f.write(gcode_B)

    return path_A, path_B


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

if __name__ == "__main__":
    model     = load_shapex()
    toolpaths = build_toolpaths(model)
    P         = model["params"]

    common_params = dict(
        target_depth = P["target_depth"],
        num_passes   = P["num_passes"],
        feed_xy      = P["feed_xy"],
        feed_z       = P["feed_z"],
        safe         = P["safe"],
        spindle      = P["spindle"],
        emit_spindle = P["emit_spindle"],
        stepover_mm  = P["stepover"],
        tow          = P["tow"],
        tool_id      = P["tool_id"],
    )

    if toolpaths["two_pass"]:
        # Edge-shaped or bulge workflow → two separate files
        path_A, path_B = write_two_pass_files(
            toolpaths, base_name="shapex", **common_params)
        print(f"Two-pass output written:")
        print(f"  Pass A → {path_A}")
        print(f"  Pass B → {path_B}")
    else:
        # Single-pass → one combined file
        gcode = generate_gcode(toolpaths, **common_params)
        with open("output.nc", "w") as f:
            f.write(gcode)
        print("output.nc written.")