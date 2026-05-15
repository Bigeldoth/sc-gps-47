"""Fine-tune a PaddleOCR recognition model on the SC HUD font.

Pipeline:
  1. Build a labeled rec dataset via `scripts/prepare_paddle_dataset.py`
     (frame extraction + auto-labeling on a 5-minute gameplay clip).
  2. Download the official PP-OCRv4 English recognition pretrained weights
     (if not already cached).
  3. Run PaddleOCR's training entry point with a generated YAML config.

PaddleOCR training requires the `paddleocr` and `paddlepaddle` packages.
For convenience, this script auto-installs them if they're missing.

Typical usage:
    python tools/train_paddle.py --video gameplay.mp4 --epochs 30 \
        --output models/paddle/rec_finetuned

Once training completes, point the app at the resulting directory by
setting `paddle_model_dir` in the [OCR] section of config.ini (or via the
Options dialog → OCR → Paddle device / model fields).
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

DEFAULT_PRETRAIN_URL = (
    "https://paddleocr.bj.bcebos.com/PP-OCRv4/english/"
    "en_PP-OCRv4_rec_train.tar"
)


def _ensure_paddle_installed() -> None:
    """Installs paddlepaddle + paddleocr if either is missing."""
    import importlib.util
    missing = [
        pkg for pkg in ("paddle", "paddleocr")
        if importlib.util.find_spec(pkg) is None
    ]
    if not missing:
        return
    logger.info("Installing missing packages: %s", missing)
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--upgrade",
        "paddlepaddle", "paddleocr",
    ])


def _build_dataset(video: Path, dataset_dir: Path, max_frames: int) -> None:
    """Delegates to scripts/prepare_paddle_dataset.py."""
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "prepare_paddle_dataset.py"),
        "--video", str(video),
        "--output", str(dataset_dir),
        "--max-frames", str(max_frames),
    ]
    logger.info("Running %s", " ".join(cmd))
    subprocess.check_call(cmd)


def _write_yaml(config_path: Path, dataset_dir: Path, output_dir: Path,
                pretrain_dir: Path | None, epochs: int) -> None:
    """Writes a minimal PP-OCRv4 rec training config.

    The config follows the structure expected by `tools/train.py` from the
    PaddleOCR repository (mirrored upstream — see
    https://github.com/PaddlePaddle/PaddleOCR/blob/main/configs/rec/PP-OCRv4/).
    """
    labels_path = dataset_dir / "labels.txt"
    pretrain_line = f"  pretrained_model: {pretrain_dir}\n" if pretrain_dir else ""
    yaml = f"""\
Global:
  use_gpu: false
  epoch_num: {epochs}
  log_smooth_window: 20
  print_batch_step: 10
  save_model_dir: {output_dir}
  save_epoch_step: 5
  eval_batch_step: [0, 200]
  cal_metric_during_train: true
{pretrain_line}\
  character_dict_path: {labels_path}
  use_space_char: true
  save_inference_dir: {output_dir / "inference"}

Optimizer:
  name: Adam
  lr:
    name: Cosine
    learning_rate: 0.0005

Architecture:
  model_type: rec
  algorithm: SVTR_LCNet
  Transform:
  Backbone:
    name: PPLCNetV3
    scale: 0.95
  Neck:
    name: SequenceEncoder
    encoder_type: svtr
    dims: 120
    depth: 2
    hidden_dims: 120
    kernel_size: [1, 3]
    use_guide: True
  Head:
    name: CTCHead
    fc_decay: 0.00001

Loss:
  name: CTCLoss

PostProcess:
  name: CTCLabelDecode

Metric:
  name: RecMetric
  main_indicator: acc

Train:
  dataset:
    name: SimpleDataSet
    data_dir: {dataset_dir}
    label_file_list: [{dataset_dir / "train.txt"}]
    transforms:
      - DecodeImage: {{img_mode: BGR, channel_first: false}}
      - RecAug:
      - CTCLabelEncode:
      - RecResizeImg: {{image_shape: [3, 48, 320]}}
      - KeepKeys: {{keep_keys: [image, label, length]}}
  loader:
    shuffle: true
    batch_size_per_card: 32
    drop_last: true
    num_workers: 2

Eval:
  dataset:
    name: SimpleDataSet
    data_dir: {dataset_dir}
    label_file_list: [{dataset_dir / "val.txt"}]
    transforms:
      - DecodeImage: {{img_mode: BGR, channel_first: false}}
      - CTCLabelEncode:
      - RecResizeImg: {{image_shape: [3, 48, 320]}}
      - KeepKeys: {{keep_keys: [image, label, length]}}
  loader:
    shuffle: false
    drop_last: false
    batch_size_per_card: 32
    num_workers: 2
"""
    config_path.write_text(yaml, encoding="utf-8")


def _run_training(config_path: Path) -> int:
    """Invokes the PaddleX 3.x training via its Python API.

    The PaddleX CLI exposes pipeline prediction only; module training has
    to go through `build_trainer(parse_config(...))`. We import the model
    register module before parsing so that `'en_PP-OCRv4_mobile_rec'`
    resolves to a known registered model.

    Requires the PaddleOCR plugin to be installed
    (`python -m paddlex --install PaddleOCR -y`).
    """
    try:
        import paddlex.repo_apis.PaddleOCR_api.text_rec.register  # noqa: F401
        from paddlex.utils.config import parse_config
        from paddlex import build_trainer
    except ImportError as exc:
        logger.error(
            "PaddleX text-rec API import failed (%s). Did the PaddleOCR "
            "plugin install? Run `python -m paddlex --install PaddleOCR -y`.",
            exc,
        )
        return 2
    logger.info("Launching training from %s", config_path)
    cfg = parse_config(str(config_path))
    trainer = build_trainer(cfg)
    try:
        trainer.train()
    except Exception:
        logger.exception("Training raised an exception")
        return 1
    return 0


def _write_paddlex_yaml(
    out_yaml: Path,
    dataset_dir: Path,
    output_dir: Path,
    epochs: int,
    base_config: Path,
) -> None:
    """Builds a PaddleX 3.x rec training yaml from the shipped template.

    Loads the official `en_PP-OCRv4_mobile_rec.yaml` ships inside the paddlex
    package, overrides dataset_dir / output / mode / device / epochs, and
    writes the result next to the trained weights so we have a self-contained
    record of how the model was produced.
    """
    import yaml
    cfg = yaml.safe_load(base_config.read_text(encoding="utf-8"))
    cfg["Global"]["mode"] = "train"
    cfg["Global"]["dataset_dir"] = str(dataset_dir)
    cfg["Global"]["output"] = str(output_dir)
    cfg["Global"]["device"] = "cpu"
    cfg["Train"]["epochs_iters"] = epochs
    cfg["Train"]["batch_size"] = 8
    # The shipped config pulls the pretrained URL — keep it; PaddleX caches.
    out_yaml.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", required=True, type=Path,
                   help="Gameplay video used to build the rec dataset")
    p.add_argument("--output", type=Path,
                   default=ROOT / "models" / "paddle" / "rec_finetuned")
    p.add_argument("--dataset-dir", type=Path,
                   default=ROOT / "dataset" / "paddle_rec",
                   help="Where to store the prepared rec dataset")
    p.add_argument("--max-frames", type=int, default=600)
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--pretrain-dir", type=Path, default=None,
                   help="Optional pretrained PP-OCRv4 rec weights directory "
                        "(skip → train from scratch). Download from " + DEFAULT_PRETRAIN_URL)
    p.add_argument("--skip-prepare", action="store_true",
                   help="Re-use an existing dataset in --dataset-dir")
    args = p.parse_args()

    _ensure_paddle_installed()

    if not args.skip_prepare:
        if args.dataset_dir.exists():
            shutil.rmtree(args.dataset_dir)
        _build_dataset(args.video, args.dataset_dir, args.max_frames)

    args.output.mkdir(parents=True, exist_ok=True)
    config_path = args.output / "rec_finetune.yml"
    # Locate the shipped PaddleX template (en_PP-OCRv4_mobile_rec.yaml).
    import paddlex
    base_config = (
        Path(paddlex.__file__).parent
        / "configs" / "modules" / "text_recognition"
        / "en_PP-OCRv4_mobile_rec.yaml"
    )
    if not base_config.exists():
        logger.error("PaddleX template not found at %s", base_config)
        sys.exit(2)
    _write_paddlex_yaml(
        config_path, args.dataset_dir, args.output, args.epochs, base_config,
    )
    logger.info("Wrote training config to %s", config_path)

    rc = _run_training(config_path)
    if rc != 0:
        logger.error("Training exited with code %d", rc)
        sys.exit(rc)

    logger.info(
        "Training complete. To use this model, set paddle_model_dir = %s "
        "in the [OCR] section of config.ini (or via Options → OCR).",
        args.output / "inference",
    )


if __name__ == "__main__":
    main()
