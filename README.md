## ShapeEditor — User Guide1. 

in a libaray folder
<img width="1920" height="982" alt="Editor" src="https://github.com/user-attachments/assets/36a43f13-f582-4c79-9f11-7f7669a23e0c" />


### What Is ShapeEditor?

ShapeEditor aims to simplify offsite flat pack production of cabinates for rapid onsite installaion by joiners, shop fitters and DIY builders. Users can create their individual components in a libaray folder such a pockets for soft close hinges, mouldings etc, and use them in their current project.  ShapeEditor is a CNC geometry tool for creating, editing, and organising shapes that output clean G-code for grblHAL-based controllers (e.g. the RP23CNC).

You can:

Draw shapes using points and traces
Edit geometry visually or numerically
Apply trace sub-types (Profile, Pocket, Circles, Curves)
Manage multiple shapes per sheet
Transfer geometry between sheets
Generate G-code for each sheet

### 2. Basic Navigation


Action
How To




Zoom
Mouse wheel (cursor-anchored)


Pan
Middle mouse button


Reset View
Press R


Select Point
Left-click


Drag Point
Left-click + drag


Fine Move
Arrow keys (1 mm steps)


Rotate Shapes
Use rotate tool


Merge Shapes
Use merge tool


### 3. Creating GeometryDrawing Points & Traces
Click to place your first point.
Click again to place a second point — a trace appears automatically.
Keep clicking to add more points; each connects to the previous.
Right-click to close the loop and form a polygon.
Press Del to reopen the loop if needed.

### Editing Points

Click a point to select it (shown with an orange halo).
Drag it with the mouse, or nudge it with the arrow keys.
Edit X/Y coordinates directly in the table — double-click a cell to type values.
When multiple shapes exist, make sure the correct shape is selected first.
4. Working With Multiple Shapes
Add as many shapes as needed to a single sheet.
Import shapes from file to combine geometry.
Use the Shape box to select the active shape before editing.

### 5. Trace Types & Sub-Types

#### Rtrace (Red)

Sub-Type            Tool Offset Direction

Profile             Outside the line

Pocket              Inside the line


#### Btrace (Blue)  

Sub-Type             Tool Offset Direction

Profile              Inside the line


Pocket                Outside the line



⚠️ Offsets do not apply to the basic Trace type.
When setting a radius to cut a circle, you will be prompted to provide tool offset compensation if required.

### 6. Core Rules
These rules apply to all polygons — no exceptions.

Rule 1 — Btrace Pocket needs a boundary: A Btrace must be a closed polygon before it can be set to Pocket.

Rule 2 — Only Btrace can cut holes: Holes require an inward offset, which only Btrace supports.

Rule 3 — Rules are universal: Offset and validity rules apply consistently across all shapes.


### 7. Sheet-to-Sheet TransferYou can transfer points from one sheet to another, which allows you to:

Reuse existing geometry
Align new work to previous work
Clean up unused transferred points
Each sheet generates its own G-code file, supporting tool changes and keeping workflows separate.

### 8. Polygon WorkflowsProfile 

#### Cut — No Shaped Edge
Tool runs outside the polygon line.

Produces a single .nc file — no tool change needed.

Enter the tool offset (mm) in Machining Parameters.

#### Profile Cut — Shaped Edge
Load the base Rtrace or Btrace (File → Load Shape or Add Shape).

The S/B shape overlays the canvas.

Select the original base trace in the shape list → press Clear Trace to delete it.

Save the remaining S/B trace (Save Selected).

#### Output:

Two .nc profile files produced

A tool change is required between files

Diagnostic shapexA.json and shapexB.json saved before G-code generation

### 10. Circle WorkflowsProfile (+offset)
Tool runs outside the circle line.

Single .nc file — no tool change needed.

Enter tool offset → click canvas once to place centre.

## Profile Minus (-offset) — Hole Cut
Tool runs inside the circle line.

Single .nc file — no tool change needed.

Enter tool offset → click canvas once to place centre.

## Pocket Minus (-offset)
Tool clears all material inside the circle.

Single .nc file — no tool change needed.

Enter tool offset → click canvas once to place centre.

## Edge Plus (+offset) — Shaped Edge
Produces two .nc files:

shapexA.nc → Pass 1 (profile / waste removal)

shapexB.nc → Pass 2 (edge / moulding tool)


A tool change is required between files.

Diagnostic shapexA.json and shapexB.json saved before G-code generation.

Enter tool offset → click canvas once to place centre.

## Edge Minus (-offset) — Shaped Edge
Same two-file output as Edge Plus, but with a negative offset.
A tool change is required between files.
Diagnostic files saved before G-code generation.
Enter tool offset → click canvas once to place centre.


### 10. Bulge Edge Shape Workflow
No offset is applied to this shape type. The S/B trace overlays an existing Rtrace or Btrace.

Load the base Rtrace or Btrace (File → Load Shape or Add Shape).

The S/B shape overlays the canvas.

Select the original base trace → press Clear Trace to delete it.

Save the remaining S/B trace (Save Selected).

#### Output:
Two .nc profile files produced

A tool change is required between files

Diagnostic shapexA.json and shapexB.json saved before G-code generation


### 11. Generating & Running G-Code
ShapeEditor generates a named .nc file for each sheet.

Load the .nc file into iOSender.

Run it on your grblHAL controller.


### 12. Controller Setup — RP23CNC
Download firmware from the grblHAL web builder.

Hold BOOTSEL while plugging the RP23CNC into your computer.

A virtual drive appears on the desktop.

Drag firmware.uf2 onto the drive.

The board flashes automatically.
