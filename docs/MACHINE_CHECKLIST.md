Before running PenguinCAM-generated G-code, confirm the following.

Required for the default (portable, G54-only) output:
☐ Work zero (G54) set at the part origin: X=0/Y=0 at the lower-left, Z=0 at the sacrifice-board surface
☐ Controller supports G91.1 incremental arc centers
☐ Controller supports true helical interpolation (XYZ arcs)
☐ S words specify RPM (not percentage or Hz)
☐ Rapids (G0) do not alter modal feed state

Only if you set `park_position` in your config (this is what adds G53 machine moves):
☐ Controller supports G53 machine-coordinate moves
☐ Machine Z increases upward and machine Z=0 is a safe, high-clearance position
   → If your controller mishandles G53 (e.g. GRBL/Easel), REMOVE `park_position` from your config.

Only if you set `machine.coolant` (this is what adds M7/M8/M9):
☐ Controller supports the coolant M-codes (stock GRBL needs M7 compiled in)
   → If not, remove `coolant` (or set it to `None`).

Notes:
- Easel (Inventables) rejects arcs (G2/G3) on import; PenguinCAM's toolpaths are arc-based.
❌ If a required box is unchecked, review or modify the post-processor before running.
