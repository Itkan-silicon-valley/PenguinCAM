# Machine-Specific Assumptions for PenguinCAM-Generated G-Code

This document describes **implicit machine, controller, and interpreter assumptions** made by PenguinCAM when generating G-code.

These assumptions are **independent of user-configurable inputs** such as material, thickness, tool diameter, feeds/speeds, tab count, retract heights, or cut depth. If any assumption below is false on a target machine, the generated G-code may behave incorrectly or unsafely.

---

## 1. Controller & Motion Stack

By **default** the output is **G54 work-coordinate only** and uses a common, portable
subset of G-code (`G0 G1 G2 G3 G4 G17 G20/G21 G40 G49 G54 G80 G90 G91.1 G94`, `M0 M3 M5
M30`). It is designed to run on **GRBL, WinCNC, Mach3/Mach4, LinuxCNC** and similar
controllers without machine-coordinate moves.

Two features are **opt-in via config** and, if enabled, add codes that not every
controller supports:

* **`park_position`** (machine-coordinate park) → emits `G53` machine moves. Leave it out
  for controllers that don't support `G53` (e.g. GRBL/Easel behave unexpectedly). This is
  the ONLY thing that puts `G53` in the output.
* **`machine.coolant`** (`Air`/`Mist`/`Flood`) → emits `M7`/`M8`/`M9`. Leave it out (or set
  `None`) on controllers without coolant M-codes (stock GRBL rejects `M7` unless compiled
  with it).

Still assumed regardless: standard modal behavior and state persistence.

Note: **Easel** (Inventables) additionally rejects arcs (`G2/G3`) on import — PenguinCAM's
arc-based toolpaths need a separate linearized export for the Easel *app* (they run fine on
the underlying GRBL controller via a general sender).

---

## 2. Work vs Machine Coordinates

* **G54** is used for all programmed motion, including start/end safe-Z retracts (in work
  coordinates, `G0 Z<safe_height>`).
* **G53** machine-coordinate moves appear **only** when `park_position` is configured
  (the end-of-program / tube-flip gantry park). With no `park_position`, the program is
  entirely G54 and portable.

---

## 3. Machine Z-Axis Orientation

* Machine Z **increases upward** (standard).
* The safe retract height is a **work-coordinate** value above Z=0 (the sacrifice board):
  `z_reference.safe_height` if set, else `material_thickness + clearance`. No assumption is
  made about where machine Z=0 sits — that was the old `G53`-based behavior and is gone by
  default.
* If you enable `park_position`, then (and only then) the program assumes machine Z=0 is a
  safe high position, since the park raises to `G53 Z<park_z>`.
