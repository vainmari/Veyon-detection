"""
app/core/colors.py
──────────────────
Shared 32-color palette used by:
  • imaging.py   — BGR tuples for cv2 bounding boxes
  • database.py  — hex strings stored in detection_class.color_hex
  • UI pages     — hex strings for badges and chart series

Keeping one source of truth ensures the live preview bounding-box colors
always match the chart colors, even after a model with new classes is activated.
"""

HEX_PALETTE: list[str] = [
    "#00ff00", "#ff8000", "#0080ff", "#8000ff",
    "#00ffff", "#ff0080", "#40c840", "#ff0000",
    "#0000ff", "#c8c800", "#ff00ff", "#00ff80",
    "#ff4000", "#4000ff", "#00c8c8", "#c800c8",
    "#80ff00", "#0040ff", "#ff0040", "#40ff40",
    "#ff8080", "#8080ff", "#80ff80", "#ffff80",
    "#ff80ff", "#80ffff", "#c04000", "#004080",
    "#400080", "#008040", "#804000", "#008080",
]


def class_hex(index: int) -> str:
    """Return the hex color for a class index — cycles for any number of classes."""
    return HEX_PALETTE[index % len(HEX_PALETTE)]


def hex_to_bgr(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b, g, r)


BGR_PALETTE: list[tuple[int, int, int]] = [hex_to_bgr(h) for h in HEX_PALETTE]