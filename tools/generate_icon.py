"""Generate SpaceDrive icon (neon cyan circle + arrow).

Reproduces logo: open neon cyan circle + emergent arrow at bottom-right,
with external glow effect. Saves as PNG (multi-size) and Windows ICO
in ``assets/`` folder at project root.

Usage:
    python tools/generate_icon.py
"""
import os
import math
from PIL import Image, ImageDraw, ImageFilter

# Main neon cyan color (close to user's logo)
CYAN = (0, 230, 232, 255)        # #00E6E8
CYAN_GLOW = (0, 200, 230, 130)   # same hue more translucent for glow

# Working resolution (downscale later for ICO)
WORK_SIZE = 1024


def _draw_arc_thick(draw, bbox, start_deg, end_deg, color, width):
    """Draw thick arc (PIL.arc only draws 1 px at thin width,
    simulate thickness via multiple concentric arcs).
    """
    draw.arc(bbox, start=start_deg, end=end_deg, fill=color, width=width)


def _draw_arrow_head(draw, tip, direction_deg, size, color):
    """Draw triangular arrowhead pointing in direction_deg.

    Args:
        tip: (x, y) arrow tip
        direction_deg: angle of tip (0° = right, 90° = down)
        size: triangle side length
        color: RGBA
    """
    a = math.radians(direction_deg)
    # Triangle base perpendicular to direction, behind tip
    back_x = tip[0] - size * math.cos(a)
    back_y = tip[1] - size * math.sin(a)
    # Perpendicular
    perp = a + math.pi / 2
    half = size * 0.6
    p1 = (back_x + half * math.cos(perp), back_y + half * math.sin(perp))
    p2 = (back_x - half * math.cos(perp), back_y - half * math.sin(perp))
    draw.polygon([tip, p1, p2], fill=color)


def render_icon(size=WORK_SIZE):
    """Render icon at given resolution.

    Returns:
        PIL.Image RGBA transparent background.
    """
    # Transparent canvas
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))

    # Geometry: centered circle, radius ~38% of canvas
    cx, cy = size / 2, size / 2
    radius = size * 0.36
    stroke = max(2, int(size * 0.04))  # ~4% of canvas

    # bbox for arc
    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)

    # ─── Glow layer ──────────────────────────────────────────────
    # Draw pattern on layer, blur, composite under sharp pattern
    glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    glow_stroke = int(stroke * 1.6)

    # Nearly complete cyan arc, gap between ~340° and ~30° (~50° opening right)
    # PIL angles: 0° = 3h, increases clockwise, so draw 30° to 340°.
    _draw_arc_thick(gdraw, bbox, 30, 340, CYAN_GLOW, glow_stroke)

    # Arrow emerging from gap (toward ~25° = bottom-right outside)
    arrow_angle = 25  # angle arrow points
    arrow_anchor_angle = 25  # attachment position on circle (right gap)
    anchor_x = cx + radius * math.cos(math.radians(arrow_anchor_angle))
    anchor_y = cy + radius * math.sin(math.radians(arrow_anchor_angle))
    # Arrow extends outward, points right (horizontal)
    arrow_length = size * 0.14
    tip_x = anchor_x + arrow_length * math.cos(math.radians(arrow_angle))
    tip_y = anchor_y + arrow_length * math.sin(math.radians(arrow_angle))
    # Line connecting anchor to tip
    gdraw.line(
        [(anchor_x, anchor_y), (tip_x, tip_y)],
        fill=CYAN_GLOW, width=glow_stroke,
    )
    _draw_arrow_head(gdraw, (tip_x, tip_y), arrow_angle, size * 0.13, CYAN_GLOW)

    # Blur glow layer
    glow_blur = max(4, int(size * 0.04))
    glow = glow.filter(ImageFilter.GaussianBlur(glow_blur))

    # ─── Sharp layer ─────────────────────────────────────────────
    sharp = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(sharp)
    _draw_arc_thick(sdraw, bbox, 30, 340, CYAN, stroke)
    sdraw.line(
        [(anchor_x, anchor_y), (tip_x, tip_y)],
        fill=CYAN, width=stroke,
    )
    _draw_arrow_head(sdraw, (tip_x, tip_y), arrow_angle, size * 0.13, CYAN)

    # Composite: glow first, sharp pattern on top
    img = Image.alpha_composite(img, glow)
    img = Image.alpha_composite(img, sharp)

    return img


def main():
    # Output folder: <project_root>/assets/
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(tools_dir)
    out_dir = os.path.join(project_root, 'assets')
    os.makedirs(out_dir, exist_ok=True)

    # High-resolution image
    master = render_icon(WORK_SIZE)
    png_path = os.path.join(out_dir, 'icon.png')
    master.save(png_path, 'PNG')
    print(f"Wrote {png_path} ({WORK_SIZE}x{WORK_SIZE})")

    # Multi-size Windows ICO
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_images = []
    for s in ico_sizes:
        resized = master.resize((s, s), Image.Resampling.LANCZOS)
        ico_images.append(resized)
    ico_path = os.path.join(out_dir, 'icon.ico')
    ico_images[0].save(
        ico_path,
        format='ICO',
        sizes=[(s, s) for s in ico_sizes],
        append_images=ico_images[1:],
    )
    print(f"Wrote {ico_path} (sizes: {ico_sizes})")

    # 64x64 PNG for quick preview
    preview = master.resize((64, 64), Image.Resampling.LANCZOS)
    preview_path = os.path.join(out_dir, 'icon_64.png')
    preview.save(preview_path, 'PNG')
    print(f"Wrote {preview_path} (64x64)")


if __name__ == '__main__':
    main()
