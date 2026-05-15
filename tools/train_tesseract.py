"""Fine-tune a Tesseract LSTM model on the SC HUD font.

Mirrors the spirit of ``tools/train_paddle.py`` but produces a
``.traineddata`` instead of a PaddleX inference dir. Pipeline:

  1. Locate ``tesseract.exe`` / ``lstmtraining.exe`` / ``combine_tessdata.exe``
     plus a base ``eng.traineddata`` (from a stock Tesseract 5 install).
  2. If the user passed a ``--video``, build a Paddle-format dataset via
     ``scripts/prepare_paddle_dataset.py`` and convert it to the Tesseract
     layout via ``scripts/paddle_to_tesseract_dataset.py``.
  3. Generate per-pair ``.lstmf`` files: ``tesseract <img> <stem>
     --psm 7 lstm.train`` (Tesseract reads the matching ``<stem>.gt.txt``).
  4. Extract the LSTM head from ``eng.traineddata`` via ``combine_tessdata
     -e``, then run ``lstmtraining --continue_from eng.lstm
     --train_listfile … --eval_listfile … --max_iterations N``.
  5. Finalise with ``lstmtraining --stop_training`` to package the
     ``spacedrive.traineddata`` consumed at runtime.

The output is committed at ``models/tessdata/spacedrive.traineddata`` so
the GPS works out of the box for end users (cf. plan §"Livraison
out-of-the-box"). Re-run only when retraining on new gameplay.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

# Candidate locations for the Tesseract binaries on Windows / POSIX.
_TESSERACT_DIR_CANDIDATES = [
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    Path("/opt/homebrew/share/tessdata").parent,
    Path("/usr/local/share/tessdata").parent,
    Path("/usr/share/tesseract-ocr/5/tessdata").parent,
]


def _find_tess_binary(name: str) -> Path:
    """Locate a tesseract training binary on PATH or in standard install dirs."""
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)
    suffixes = (".exe", "") if sys.platform == "win32" else ("",)
    for base in _TESSERACT_DIR_CANDIDATES:
        for sfx in suffixes:
            candidate = base / f"{name}{sfx}"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        f"Cannot locate {name}. Install Tesseract 5 with the training "
        "tools (UB-Mannheim Windows installer ships them by default; "
        "Linux: apt install tesseract-ocr libtesseract-dev tesseract-ocr-eng "
        "+ build lstmtraining from source if your distro doesn't package it)."
    )


def _find_eng_traineddata() -> Path:
    """Locate the base eng.traineddata to fine-tune from."""
    for base in _TESSERACT_DIR_CANDIDATES:
        candidate = base / "tessdata" / "eng.traineddata"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Cannot locate eng.traineddata. Install Tesseract 5 + the eng "
        "language pack, then re-run."
    )


def _build_paddle_dataset(video: Path, paddle_dir: Path, max_frames: int) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "prepare_paddle_dataset.py"),
        "--video", str(video),
        "--output", str(paddle_dir),
        "--max-frames", str(max_frames),
    ]
    logger.info("Building Paddle dataset: %s", " ".join(cmd))
    subprocess.check_call(cmd)


def _convert_to_tesseract(paddle_dir: Path, tess_dir: Path) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "paddle_to_tesseract_dataset.py"),
        "--paddle-dir", str(paddle_dir),
        "--output", str(tess_dir),
    ]
    logger.info("Converting to Tesseract layout: %s", " ".join(cmd))
    subprocess.check_call(cmd)


def _write_line_box(image_path: Path, gt_text: str) -> bool:
    """Generates a `.box` file alongside the image.

    Tesseract's ``lstm.train`` mode does not consume ``.gt.txt`` directly —
    it requires a per-character box file in the format used by tesstrain.
    For a single-line crop we use a naive horizontal split: each character
    spans an equal slice of the image width. CTC alignment during training
    refines the actual character positions, so the exact box geometry
    isn't critical — the format is the gate.

    The file ends with a tab-newline ``\\n\\t 0 0 W H 0`` sentinel that
    marks the end of the line, per Tesseract convention.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.error("Pillow not installed (pip install Pillow). Aborting box gen.")
        return False
    with Image.open(image_path) as im:
        w, h = im.size
    chars = [c for c in gt_text if c != "\n"]
    if not chars:
        return False
    slice_w = max(1.0, w / len(chars))
    lines = []
    for i, ch in enumerate(chars):
        x0 = int(round(i * slice_w))
        x1 = int(round((i + 1) * slice_w))
        if x1 <= x0:
            x1 = x0 + 1
        # Box format: char x0 y0 x1 y1 page. Tesseract uses
        # bottom-left origin in box files, so y0=0 (bottom), y1=h (top).
        lines.append(f"{ch} {x0} 0 {x1} {h} 0")
    lines.append(f"\t {w} 0 {w} {h} 0")  # end-of-line sentinel
    image_path.with_suffix(".box").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _make_lstmf_files(
    tesseract_bin: Path, tess_dir: Path, tessdata_dir: Path,
) -> tuple[int, int]:
    """Generates a .lstmf next to every <stem>.png + <stem>.gt.txt pair.

    Two steps per pair: write the matching `.box` file, then run
    ``tesseract <img> <stem> --psm 13 lstm.train`` which emits ``<stem>.lstmf``.
    Must use the SAME ``eng.traineddata`` (float) as the trainer — passing a
    custom ``--tessdata-dir`` ensures the lstmf is binary-compatible.
    Returns (n_success, n_failure).
    """
    images = sorted(tess_dir.glob("*.png"))
    ok = 0
    failed = 0
    for img in images:
        gt = img.with_suffix(".gt.txt")
        if not gt.is_file():
            failed += 1
            continue
        gt_text = gt.read_text(encoding="utf-8").strip()
        if not gt_text:
            failed += 1
            continue
        if not _write_line_box(img, gt_text):
            failed += 1
            continue
        stem = img.with_suffix("")  # path/<stem> (no extension)
        cmd = [
            str(tesseract_bin), str(img), str(stem),
            "--tessdata-dir", str(tessdata_dir),
            "-l", "eng",
            "--psm", "13", "lstm.train",
        ]
        try:
            subprocess.check_call(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            ok += 1
        except subprocess.CalledProcessError:
            failed += 1
    logger.info(".lstmf generated: %d ok / %d failed", ok, failed)
    return ok, failed


def _refresh_listfiles(tess_dir: Path) -> None:
    """Rebuild ``list.train.txt`` / ``list.eval.txt`` from the persistent
    ``train.stems.txt`` / ``eval.stems.txt`` split, keeping only stems that
    actually have a `.lstmf` on disk."""
    for split, list_name in (
        ("train.stems.txt", "list.train.txt"),
        ("eval.stems.txt", "list.eval.txt"),
    ):
        src = tess_dir / split
        dst = tess_dir / list_name
        if not src.is_file():
            dst.write_text("", encoding="utf-8")
            continue
        kept = []
        for stem in src.read_text(encoding="utf-8").splitlines():
            stem = stem.strip()
            if not stem:
                continue
            lstmf = (tess_dir / f"{stem}.lstmf").resolve()
            if lstmf.is_file():
                kept.append(str(lstmf))
        # Write bytes directly with LF endings. Python on Windows otherwise
        # promotes "\n" to "\r\n" in text mode, and lstmtraining keeps the
        # trailing "\r" as part of the filename — every entry then fails
        # with "Deserialize header failed".
        dst.write_bytes(("\n".join(kept) + "\n").encode("utf-8"))
        logger.info("%s: %d entries", list_name, len(kept))


_TESSDATA_BEST_URL = (
    "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata"
)


def _copy_configs(system_tessdata: Path, work_tessdata: Path) -> None:
    """Mirror the ``configs/`` subdir from the system tessdata into our work
    dir so ``--tessdata-dir <work>`` resolves the ``lstm.train`` config.

    Tesseract resolves config keywords (``lstm.train``, ``box.train``…) by
    looking in ``<tessdata-dir>/configs/<name>``. If we only ship our own
    ``eng.traineddata`` without the configs we get the misleading
    ``read_params_file: Can't open lstm.train`` error.
    """
    src = system_tessdata / "configs"
    dst = work_tessdata / "configs"
    if dst.is_dir():
        return
    if not src.is_dir():
        raise FileNotFoundError(
            f"Tesseract configs/ dir not found at {src}. "
            "Reinstall Tesseract with training tools."
        )
    shutil.copytree(src, dst)
    logger.info("Mirrored tesseract configs/ → %s", dst)


def _ensure_best_traineddata(system_td: Path, out_dir: Path) -> Path:
    """Returns a path to the *float* eng.traineddata required for training.

    The UB-Mannheim Windows installer (and most distros) ships the
    integer-quantised ``fast`` model, which ``lstmtraining`` rejects with
    "integer (fast) model, cannot continue training". We download the
    float build from ``tessdata_best`` once and cache it locally.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    local_td = out_dir / "eng.traineddata"
    # tessdata_best is ~30 MB, fast is ~4 MB. Use size as a quick heuristic
    # before paying a download.
    if system_td.stat().st_size > 10 * 1024 * 1024:
        shutil.copyfile(system_td, local_td)
        logger.info("Reusing system eng.traineddata (%.1f MB) as float model.",
                    system_td.stat().st_size / 1e6)
        _copy_configs(system_td.parent, out_dir)
        return local_td
    if local_td.is_file() and local_td.stat().st_size > 10 * 1024 * 1024:
        logger.info("Reusing cached tessdata_best/eng.traineddata at %s", local_td)
        _copy_configs(system_td.parent, out_dir)
        return local_td
    logger.info("System eng.traineddata is the 'fast' build — downloading "
                "tessdata_best (~30 MB) from %s", _TESSDATA_BEST_URL)
    import urllib.request
    urllib.request.urlretrieve(_TESSDATA_BEST_URL, str(local_td))
    if local_td.stat().st_size < 10 * 1024 * 1024:
        raise RuntimeError(
            f"Downloaded eng.traineddata is suspiciously small "
            f"({local_td.stat().st_size} bytes). Check network."
        )
    logger.info("Downloaded %.1f MB", local_td.stat().st_size / 1e6)
    _copy_configs(system_td.parent, out_dir)
    return local_td


def _extract_lstm(combine_bin: Path, eng_traineddata: Path, out_dir: Path) -> Path:
    """Extract the LSTM weights from eng.traineddata for ``--continue_from``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(combine_bin), "-e", str(eng_traineddata), str(out_dir / "eng.lstm")]
    logger.info("Extracting LSTM: %s", " ".join(cmd))
    subprocess.check_call(cmd)
    return out_dir / "eng.lstm"


