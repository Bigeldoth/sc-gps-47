"""Génère l'icône SpaceDrive (cercle cyan néon + flèche).

Reproduit le logo : cercle cyan néon ouvert + flèche émergente en bas-droite,
avec effet de glow externe. Sauvegarde en PNG (multi-tailles) et ICO Windows
dans le dossier ``assets/`` à la racine du projet.

Usage:
    python tools/generate_icon.py
"""
import os
import math
from PIL import Image, ImageDraw, ImageFilter

# Couleur principale cyan néon (proche du logo de l'utilisateur)
CYAN = (0, 230, 232, 255)        # #00E6E8
CYAN_GLOW = (0, 200, 230, 130)   # même teinte plus translucide pour le glow

# Résolution de travail (on downscale ensuite pour les ICO)
WORK_SIZE = 1024


def _draw_arc_thick(draw, bbox, start_deg, end_deg, color, width):
    """Dessine un arc épais (PIL.arc dessine sur 1 px seulement à largeur fine,
    on simule l'épaisseur via plusieurs arcs concentriques).
    """
    draw.arc(bbox, start=start_deg, end=end_deg, fill=color, width=width)


def _draw_arrow_head(draw, tip, direction_deg, size, color):
    """Dessine une pointe de flèche triangulaire pointant dans direction_deg.

    Args:
        tip : (x, y) pointe de la flèche
        direction_deg : angle de la pointe (0° = droite, 90° = bas)
        size : longueur du côté du triangle
        color : RGBA
    """
    a = math.radians(direction_deg)
    # Base du triangle perpendiculaire à la direction, derrière la pointe
    back_x = tip[0] - size * math.cos(a)
    back_y = tip[1] - size * math.sin(a)
    # Perpendiculaire
    perp = a + math.pi / 2
    half = size * 0.6
    p1 = (back_x + half * math.cos(perp), back_y + half * math.sin(perp))
    p2 = (back_x - half * math.cos(perp), back_y - half * math.sin(perp))
    draw.polygon([tip, p1, p2], fill=color)


def render_icon(size=WORK_SIZE):
    """Rend l'icône à la résolution donnée.

    Returns:
        PIL.Image RGBA fond transparent.
    """
    # Canvas transparent
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))

    # Géométrie : cercle centré, rayon ~38% du canvas
    cx, cy = size / 2, size / 2
    radius = size * 0.36
    stroke = max(2, int(size * 0.04))  # ~4% du canvas

    # bbox pour l'arc
    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)

    # ─── Couche glow ─────────────────────────────────────────────
    # Dessiner le motif sur un calque, blurrer, composite sous le motif net
    glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    glow_stroke = int(stroke * 1.6)

    # Arc cyan presque complet, gap entre ~340° et ~30° (~50° d'ouverture à droite)
    # PIL angles : 0° = 3h, croît horaire, donc on dessine de 30° à 340°.
    _draw_arc_thick(gdraw, bbox, 30, 340, CYAN_GLOW, glow_stroke)

    # Flèche émergente depuis le gap (vers ~25° = bas-droite extérieur)
    arrow_angle = 25  # angle où la flèche pointe
    arrow_anchor_angle = 25  # position d'attachement sur le cercle (gap droite)
    anchor_x = cx + radius * math.cos(math.radians(arrow_anchor_angle))
    anchor_y = cy + radius * math.sin(math.radians(arrow_anchor_angle))
    # Flèche prolonge vers l'extérieur, pointe à droite (horizontale)
    arrow_length = size * 0.14
    tip_x = anchor_x + arrow_length * math.cos(math.radians(arrow_angle))
    tip_y = anchor_y + arrow_length * math.sin(math.radians(arrow_angle))
    # Trait reliant l'ancre à la pointe
    gdraw.line(
        [(anchor_x, anchor_y), (tip_x, tip_y)],
        fill=CYAN_GLOW, width=glow_stroke,
    )
    _draw_arrow_head(gdraw, (tip_x, tip_y), arrow_angle, size * 0.13, CYAN_GLOW)

    # Blur du calque glow
    glow_blur = max(4, int(size * 0.04))
    glow = glow.filter(ImageFilter.GaussianBlur(glow_blur))

    # ─── Couche nette ────────────────────────────────────────────
    sharp = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(sharp)
    _draw_arc_thick(sdraw, bbox, 30, 340, CYAN, stroke)
    sdraw.line(
        [(anchor_x, anchor_y), (tip_x, tip_y)],
        fill=CYAN, width=stroke,
    )
    _draw_arrow_head(sdraw, (tip_x, tip_y), arrow_angle, size * 0.13, CYAN)

    # Composite : glow d'abord, motif net par-dessus
    img = Image.alpha_composite(img, glow)
    img = Image.alpha_composite(img, sharp)

    return img


def main():
    # Dossier de sortie : <racine_projet>/assets/
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(tools_dir)
    out_dir = os.path.join(project_root, 'assets')
    os.makedirs(out_dir, exist_ok=True)

    # Image haute résolution
    master = render_icon(WORK_SIZE)
    png_path = os.path.join(out_dir, 'icon.png')
    master.save(png_path, 'PNG')
    print(f"Wrote {png_path} ({WORK_SIZE}x{WORK_SIZE})")

    # ICO Windows multi-tailles
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

    # PNG 64x64 pour preview rapide
    preview = master.resize((64, 64), Image.Resampling.LANCZOS)
    preview_path = os.path.join(out_dir, 'icon_64.png')
    preview.save(preview_path, 'PNG')
    print(f"Wrote {preview_path} (64x64)")


if __name__ == '__main__':
    main()
