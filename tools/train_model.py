"""Lightweight CNN training + ONNX export for SpaceDrive glyph OCR.

Pipeline:
  1. Load glyphs from dataset/raw/{char_safe}/ (real) and
     dataset/synthetic/{char_safe}/ (generated). Ignore dataset/raw/_to_review/.
  2. Weighting `real_weight` (default 3): favor target domain (real video).
  3. Augmentations: translation/scale/rotation, blur, brightness/contrast,
     Gaussian bloom, additive noise.
  4. TinyGlyphCNN architecture: ~25k params, input 1x24x16, output N classes.
  5. Train (AdamW + cosine LR), export ONNX opset 17.
  6. Sanity check: compare PyTorch vs ONNXRuntime accuracy on validation set.

Usage:
    python tools/train_model.py --data-real dataset/raw --data-synth dataset/synthetic \\
        --out models/ --epochs 30 --batch-size 64
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.dataset_builder import _PATH_SAFE_MAP  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_model")

GLYPH_W = 16
GLYPH_H = 24

# Inverse of _PATH_SAFE_MAP: folder name -> original character.
_DIR_TO_CHAR: dict[str, str] = {v: k for k, v in _PATH_SAFE_MAP.items()}


def decode_dir_name(name: str) -> str:
    """Convert safe folder name back to original character."""
    return _DIR_TO_CHAR.get(name, name)


@dataclass
class Sample:
    path: Path
    label: int
    is_real: bool


class SpaceDriveGlyphDataset(Dataset):
    """Dataset of 16x24 binary glyphs with training augmentations."""

    def __init__(
        self,
        samples: list[Sample],
        classes: list[str],
        training: bool,
    ) -> None:
        self.samples = samples
        self.classes = classes
        self.training = training

    def __len__(self) -> int:
        return len(self.samples)

    def _load(self, path: Path) -> np.ndarray:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            return np.zeros((GLYPH_H, GLYPH_W), dtype=np.uint8)
        if img.shape != (GLYPH_H, GLYPH_W):
            img = cv2.resize(img, (GLYPH_W, GLYPH_H), interpolation=cv2.INTER_LINEAR)
        return img

    def _augment(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape

        angle = random.uniform(-2.0, 2.0)
        scale = random.uniform(0.9, 1.1)
        tx = random.uniform(-1.5, 1.5)
        ty = random.uniform(-1.0, 1.0)
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, scale)
        m[0, 2] += tx
        m[1, 2] += ty
        img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)

        f = img.astype(np.float32)

        if random.random() < 0.4:
            sigma = random.uniform(0.3, 1.2)
            f = cv2.GaussianBlur(f, (0, 0), sigmaX=sigma)

        if random.random() < 0.5:
            bloom = cv2.GaussianBlur(f, (0, 0), sigmaX=random.uniform(1.5, 3.5))
            f = np.clip(f + bloom * random.uniform(0.2, 0.6), 0, 255)

        f = np.clip(f * random.uniform(0.6, 1.4) + random.uniform(-25, 25), 0, 255)

        if random.random() < 0.5:
            f = np.clip(f + np.random.randn(h, w) * random.uniform(2, 12), 0, 255)

        return f

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, int]:
        sample = self.samples[idx]
        img = self._load(sample.path)
        if self.training:
            arr = self._augment(img)
        else:
            arr = img.astype(np.float32)
        tensor = torch.from_numpy(arr).unsqueeze(0) / 255.0  # (1, H, W)
        return tensor.float(), sample.label


def scan_dataset(real_root: Path, synth_root: Path) -> tuple[list[Sample], list[str]]:
    """Scan raw/ + synthetic/ directories and build sample list."""
    discovered: dict[str, list[tuple[Path, bool]]] = {}

    for root, is_real in ((real_root, True), (synth_root, False)):
        if not root.is_dir():
            logger.warning("Directory missing: %s", root)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("_"):  # ignore _to_review, _trash, etc.
                continue
            char = decode_dir_name(child.name)
            files = [p for p in child.iterdir() if p.suffix.lower() in {".png", ".jpg", ".bmp"}]
            if not files:
                continue
            discovered.setdefault(char, []).extend((p, is_real) for p in files)

    classes = sorted(discovered.keys())
    samples: list[Sample] = []
    for label, char in enumerate(classes):
        for path, is_real in discovered[char]:
            samples.append(Sample(path=path, label=label, is_real=is_real))

    return samples, classes


def stratified_split(
    samples: list[Sample],
    val_ratio: float,
    seed: int,
) -> tuple[list[Sample], list[Sample]]:
    """Deterministic split by class (without sklearn)."""
    rng = random.Random(seed)
    by_label: dict[int, list[Sample]] = {}
    for s in samples:
        by_label.setdefault(s.label, []).append(s)

    train, val = [], []
    for label, group in by_label.items():
        rng.shuffle(group)
        n_val = max(1, int(len(group) * val_ratio)) if len(group) > 1 else 0
        val.extend(group[:n_val])
        train.extend(group[n_val:])
    return train, val


class TinyGlyphCNN(nn.Module):
    """Compact architecture ~25k params, designed for 16x24 glyphs."""

    def __init__(self, num_classes: int) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, 3, padding=1)
        self.bn1 = nn.BatchNorm2d(16)
        self.conv2 = nn.Conv2d(16, 32, 3, padding=1)
        self.bn2 = nn.BatchNorm2d(32)
        self.conv3 = nn.Conv2d(32, 64, 3, padding=1)
        self.bn3 = nn.BatchNorm2d(64)
        self.pool = nn.MaxPool2d(2)
        self.dropout = nn.Dropout(0.2)
        self.fc = nn.Linear(64, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.pool(F.relu(self.bn1(self.conv1(x))))    # 24x16 -> 12x8
        x = self.pool(F.relu(self.bn2(self.conv2(x))))    # 12x8  -> 6x4
        x = F.relu(self.bn3(self.conv3(x)))               # 6x4
        x = F.adaptive_avg_pool2d(x, 1).flatten(1)        # (B, 64)
        x = self.dropout(x)
        return self.fc(x)


def build_sampler(train_samples: list[Sample], real_weight: float) -> WeightedRandomSampler:
    """Weight each real sample × real_weight to favor target domain."""
    weights = torch.tensor([real_weight if s.is_real else 1.0 for s in train_samples],
                           dtype=torch.double)
    return WeightedRandomSampler(weights, num_samples=len(train_samples), replacement=True)


@torch.no_grad()
def evaluate(model: nn.Module, loader: DataLoader, device: torch.device) -> float:
    model.eval()
    correct = total = 0
    for x, y in loader:
        x = x.to(device)
        y = y.to(device)
        logits = model(x)
        preds = logits.argmax(dim=1)
        correct += int((preds == y).sum())
        total += int(y.numel())
    return correct / max(1, total)


def export_onnx(model: nn.Module, num_classes: int, path: Path) -> None:
    model.eval()
    dummy = torch.randn(1, 1, GLYPH_H, GLYPH_W)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        model.cpu(),
        dummy,
        str(path),
        input_names=["input"],
        output_names=["logits"],
        opset_version=17,
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        dynamo=False,  # legacy TorchScript exporter — does not require onnxscript
    )
    logger.info("ONNX model exported: %s (size %.1f KB)", path, path.stat().st_size / 1024)
    _ = num_classes


def onnx_validate(onnx_path: Path, val_ds: Dataset, classes: list[str]) -> float:
    """Sanity check: accuracy via onnxruntime on validation set."""
    try:
        import onnxruntime as ort
    except ImportError:
        logger.warning("onnxruntime unavailable — ONNX sanity check skipped.")
        return -1.0

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    correct = total = 0
    for i in range(len(val_ds)):
        x, y = val_ds[i]
        out = sess.run(None, {"input": x.unsqueeze(0).numpy()})[0]
        pred = int(np.argmax(out, axis=1)[0])
        correct += int(pred == y)
        total += 1
    acc = correct / max(1, total)
    logger.info("ONNX accuracy on val: %.4f  (%d classes)", acc, len(classes))
    return acc


def train(args: argparse.Namespace) -> int:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    real_root = Path(args.data_real)
    synth_root = Path(args.data_synth)
    samples, classes = scan_dataset(real_root, synth_root)

    if not samples:
        logger.error("No samples found. Run dataset_builder.py / dataset_synthetic.py first.")
        return 1
    if len(classes) < 2:
        logger.error("At least 2 classes required (found: %s).", classes)
        return 1

    train_s, val_s = stratified_split(samples, args.val_ratio, args.seed)
    logger.info("Classes (%d): %s", len(classes), classes)

    val_ds = SpaceDriveGlyphDataset(val_s, classes, training=False)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / "spacedrive_ocr.pt"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = TinyGlyphCNN(num_classes=len(classes)).to(device)

    # Export-only mode: load existing checkpoint and skip training
    if args.export_only:
        if not ckpt_path.is_file():
            logger.error("--export-only requested but %s not found.", ckpt_path)
            return 1
        model.load_state_dict(torch.load(str(ckpt_path), map_location="cpu"))
        logger.info("Checkpoint loaded: %s  (export-only)", ckpt_path)
        best_acc = evaluate(model, DataLoader(val_ds, batch_size=64), device)
        logger.info("val_acc on checkpoint: %.4f", best_acc)
    else:
        n_real = sum(1 for s in samples if s.is_real)
        logger.info("Train: %d samples  |  Val: %d samples", len(train_s), len(val_s))
        logger.info("Of which %d real, %d synthetic (real_weight=%.1f).",
                    n_real, len(samples) - n_real, args.real_weight)

        train_ds = SpaceDriveGlyphDataset(train_s, classes, training=True)
        sampler = build_sampler(train_s, args.real_weight)
        train_loader = DataLoader(train_ds, batch_size=args.batch_size, sampler=sampler,
                                  num_workers=0, pin_memory=False)
        val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False,
                                num_workers=0, pin_memory=False)
        n_params = sum(p.numel() for p in model.parameters())
        logger.info("TinyGlyphCNN  params=%d  device=%s", n_params, device)

        optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
        criterion = nn.CrossEntropyLoss()
        best_acc = 0.0

        for epoch in range(1, args.epochs + 1):
            model.train()
            loss_sum = n_batches = 0
            for x, y in train_loader:
                x, y = x.to(device), y.to(device)
                optimizer.zero_grad()
                loss = criterion(model(x), y)
                loss.backward()
                optimizer.step()
                loss_sum += float(loss)
                n_batches += 1
            scheduler.step()

            val_acc = evaluate(model, val_loader, device)
            logger.info("Epoch %02d/%d  loss=%.4f  val_acc=%.4f  lr=%.2e",
                        epoch, args.epochs, loss_sum / max(1, n_batches),
                        val_acc, scheduler.get_last_lr()[0])

            if val_acc > best_acc:
                best_acc = val_acc
                torch.save(model.state_dict(), ckpt_path)

        logger.info("Best val_acc: %.4f", best_acc)
        model.load_state_dict(torch.load(str(ckpt_path), map_location="cpu"))

    onnx_path = out_dir / "spacedrive_ocr.onnx"
    export_onnx(model, len(classes), onnx_path)

    classes_path = out_dir / "spacedrive_ocr.classes.json"
    classes_path.write_text(json.dumps({"classes": classes}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    logger.info("Classes mapping: %s", classes_path)

    onnx_acc = onnx_validate(onnx_path, val_ds, classes)
    if onnx_acc >= 0 and abs(onnx_acc - best_acc) > 0.01:
        logger.warning("torch/onnx gap > 1%%: torch=%.4f  onnx=%.4f", best_acc, onnx_acc)

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-real", default="dataset/raw", type=str)
    p.add_argument("--data-synth", default="dataset/synthetic", type=str)
    p.add_argument("--out", default="models", type=str)
    p.add_argument("--epochs", default=30, type=int)
    p.add_argument("--batch-size", default=64, type=int)
    p.add_argument("--lr", default=1e-3, type=float)
    p.add_argument("--val-ratio", default=0.15, type=float)
    p.add_argument("--real-weight", default=3.0, type=float)
    p.add_argument("--seed", default=42, type=int)
    p.add_argument("--export-only", action="store_true", dest="export_only",
                   help="Load existing models/spacedrive_ocr.pt and export ONNX without retraining.")
    return p


if __name__ == "__main__":
    raise SystemExit(train(build_parser().parse_args()))