def _run_lstmtraining(
    lstmtraining_bin: Path,
    eng_traineddata: Path,
    eng_lstm: Path,
    list_train: Path,
    list_eval: Path,
    model_output_base: Path,
    max_iterations: int,
) -> int:
    cmd = [
        str(lstmtraining_bin),
        "--continue_from", str(eng_lstm),
        "--traineddata", str(eng_traineddata),
        "--model_output", str(model_output_base),
        "--train_listfile", str(list_train),
        "--max_iterations", str(max_iterations),
        "--target_error_rate", "0.01",
    ]
    if list_eval.is_file() and list_eval.stat().st_size > 0:
        cmd.extend(["--eval_listfile", str(list_eval)])
    logger.info("Training: %s", " ".join(cmd))
    return subprocess.call(cmd)


def _finalize(
    lstmtraining_bin: Path,
    eng_traineddata: Path,
    checkpoint: Path,
    out_traineddata: Path,
) -> int:
    out_traineddata.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(lstmtraining_bin),
        "--stop_training",
        "--continue_from", str(checkpoint),
        "--traineddata", str(eng_traineddata),
        "--model_output", str(out_traineddata),
    ]
    logger.info("Finalising: %s", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", type=Path, default=None,
                   help="Gameplay video. Required unless --skip-prepare AND "
                        "--tess-dir already contain pairs.")
    p.add_argument("--paddle-dir", type=Path,
                   default=ROOT / "dataset" / "paddle_rec",
                   help="Intermediate Paddle dataset (built unless reused)")
    p.add_argument("--tess-dir", type=Path,
                   default=ROOT / "dataset" / "tesseract",
                   help="Tesseract pair directory")
    p.add_argument("--output", type=Path,
                   default=ROOT / "models" / "tessdata" / "spacedrive.traineddata")
    p.add_argument("--max-frames", type=int, default=1000)
    p.add_argument("--max-iterations", type=int, default=4000)
    p.add_argument("--skip-prepare", action="store_true",
                   help="Re-use existing --tess-dir contents (skip paddle "
                        "build + conversion).")
    args = p.parse_args()

    tesseract_bin = _find_tess_binary("tesseract")
    lstmtraining_bin = _find_tess_binary("lstmtraining")
    combine_bin = _find_tess_binary("combine_tessdata")
    eng_traineddata = _find_eng_traineddata()
    logger.info(
        "Tools: tesseract=%s lstmtraining=%s combine_tessdata=%s eng=%s",
        tesseract_bin, lstmtraining_bin, combine_bin, eng_traineddata,
    )

    if not args.skip_prepare:
        if args.video is None or not args.video.is_file():
            logger.error("--video required unless --skip-prepare is set.")
            return 2
        if args.paddle_dir.exists():
            shutil.rmtree(args.paddle_dir)
        _build_paddle_dataset(args.video, args.paddle_dir, args.max_frames)
        if args.tess_dir.exists():
            shutil.rmtree(args.tess_dir)
        _convert_to_tesseract(args.paddle_dir, args.tess_dir)
    elif not args.tess_dir.is_dir():
        logger.error("--skip-prepare set but %s does not exist.", args.tess_dir)
        return 2

    # Ensure the float `eng.traineddata` is ready BEFORE generating lstmf
    # so both steps use the same model version (mixing fast/float models
    # makes lstmf headers undeserialisable by lstmtraining).
    work_dir = args.output.parent / "_train_work"
    local_eng = _ensure_best_traineddata(eng_traineddata, work_dir)
    rm_count = 0
    for stale in args.tess_dir.glob("*.lstmf"):
        stale.unlink()
        rm_count += 1
    if rm_count:
        logger.info("Removed %d stale .lstmf files", rm_count)

    ok, failed = _make_lstmf_files(tesseract_bin, args.tess_dir, work_dir)
    if ok == 0:
        logger.error("No .lstmf files generated — aborting.")
        return 3
    _refresh_listfiles(args.tess_dir)

    eng_lstm = _extract_lstm(combine_bin, local_eng, work_dir)
    model_base = work_dir / "spacedrive"

    rc = _run_lstmtraining(
        lstmtraining_bin,
        local_eng,
        eng_lstm,
        args.tess_dir / "list.train.txt",
        args.tess_dir / "list.eval.txt",
        model_base,
        args.max_iterations,
    )
    if rc != 0:
        logger.error("lstmtraining exited with code %d", rc)
        return rc

    # The latest checkpoint sits at <model_base>_checkpoint.
    checkpoint = Path(str(model_base) + "_checkpoint")
    if not checkpoint.is_file():
        logger.error("Checkpoint not found at %s", checkpoint)
        return 4

    rc = _finalize(lstmtraining_bin, local_eng, checkpoint, args.output)
    if rc != 0:
        logger.error("Finalisation exited with code %d", rc)
        return rc

    logger.info(
        "Training complete. To use this model, set tesseract_lang = "
        "spacedrive in the [OCR] section of config.ini. "
        "Drop %s into a folder named `tessdata/` and point "
        "tesseract_tessdata_dir at that folder (or leave it next to the "
        "shipped one — config_manager picks it up automatically).",
        args.output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
