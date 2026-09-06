"""Physical display identities shared by capture and the Options dialog."""
from __future__ import annotations

import mss


def list_capture_monitors() -> list[dict]:
    """Return a fresh snapshot of physical displays, excluding the virtual desktop.

    MSS caches topology on each instance, so enumeration uses a short-lived
    instance in the calling thread. Native IDs survive monitor list reordering.
    Backends without IDs use geometry: a changed layout then requires an explicit
    selection instead of silently capturing a different display.
    """
    with mss.mss() as sct:
        monitors = [dict(monitor) for monitor in sct.monitors[1:]]

    result = []
    for monitor in monitors:
        left, top = int(monitor["left"]), int(monitor["top"])
        width, height = int(monitor["width"]), int(monitor["height"])
        if width <= 0 or height <= 0:
            continue
        native_id = str(monitor.get("unique_id") or "").strip()
        monitor_id = (
            f"monitor:{native_id}" if native_id
            else f"geometry:{left}:{top}:{width}:{height}"
        )
        primary = bool(monitor.get("is_primary", left == 0 and top == 0))
        name = str(monitor.get("name") or "Display").strip() or "Display"
        label = f"{name} — {width} × {height} — ({left}, {top})"
        if primary:
            label += " — Primary"
        result.append({
            "id": monitor_id, "label": label,
            "left": left, "top": top, "width": width, "height": height,
            "is_primary": primary,
        })
    return result
