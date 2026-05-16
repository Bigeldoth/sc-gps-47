"""Convert a PaddleOCR rec dataset to the Tesseract LSTM training layout.

Input  (produced by ``scripts/prepare_paddle_dataset.py``):
    <paddle_dir>/
        images/<stem>.png
        train.txt   # `<relpath>\t<transcript>` per line
        val.txt     # same format
        dict.txt    # one character per line

Output (consumed by ``tools/train_tesseract.py``):
    <tess_dir>/
        <stem>.png       # copied from paddle source
        <stem>.gt.txt    # single-line transcript (no trailing newline)
        list.train.txt   # one absolute .lstmf path per line (filled by trainer)
        list.eval.txt    # idem for val split

Tesseract's ``lstm.train`` expects image+gt pairs sitting in the same
directory with matching stems. We flatten the structure (no images/ subdir)
and write `.gt.txt` ground-truth files alongside each crop. The trainer
will later generate `.lstmf` files in place and emit the listfiles itself.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_supported_gt(text: str) -> bool:
    """True if the transcript only contains characters Tesseract's English
    ``eng.traineddata`` is expected to handle. Drops samples Paddle labelled
    with corrupted bytes / inverted glyphs — those would otherwise sit in
    the lstmf with a header that ``lstmtraining`` can't deserialize."""
    if not text:
        return False
    for ch in text:
        # ASCII printable + tab; everything else (control bytes, latin-1
        # supplement, RTL marks, …) is rejected.
        if ch == "\t":
            continue
        if not (0x20 <= ord(ch) < 0x7F):
            return False
    return True


def _parse_split(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if not path.exists():
        return pairs
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip("\r\n")
        if not line or "\t" not in line:
            continue
        relpath, transcript = line.split("\t", 1)
        transcript = transcript.strip()
        if not transcript:
            continue
        if not _is_supported_gt(transcript):
            skipped += 1
            continue
        pairs.append((relpath, transcript))
    if skipped:
        logger.info("Dropped %d non-ASCII transcripts from %s", skipped, path.name)
    return pairs


def _stem_for(relpath: str) -> str:
    """`images/frame_000123_row0.png` → `frame_000123_row0`."""
    return Path(relpath).stem


# Minimum image-width to ground-truth-char ratio. CTC training rescales
# the input to ``LSTM_INPUT_HEIGHT`` then downsamples width by 3× through
# the maxpool stage. For our HUD crops (height ≈ 90), the effective width
# per char in the LSTM domain is roughly ``input_width × (36/90) / 3``,
# and CTC needs at least 2 frames per char. The safe minimum is
# ``input_width / len(gt) ≥ 15``; we use 20 to leave a margin. Below this
# ratio ``lstmtraining`` aborts with "Compute CTC targets failed" every
# iteration. Paddle's auto-labeller sometimes hallucinates 100+ char
# transcripts for a row that physically contains 30 — we drop those.
_MIN_PX_PER_CHAR = 20


def _gt_fits_image(image_path: Path, gt_len: int) -> bool:
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            return im.size[0] >= _MIN_PX_PER_CHAR * gt_len
    except Exception:
        return True  # be permissive if PIL fails — better to keep than drop


def convert(paddle_dir: Path, tess_dir: Path) -> None:
    if not paddle_dir.is_dir():
        raise FileNotFoundError(f"Paddle dataset not found: {paddle_dir}")
    train_pairs = _parse_split(paddle_dir / "train.txt")
    val_pairs = _parse_split(paddle_dir / "val.txt")
    if not train_pairs:
        raise RuntimeError(
            f"No usable lines in {paddle_dir / 'train.txt'} — re-run "
            "prepare_paddle_dataset.py first.")
    tess_dir.mkdir(parents=True, exist_ok=True)

    written_train: list[str] = []
    written_eval: list[str] = []
    seen_stems: set[str] = set()
    ctc_dropped = 0
    for split_name, pairs, sink in (
        ("train", train_pairs, written_train),
        ("eval", val_pairs, written_eval),
    ):
        for relpath, transcript in pairs:
            src_img = paddle_dir / relpath
            if not src_img.is_file():
                logger.warning("Missing image, skipping: %s", src_img)
                continue
            if not _gt_fits_image(src_img, len(transcript)):
                ctc_dropped += 1
                continue
            stem = _stem_for(relpath)
            # Collisions are unlikely (paddle uses frame_XXXX_rowK), but if
            # train and val share a stem the second copy would silently
            # overwrite the first. Suffix on collision rather than lose data.
            if stem in seen_stems:
                stem = f"{stem}_{split_name}"
            seen_stems.add(stem)
            dst_img = tess_dir / f"{stem}.png"
            shutil.copyfile(src_img, dst_img)
            # `gt.txt` must be a single line, no trailing newline (Tesseract
            # treats trailing whitespace as part of the label).
            (tess_dir / f"{stem}.gt.txt").write_text(
                transcript, encoding="utf-8")
            sink.append(stem)

    # Persistent split: just the stems, one per line. `tools/train_tesseract.py`
    # joins these with `<tess_dir>/<stem>.lstmf` after generating the lstmfs.
    # Storing stems (not absolute paths) keeps the split portable.
    (tess_dir / "train.stems.txt").write_text(
        "\n".join(written_train), encoding="utf-8")
    (tess_dir / "eval.stems.txt").write_text(
        "\n".join(written_eval), encoding="utf-8")
    logger.info(
        "Converted %d train + %d eval pairs → %s",
        len(written_train), len(written_eval), tess_dir,
    )
    if ctc_dropped:
        logger.info("Dropped %d samples that would fail CTC (gt too long for image width)", ctc_dropped)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paddle-dir", type=Path, required=True,
                   help="Directory produced by prepare_paddle_dataset.py")
    p.add_argument("--output", type=Path, required=True,
                   help="Destination directory for the Tesseract layout")
    args = p.parse_args()
    convert(args.paddle_dir.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
