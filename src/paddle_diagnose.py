"""PaddleOCR diagnostic — collects N live HUD captures, runs paddle on each,
saves the raw image, an annotated image with detection boxes, and a JSON
summary suitable for offline analysis (and for Claude to read when iterating
on the model).

Output layout (one folder per run, timestamped):
    diagnostics/paddle/<YYYYMMDD_HHMMSS>/
        summary.md            human-readable summary
        results.json          structured data for tooling
        frame_000_raw.png     captured HUD strip (after crop, before upscale)
        frame_000_annotated.png   upscaled image with detection polys + labels
        ...

The script is callable from the UI (Options → Debug → Run PaddleOCR
diagnostic) via `run_diagnostic()`; it never touches the live OCR worker,
so the running app keeps scanning in parallel.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Default sampling cadence — matches the OCR scan interval so the captures
# are representative of what the live worker would see.
_DEFAULT_INTERVAL_MS = 200
_DEFAULT_MAX_SAMPLES = 20

# Re-use the existing pos regex so we report what the runtime would have
# parsed from each frame. We pull BOTH the strict and the relaxed regex so
# the diagnostic reflects what `_extract_coords_from_line` does at runtime
# (strict first, then relaxed last-coord fallback).
try:
    from ocr import (
        _RE_POS,
        _RE_POS_RELAXED_LAST,
        _RE_POS_HEADLESS,
        _RE_OOC_HINT,
        _coords_in_range,
        _normalize_ooc_line,
        _join_orphan_pos_lines,
        _try_recover_pos_line,
    )
except Exception:  # pragma: no cover - imported in odd test envs
    _RE_POS = re.compile(r"$^")  # never matches
    _RE_POS_RELAXED_LAST = re.compile(r"$^")
    _RE_POS_HEADLESS = re.compile(r"$^")
    _RE_OOC_HINT = re.compile(r"$^")
    def _coords_in_range(*_a): return True
    def _normalize_ooc_line(s: str) -> str: return s
    def _join_orphan_pos_lines(s: str) -> str: return s
    def _try_recover_pos_line(s: str): return None


def _match_pos(line: str):
    """Mirror of OCRProcessor._extract_coords_from_line for offline analysis.

    Returns (coords_tuple, method_used) or (None, None).
    """
    m = _RE_POS.search(line)
    if m:
        try:
            return (float(m.group(1)), float(m.group(2)), float(m.group(3))), "strict"
        except ValueError:
            pass
    m = _RE_POS_RELAXED_LAST.search(line)
    if m:
        try:
            return (float(m.group(1)), float(m.group(2)), float(m.group(3))), "relaxed"
        except ValueError:
            pass
    if _RE_OOC_HINT.search(line):
        m = _RE_POS_HEADLESS.search(line)
        if m:
            try:
                coords = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                if _coords_in_range(*coords):
                    return coords, "headless"
            except ValueError:
                pass
    rec = _try_recover_pos_line(line)
    if rec is not None:
        return rec, "decimal_recovery"
    return None, None


def _annotate(image: np.ndarray, polys, texts, scores) -> np.ndarray:
    """Draws detection polygons + (text, score) labels on a copy of `image`.

    Color encodes confidence:
      ≥ 0.85 → green, 0.60–0.85 → yellow, < 0.60 → red.
    """
    canvas = image.copy()
    # Keep annotated images at most 1800 wide so they're easy to view.
    max_w = 1800
    if canvas.shape[1] > max_w:
        scale = max_w / canvas.shape[1]
        canvas = cv2.resize(
            canvas, (max_w, int(canvas.shape[0] * scale)),
            interpolation=cv2.INTER_LINEAR,
        )
        polys = [
            [(int(x * scale), int(y * scale)) for x, y in poly] for poly in polys
        ]

    for poly, text, score in zip(polys, texts, scores):
        if not poly:
            continue
        if score >= 0.85:
            color = (0, 200, 0)
        elif score >= 0.60:
            color = (0, 200, 200)
        else:
            color = (0, 0, 220)
        pts = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(canvas, [pts], isClosed=True, color=color, thickness=2)
        x0, y0 = poly[0]
        label = f"{text}  ({score:.2f})"
        cv2.putText(
            canvas, label, (x0, max(0, y0 - 4)),
            cv2.FONT_HERSHEY_PLAIN, 1.1, color, 1, cv2.LINE_AA,
        )
    return canvas


def _classify_sample(detections: list[dict]) -> dict:
    """Replays the runtime's regex pipeline to label each sample as
    complete / partial / none with respect to Pos extraction.

    Returns a small summary dict consumed by `aggregate()`.
    """
    # Build the same joined text the runtime parses.
    raw = "\n".join(d["text"] for d in detections)
    joined = _join_orphan_pos_lines(raw)
    matched = None
    match_method = None
    partial_coord_count = 0
    for line in joined.split("\n"):
        norm = _normalize_ooc_line(line)
        coords, method = _match_pos(norm)
        if coords is not None:
            matched = {
                "x": float(coords[0]),
                "y": float(coords[1]),
                "z": float(coords[2]),
                "from_line": line,
                "normalized": norm,
            }
            match_method = method
            break
        # Count partial coord readings (line has 'Pos:'-ish + at least one
        # decimal number) so we can distinguish "1-2 coords detected" from
        # "no coords detected at all". Loosened to accept the Paddle Pos
        # misreads (P0s/P05/P95/Pas) since these reflect partial reads too.
        if re.search(r"[Pp9][0oOaA9qQ][sSa56][:\s]\s*-?\d+\.\d", line) or \
           re.search(r"[Pp]os:?\s*-?\d+\.\d", line):
            count = len(re.findall(r"-?\d+\.\d{2,4}", line))
            partial_coord_count = max(partial_coord_count, count)

    if matched:
        kind = "complete"
    elif partial_coord_count > 0:
        kind = "partial"
    else:
        kind = "none"
    return {
        "outcome": kind,
        "partial_coord_count": partial_coord_count,
        "matched_pos": matched,
        "match_method": match_method,
        "joined_text": joined,
    }


def _score_bucket(score: float) -> str:
    if score >= 0.95:
        return "0.95-1.00"
    if score >= 0.85:
        return "0.85-0.95"
    if score >= 0.70:
        return "0.70-0.85"
    if score >= 0.50:
        return "0.50-0.70"
    return "<0.50"


def _flag_outliers(samples: list[dict]) -> None:
    """Marks samples whose matched Pos is a wild outlier vs the run median.

    Mutates samples in-place by adding a `suspect` field with a short reason
    string. Useful for catching paddle's clipped-leading-sign bug (e.g. X
    flips from -748.279 to +748.279) which would otherwise be reported as a
    "complete" match in the per-sample table.
    """
    coords = [
        (s["matched_pos"]["x"], s["matched_pos"]["y"], s["matched_pos"]["z"])
        for s in samples if s.get("matched_pos")
    ]
    if len(coords) < 3:
        return
    # Median per axis — robust to one or two bad readings.
    sx = sorted(c[0] for c in coords)
    sy = sorted(c[1] for c in coords)
    sz = sorted(c[2] for c in coords)
    med = (sx[len(sx)//2], sy[len(sy)//2], sz[len(sz)//2])
    # A 10 km jump is implausible at 5 fps — game ships move < 5 km/s so two
    # consecutive frames at 200 ms can differ by at most ~1 km.
    threshold_km = 10.0
    for s in samples:
        pos = s.get("matched_pos")
        if not pos:
            continue
        reasons = []
        if abs(pos["x"] - med[0]) > threshold_km:
            reasons.append(f"X drift {pos['x']-med[0]:+.1f} km")
        if abs(pos["y"] - med[1]) > threshold_km:
            reasons.append(f"Y drift {pos['y']-med[1]:+.1f} km")
        if abs(pos["z"] - med[2]) > threshold_km:
            reasons.append(f"Z drift {pos['z']-med[2]:+.1f} km")
        # Sign-flip is the most common silent bug: paddle clips the leading
        # '-' so the value's magnitude matches but the sign is wrong.
        if (pos["x"] * med[0] < 0) and abs(abs(pos["x"]) - abs(med[0])) < 5.0:
            reasons.append("X sign flipped vs median")
        if (pos["y"] * med[1] < 0) and abs(abs(pos["y"]) - abs(med[1])) < 5.0:
            reasons.append("Y sign flipped vs median")
        if (pos["z"] * med[2] < 0) and abs(abs(pos["z"]) - abs(med[2])) < 5.0:
            reasons.append("Z sign flipped vs median")
        if reasons:
            s["suspect"] = "; ".join(reasons)


def aggregate(samples: list[dict]) -> dict:
    """Computes the aggregate stats block included in results.json and
    rendered in summary.md."""
    n = len(samples)
    complete = sum(1 for s in samples if s["pos_outcome"] == "complete")
    partial = sum(1 for s in samples if s["pos_outcome"] == "partial")
    none = sum(1 for s in samples if s["pos_outcome"] == "none")
    suspects = sum(1 for s in samples if s.get("suspect"))
    inf_times = [s["inference_ms"] for s in samples if s["inference_ms"] is not None]
    score_hist: dict[str, int] = {}
    method_hist: dict[str, int] = {}
    text_lengths: list[int] = []
    detection_counts: list[int] = []
    for s in samples:
        detection_counts.append(len(s["detections"]))
        method = s.get("match_method")
        if method:
            method_hist[method] = method_hist.get(method, 0) + 1
        for d in s["detections"]:
            score_hist[_score_bucket(d["score"])] = score_hist.get(
                _score_bucket(d["score"]), 0,
            ) + 1
            text_lengths.append(len(d["text"]))

    def _mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "samples": n,
        "pos_complete": complete,
        "pos_partial": partial,
        "pos_none": none,
        "pos_complete_rate": round(complete / n, 3) if n else 0.0,
        "suspect_samples": suspects,
        "match_method_histogram": method_hist,
        "mean_inference_ms": round(_mean(inf_times), 1),
        "max_inference_ms": round(max(inf_times), 1) if inf_times else 0.0,
        "mean_detections_per_frame": round(_mean(detection_counts), 2),
        "score_histogram": dict(sorted(score_hist.items())),
        "mean_text_length": round(_mean(text_lengths), 1),
    }


def _write_summary_md(out_dir: Path, ctx: dict, agg: dict, samples: list[dict]):
    """Writes a human (and Claude) readable summary."""
    md: list[str] = []
    md.append(f"# Paddle OCR Diagnostic — {ctx['timestamp']}")
    md.append("")
    md.append("## Setup")
    md.append(f"- engine: paddle (device={ctx['device']})")
    md.append(f"- paddle: {ctx.get('paddle_version', '?')}")
    md.append(f"- paddleocr: {ctx.get('paddleocr_version', '?')}")
    md.append(f"- upscale factor: {ctx['upscale']}×")
    md.append(f"- sampling interval: {ctx['interval_ms']} ms")
    md.append(f"- samples requested: {ctx['max_samples']}  /  produced: {agg['samples']}")
    md.append("")
    md.append("## Aggregate")
    md.append(
        f"- Pos extraction: **{agg['pos_complete']}/{agg['samples']} complete**"
        f" ({100*agg['pos_complete_rate']:.1f}%), "
        f"{agg['pos_partial']} partial, {agg['pos_none']} none"
    )
    md.append(
        f"- Mean inference: {agg['mean_inference_ms']} ms "
        f"(max {agg['max_inference_ms']} ms)"
    )
    md.append(f"- Mean detected lines/frame: {agg['mean_detections_per_frame']}")
    md.append(f"- Score histogram: {agg['score_histogram']}")
    if agg.get("match_method_histogram"):
        md.append(f"- Match method histogram: {agg['match_method_histogram']}")
    if agg.get("suspect_samples", 0):
        md.append(
            f"- ⚠ Suspect samples: {agg['suspect_samples']} "
            f"(coords flagged as outliers — see table)"
        )
    md.append("")
    md.append("## Per-sample")
    md.append("")
    md.append("| # | outcome | method | infer ms | #det | min score | matched Pos | suspect |")
    md.append("|---|---------|--------|----------|------|-----------|-------------|---------|")
    for s in samples:
        scores = [d["score"] for d in s["detections"]]
        min_score = min(scores) if scores else 0.0
        pos = s["matched_pos"]
        pos_str = (
            f"({pos['x']:.3f}, {pos['y']:.3f}, {pos['z']:.3f})"
            if pos else "—"
        )
        method = s.get("match_method") or ""
        suspect = s.get("suspect") or ""
        md.append(
            f"| {s['frame_index']} | {s['pos_outcome']} | {method} | "
            f"{s['inference_ms']:.0f} | {len(s['detections'])} | "
            f"{min_score:.2f} | {pos_str} | {suspect} |"
        )
    md.append("")
    md.append("## Detected text (per sample)")
    md.append("")
    for s in samples:
        md.append(f"### Frame {s['frame_index']} — {s['pos_outcome']}")
        md.append(f"`{s['raw_image_path']}`  →  `{s['annotated_image_path']}`")
        md.append("")
        for d in s["detections"]:
            md.append(f"- `{d['text']}`   _(score {d['score']:.2f})_")
        if s["joined_text"]:
            md.append("")
            md.append("Joined (after orphan-Pos repair):")
            md.append("```")
            md.append(s["joined_text"])
            md.append("```")
        md.append("")

    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")


def run_diagnostic(
    output_root: str | Path = "diagnostics/paddle",
    max_samples: int = _DEFAULT_MAX_SAMPLES,
    interval_ms: int = _DEFAULT_INTERVAL_MS,
    device: str = "gpu",
    progress: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Captures up to `max_samples` HUD strips and runs paddle on each.

    Args:
        output_root: parent directory. A `<YYYYMMDD_HHMMSS>` subfolder is
            created for the run. Returned.
        max_samples: cap on captures (the loop also stops on the first OCR
            error or on `progress` returning False).
        interval_ms: delay between consecutive captures.
        device: 'gpu' or 'cpu' — same semantics as the runtime config.
        progress: optional callback `(idx, total, message)` for UI updates.

    Returns the path to the timestamped output directory.
    """
    # Local imports — keep module-import cheap when paddle isn't used.
    from capture import ScreenCapture
    from paddle_adapter import PaddleAdapter

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(output_root) / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("PaddleOCR diagnostic: output → %s", out_dir)

    if progress:
        progress(0, max_samples, "Initialising PaddleOCR…")
    adapter = PaddleAdapter(device=device)
    capture = ScreenCapture()

    # Best-effort version stamps for context.
    try:
        import paddle  # type: ignore
        paddle_version = paddle.__version__
    except Exception:
        paddle_version = "?"
    try:
        import paddleocr  # type: ignore
        paddleocr_version = paddleocr.__version__
    except Exception:
        paddleocr_version = "?"

    ctx = {
        "timestamp": ts,
        "device": device,
        "upscale": 3,  # mirror PaddleAdapter's _UPSCALE_FACTOR
        "interval_ms": interval_ms,
        "max_samples": max_samples,
        "paddle_version": paddle_version,
        "paddleocr_version": paddleocr_version,
    }

    samples: list[dict] = []
    for i in range(max_samples):
        if progress:
            progress(i, max_samples, f"Capturing frame {i+1}/{max_samples}…")
        images, _glyph = capture.capture()
        raw = images.get("raw")
        if raw is None:
            logger.warning("diagnostic: no 'raw' image in capture dict — aborting")
            break

        t0 = time.perf_counter()
        detail = adapter.recognize_detailed(raw)
        dt_ms = (time.perf_counter() - t0) * 1000.0

        # Build the per-detection records.
        detections = []
        for t, s, p in zip(detail["texts"], detail["scores"], detail["polys"]):
            detections.append({"text": t, "score": float(s), "poly": p})

        # Persist images (raw HUD strip + annotated upscaled view).
        raw_path = out_dir / f"frame_{i:03d}_raw.png"
        ann_path = out_dir / f"frame_{i:03d}_annotated.png"
        cv2.imwrite(str(raw_path), raw)
        upscaled = cv2.resize(
            raw,
            (raw.shape[1] * detail["upscale"], raw.shape[0] * detail["upscale"]),
            interpolation=cv2.INTER_LINEAR,
        ) if detail["upscale"] != 1 else raw
        annotated = _annotate(
            upscaled, detail["polys"], detail["texts"], detail["scores"],
        )
        cv2.imwrite(str(ann_path), annotated)

        clf = _classify_sample(detections)
        samples.append({
            "frame_index": i,
            "raw_image_path": raw_path.name,
            "annotated_image_path": ann_path.name,
            "raw_shape": list(raw.shape),
            "upscale": detail["upscale"],
            "inference_ms": dt_ms,
            "detections": detections,
            "pos_outcome": clf["outcome"],
            "partial_coord_count": clf["partial_coord_count"],
            "matched_pos": clf["matched_pos"],
            "match_method": clf.get("match_method"),
            "joined_text": clf["joined_text"],
        })

        if progress:
            keep_going = progress(
                i + 1, max_samples,
                f"Frame {i+1}/{max_samples}: {clf['outcome']} "
                f"({dt_ms:.0f} ms, {len(detections)} lines)",
            )
            if keep_going is False:
                logger.info("diagnostic: cancelled by caller after %d frames", i + 1)
                break

        if interval_ms > 0:
            time.sleep(interval_ms / 1000.0)

    _flag_outliers(samples)
    agg = aggregate(samples)
    payload = {"context": ctx, "aggregate": agg, "samples": samples}
    (out_dir / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_summary_md(out_dir, ctx, agg, samples)
    logger.info(
        "PaddleOCR diagnostic done: %d samples, %d/%d complete Pos → %s",
        agg["samples"], agg["pos_complete"], agg["samples"], out_dir,
    )
    return out_dir
