"""Figure-type classifier (PyTorch) — routes cropped figures before vision captioning.

Why this exists
---------------
GPT-4 Vision calls are the most expensive step in the pipeline. Not every
extracted figure deserves one: photos and decorative images add cost but no
retrieval value, while diagrams, schematics, tables, and charts are exactly
what users ask about. This module classifies each cropped figure PNG
(produced by ``shared.pdf_crop.crop_figure_png_b64``) into one of four
types so the pre-analyze step can route them:

    diagram / chart / table  -> send to GPT-4 Vision for description + OCR
    photo                    -> index thumbnail metadata only (skip Vision)

Design
------
* **Backbone**: torchvision ResNet-18 pretrained on ImageNet, used as a
  frozen feature extractor (512-d embeddings). Small, CPU-friendly - this
  runs in the local pre-analyze path (scripts/preanalyze.py), not inside
  the Azure Function runtime.
* **Head**: nearest-centroid over class embeddings computed from a small
  labeled seed set (see ``seed/`` folder or build your own with
  ``build_centroids``). Nearest-centroid keeps the model transparent,
  trivially retrainable, and dependency-light - no training loop needed
  to bootstrap.
* **Fallback**: if torch is not installed, ``classify_figure`` degrades to
  a cheap luminance-histogram heuristic (photos have smooth histograms;
  line art is bimodal), so the pipeline never hard-fails on the optional
  dependency.

Usage
-----
    from shared.figure_classifier import FigureClassifier

    clf = FigureClassifier()                  # loads centroids if present
    kind, confidence = clf.classify(png_bytes)
    if kind in ("diagram", "chart", "table") or confidence < 0.55:
        describe_with_vision(png_bytes)       # low confidence -> be safe
"""

from __future__ import annotations

import io
import json
import logging
import math
from pathlib import Path
from typing import Tuple

from PIL import Image

log = logging.getLogger(__name__)

CLASSES = ("diagram", "chart", "table", "photo")
_CENTROID_FILE = Path(__file__).with_name("figure_centroids.json")

try:  # torch is an optional, local-only dependency (see requirements.txt)
    import torch
    import torchvision.models as tvm
    import torchvision.transforms as T

    _TORCH = True
except ImportError:  # pragma: no cover - exercised on torch-less installs
    _TORCH = False


class FigureClassifier:
    """Classify a cropped figure PNG into diagram / chart / table / photo."""

    def __init__(self, centroid_file: Path = _CENTROID_FILE) -> None:
        self._centroids = None
        if _TORCH:
            weights = tvm.ResNet18_Weights.IMAGENET1K_V1
            backbone = tvm.resnet18(weights=weights)
            backbone.fc = torch.nn.Identity()  # 512-d embeddings
            backbone.eval()
            self._backbone = backbone
            self._preprocess = T.Compose(
                [
                    T.Resize(256),
                    T.CenterCrop(224),
                    T.ToTensor(),
                    T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                ]
            )
            if centroid_file.exists():
                raw = json.loads(centroid_file.read_text())
                self._centroids = {k: torch.tensor(v) for k, v in raw.items()}
            else:
                log.warning("No centroid file at %s - falling back to heuristic.", centroid_file)

    # ------------------------------------------------------------------ API
    def classify(self, png_bytes: bytes) -> Tuple[str, float]:
        """Return (class_name, confidence in [0, 1])."""
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        if _TORCH and self._centroids:
            return self._classify_torch(img)
        return _heuristic(img)

    def embed(self, img: Image.Image) -> "torch.Tensor":
        with torch.no_grad():
            return self._backbone(self._preprocess(img).unsqueeze(0)).squeeze(0)

    # ------------------------------------------------------------- internals
    def _classify_torch(self, img: Image.Image) -> Tuple[str, float]:
        emb = self.embed(img)
        sims = {
            name: torch.nn.functional.cosine_similarity(emb, cen, dim=0).item()
            for name, cen in self._centroids.items()
        }
        best = max(sims, key=sims.get)
        ordered = sorted(sims.values(), reverse=True)
        margin = ordered[0] - (ordered[1] if len(ordered) > 1 else 0.0)
        confidence = max(0.0, min(1.0, 0.5 + margin * 2.5))
        return best, confidence


def build_centroids(labeled_dir: Path, out_file: Path = _CENTROID_FILE) -> None:
    """Compute class centroids from ``labeled_dir/<class>/*.png`` seed images."""
    if not _TORCH:
        raise RuntimeError("torch/torchvision required: pip install -r requirements.txt")
    clf = FigureClassifier.__new__(FigureClassifier)
    FigureClassifier.__init__(clf, centroid_file=Path("/nonexistent"))
    centroids = {}
    for cls in CLASSES:
        embs = [
            clf.embed(Image.open(p).convert("RGB"))
            for p in sorted((labeled_dir / cls).glob("*.png"))
        ]
        if not embs:
            raise ValueError(f"No seed images for class '{cls}' in {labeled_dir / cls}")
        centroids[cls] = torch.stack(embs).mean(dim=0).tolist()
    out_file.write_text(json.dumps(centroids))
    log.info("Wrote %d class centroids to %s", len(centroids), out_file)


def _heuristic(img: Image.Image) -> Tuple[str, float]:
    """Torch-free fallback: line art has a bimodal luminance histogram."""
    hist = img.convert("L").histogram()
    total = sum(hist) or 1
    dark = sum(hist[:64]) / total
    light = sum(hist[192:]) / total
    mid = 1.0 - dark - light
    if light > 0.55 and dark > 0.02 and mid < 0.35:
        return "diagram", 0.5  # mostly white page with ink -> line art
    return "photo", 0.5


__all__ = ["FigureClassifier", "build_centroids", "CLASSES"]
