"""
app/services/training_service.py
─────────────────────────────────
Dataset analysis, split creation, YOLO training, ONNX export.

Progress message shapes
───────────────────────
{"type": "status",  "message": str}
{"type": "batch",   "batch": int, "total_batches": int,
                    "epoch": int, "total_epochs": int}
{"type": "epoch",   "epoch": int, "total": int,
                    "map50": float, "map50_95": float,
                    "precision": float, "recall": float,
                    "box_loss": float, "cls_loss": float, "dfl_loss": float}
{"type": "done",    "model_id": int, "onnx_path": str,
                    "map50": float, "map50_95": float,
                    "precision": float, "recall": float}
{"type": "error",   "message": str}
{"type": "cancelled"}
"""
from __future__ import annotations

import csv
import json
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

DATASETS_DIR = Path("data/datasets")
MODELS_DIR   = Path("data/models")
MODELS_DIR.mkdir(parents=True, exist_ok=True)
DATASETS_DIR.mkdir(parents=True, exist_ok=True)

BASE_MODELS = [
                "yolov5n.pt", "yolov5s.pt", "yolov5m.pt",
                "yolov8n.pt", "yolov8s.pt", "yolov8m.pt",
            #   "yolov9t.pt", "yolov9s.pt", "yolov9m.pt",
            #   "yolov10n.pt", "yolov10s.pt", "yolov10m.pt",
                "yolo11n.pt", "yolo11s.pt", "yolo11m.pt",
            #   "yolov12n.pt", "yolov12s.pt", "yolov12m.pt",
            #   "yolov13n.pt", "yolov13s.pt", "yolov13m.pt",
               "yolo26n.pt", "yolo26s.pt", "yolo26m.pt",
]


# ── GPU detection ─────────────────────────────────────────────────────────────

def get_torch_info() -> dict:
    """Return dict with torch version, cuda availability, and suggested install."""
    try:
        import torch
        cuda_ok    = torch.cuda.is_available()
        torch_ver  = torch.__version__
        cuda_ver   = getattr(torch.version, "cuda", None) or "—"
        gpu_name   = torch.cuda.get_device_name(0) if cuda_ok else None
    except ImportError:
        return {"installed": False}

    # Try to detect system CUDA via nvidia-smi
    sys_cuda: Optional[str] = None
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True,
                           text=True, timeout=5)
        m = re.search(r"CUDA Version:\s*(\d+\.\d+)", r.stdout)
        if m:
            sys_cuda = m.group(1)
    except Exception:
        pass

    # Map system CUDA to pip wheel suffix
    install_url = _cuda_install_url(sys_cuda)

    return {
        "installed":   True,
        "torch_ver":   torch_ver,
        "cuda_ok":     cuda_ok,
        "cuda_ver":    cuda_ver,
        "gpu_name":    gpu_name,
        "sys_cuda":    sys_cuda,
        "install_url": install_url,
    }


def _cuda_install_url(sys_cuda: Optional[str]) -> str:
    if sys_cuda:
        major = int(sys_cuda.split(".")[0])
        if major >= 12:
            return "https://download.pytorch.org/whl/cu121"
        if major == 11:
            return "https://download.pytorch.org/whl/cu118"
    return "https://download.pytorch.org/whl/cu121"


def install_cuda_torch(progress_q: queue.Queue) -> None:
    """
    Uninstall the current CPU torch, then install the CUDA build.
    Runs two pip commands sequentially so the replace is forced even if
    the version numbers look the same to pip.
    torchaudio is omitted — YOLO doesn't need it and it causes pip to
    spin forever on version resolution.
    """
    info = get_torch_info()
    url  = info.get("install_url", "https://download.pytorch.org/whl/cu121")

    def _run(cmd: list[str], label: str) -> bool:
        progress_q.put(f"▶ {label}")
        progress_q.put(f"  {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                stripped = line.rstrip()
                if stripped:
                    progress_q.put(stripped)
            proc.wait()
            if proc.returncode != 0:
                progress_q.put(f"❌ Command failed (exit {proc.returncode})")
                return False
            return True
        except Exception as e:
            progress_q.put(f"❌ {e}")
            return False

    # Step 1 — uninstall (force-replace even if version looks identical)
    ok = _run(
        [sys.executable, "-m", "pip", "uninstall", "torch", "torchvision",
         "-y"],
        "Uninstalling current torch…",
    )
    if not ok:
        return

    # Step 2 — install CUDA build (~2.4 GB download)
    progress_q.put("Downloading CUDA torch (~2.4 GB) — this may take several minutes…")
    ok = _run(
        [sys.executable, "-m", "pip", "install",
         "torch", "torchvision",
         "--index-url", url],
        "Installing CUDA torch…",
    )
    if ok:
        progress_q.put(
            "✅ Done — restart the server and the GPU card will show green."
        )


# ── COCO → YOLO conversion ────────────────────────────────────────────────────

def _find_coco_jsons(root: Path) -> list[Path]:
    """
    Return annotation JSON files that look like COCO format.
    Searches root/annotations/*.json and root/**/*instances*.json.
    """
    candidates: list[Path] = []
    ann_dir = root / "annotations"
    if ann_dir.exists():
        candidates += sorted(ann_dir.glob("*.json"))
    # Also check top-level and one level deep
    candidates += [p for p in root.glob("*.json")
                   if p not in candidates]
    candidates += [p for p in root.glob("*/*.json")
                   if p not in candidates]

    valid = []
    for p in candidates:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "images" in d and "annotations" in d:
                valid.append(p)
        except Exception:
            pass
    return valid


def _coco_split_name(json_path: Path) -> str:
    """
    Guess the split name from the JSON filename.
    instances_train2017.json → train
    val.json                 → val
    test_annotations.json    → test
    """
    stem = json_path.stem.lower()
    for s in ("train", "val", "valid", "test"):
        if s in stem:
            return "val" if s == "valid" else s
    return "train"   # safest default


def _find_coco_images_dir(root: Path, split: str, json_path: Path) -> Optional[Path]:
    """Try several common image directory layouts for a given split."""
    candidates = [
        root / split,
        root / f"{split}2017",
        root / f"{split}2019",
        root / "images" / split,
        root / "images" / f"{split}2017",
        json_path.parent.parent / split,
        json_path.parent.parent / f"{split}2017",
        json_path.parent.parent / "images" / split,
    ]
    for c in candidates:
        if c.exists() and _count_images(c) > 0:
            return c
    # Last resort: any directory whose name contains the split word
    for d in root.rglob("*"):
        if d.is_dir() and split in d.name.lower() and _count_images(d) > 0:
            return d
    return None


def convert_coco_to_yolo(root: Path) -> Optional[Path]:
    """
    Detect COCO-format annotations inside *root*, convert them to YOLO layout,
    write a data.yaml, and return the dataset root path.

    Returns None if no COCO annotations are found (caller should try YOLO path).

    Output layout (written inside root/yolo_converted/):
      yolo_converted/
        images/train/  images/val/  images/test/
        labels/train/  labels/val/  labels/test/
        data.yaml
    """
    json_files = _find_coco_jsons(root)
    if not json_files:
        return None

    out = root / "yolo_converted"

    # ── Collect all categories across all JSONs (they must be consistent) ────
    all_cats: dict[int, str] = {}   # coco_id → name
    for jp in json_files:
        with open(jp, encoding="utf-8") as f:
            d = json.load(f)
        for cat in d.get("categories", []):
            all_cats[cat["id"]] = cat["name"]

    if not all_cats:
        return None

    # Build a contiguous 0-indexed mapping (COCO ids are not always 0-based)
    sorted_ids   = sorted(all_cats.keys())
    coco_to_yolo = {coco_id: yolo_idx
                    for yolo_idx, coco_id in enumerate(sorted_ids)}
    names        = [all_cats[cid] for cid in sorted_ids]

    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

    for jp in json_files:
        split = _coco_split_name(jp)
        img_src = _find_coco_images_dir(root, split, jp)

        out_img = out / "images" / split
        out_lbl = out / "labels" / split
        out_img.mkdir(parents=True, exist_ok=True)
        out_lbl.mkdir(parents=True, exist_ok=True)

        with open(jp, encoding="utf-8") as f:
            data = json.load(f)

        # Build image_id → filename map
        id_to_file: dict[int, str] = {
            img["id"]: img["file_name"]
            for img in data.get("images", [])
        }
        id_to_size: dict[int, tuple[int, int]] = {
            img["id"]: (img["width"], img["height"])
            for img in data.get("images", [])
        }

        # Group annotations by image_id
        from collections import defaultdict
        ann_by_img: dict[int, list] = defaultdict(list)
        for ann in data.get("annotations", []):
            ann_by_img[ann["image_id"]].append(ann)

        for img_id, fname in id_to_file.items():
            # Copy image
            src_img: Optional[Path] = None
            if img_src:
                # fname may include a subdirectory, e.g. "train2017/000001.jpg"
                candidate = img_src / Path(fname).name
                if not candidate.exists():
                    candidate = img_src / fname
                if candidate.exists():
                    src_img = candidate
            if src_img is None:
                # Global search as fallback
                name_only = Path(fname).name
                hits = list(root.rglob(name_only))
                if hits:
                    src_img = hits[0]

            if src_img and src_img.suffix.lower() in exts:
                dst_img = out_img / src_img.name
                if not dst_img.exists():
                    shutil.copy2(src_img, dst_img)

            # Write YOLO label file
            W, H = id_to_size.get(img_id, (0, 0))
            if W == 0 or H == 0:
                continue

            lines: list[str] = []
            for ann in ann_by_img[img_id]:
                cat_id  = ann.get("category_id")
                if cat_id not in coco_to_yolo:
                    continue
                yolo_cls = coco_to_yolo[cat_id]
                x, y, w, h = ann["bbox"]   # COCO: top-left x,y + width, height
                cx = (x + w / 2) / W
                cy = (y + h / 2) / H
                nw = w / W
                nh = h / H
                # Clamp to [0, 1]
                cx = max(0.0, min(1.0, cx))
                cy = max(0.0, min(1.0, cy))
                nw = max(0.0, min(1.0, nw))
                nh = max(0.0, min(1.0, nh))
                if nw > 0 and nh > 0:
                    lines.append(
                        f"{yolo_cls} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

            if lines:
                lbl_stem = Path(
                    id_to_file[img_id]).stem
                (out_lbl / f"{lbl_stem}.txt").write_text(
                    "\n".join(lines), encoding="utf-8")

    # Write data.yaml
    yaml_data = {
        "path":  str(out),
        "train": "images/train",
        "val":   "images/val",
        "test":  "images/test",
        "nc":    len(names),
        "names": names,
    }
    yaml_out = out / "data.yaml"
    with open(yaml_out, "w", encoding="utf-8") as f:
        yaml.dump(yaml_data, f, allow_unicode=True)

    return out


# ── ZIP extraction ────────────────────────────────────────────────────────────

def extract_zip(zip_bytes: bytes, dest: Path) -> Path:
    """
    Write bytes to disk, extract, return the directory that contains data.yaml.
    Handles three common ZIP layouts:
      - data.yaml at root of ZIP
      - data.yaml inside one top-level subfolder
      - data.yaml anywhere deeper in the tree
    """
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / "_upload.zip"
    zip_path.write_bytes(zip_bytes)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest)
    zip_path.unlink(missing_ok=True)

    # Find data.yaml anywhere in the extracted tree (most reliable)
    yaml_files = list(dest.rglob("data.yaml"))
    if yaml_files:
        # Prefer the shallowest one
        yaml_files.sort(key=lambda p: len(p.parts))
        return yaml_files[0].parent

    # Fallback: single top-level folder
    children = [c for c in dest.iterdir() if c.is_dir()]
    if len(children) == 1:
        return children[0]
    return dest


# ── Dataset analysis ──────────────────────────────────────────────────────────

def _count_images(d: Path) -> int:
    if not d.exists():
        return 0
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    return sum(1 for f in d.rglob("*") if f.suffix.lower() in exts)


def _count_labels(d: Path) -> dict[int, int]:
    counts: dict[int, int] = {}
    if not d.exists():
        return counts
    for f in d.rglob("*.txt"):
        for line in f.read_text(errors="ignore").splitlines():
            parts = line.strip().split()
            if parts:
                try:
                    cid = int(parts[0])
                    counts[cid] = counts.get(cid, 0) + 1
                except ValueError:
                    pass
    return counts


def _find_split_dir(yaml_base: Path, data: dict, split: str) -> Optional[Path]:
    rel = data.get(split)
    candidates: list[Path] = []
    if rel:
        # rel may be absolute already or relative to yaml_base
        p = Path(rel)
        candidates.append(p if p.is_absolute() else (yaml_base / p).resolve())
    candidates += [
        yaml_base / "images" / split,
        yaml_base / split / "images",
        yaml_base / split,
    ]
    for c in candidates:
        if c.exists() and _count_images(c) > 0:
            return c
    return None


def analyze_dataset(dataset_dir: str) -> dict:
    root = Path(dataset_dir)

    # ── Auto-detect format ────────────────────────────────────────────────────
    yaml_candidates = list(root.rglob("data.yaml"))

    if not yaml_candidates:
        # No data.yaml → try COCO conversion
        converted = convert_coco_to_yolo(root)
        if converted is None:
            return {
                "ok": False,
                "error": (
                    "No data.yaml found and no COCO annotation JSON detected.  \n"
                    "Supported formats: YOLO (data.yaml + images/ + labels/) "
                    "or COCO (annotations/*.json with images/)."
                ),
            }
        root           = converted
        yaml_candidates = list(root.rglob("data.yaml"))
        if not yaml_candidates:
            return {"ok": False,
                    "error": "COCO conversion succeeded but data.yaml missing."}
    else:
        # data.yaml exists — but may still have COCO annotations alongside it
        # (some export tools include both). Prefer YOLO if data.yaml is valid.
        pass

    yaml_candidates.sort(key=lambda p: len(p.parts))
    yaml_path = yaml_candidates[0]

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    names = data.get("names", [])
    nc    = int(data.get("nc", len(names)))
    if not names:
        return {"ok": False,
                "error": "data.yaml is missing the 'names' list."}

    yaml_base = yaml_path.parent
    splits: dict[str, dict] = {}
    for s in ("train", "val", "test"):
        img_dir = _find_split_dir(yaml_base, data, s)
        if img_dir:
            lbl_dir = Path(str(img_dir).replace("images", "labels", 1))
            splits[s] = {
                "img_dir": str(img_dir),
                "lbl_dir": str(lbl_dir),
                "images":  _count_images(img_dir),
            }

    if "train" not in splits:
        return {"ok": False,
                "error": "No training images found. "
                         "Expected an images/train/ directory."}

    total_counts: dict[int, int] = {}
    for info in splits.values():
        for cid, cnt in _count_labels(Path(info["lbl_dir"])).items():
            total_counts[cid] = total_counts.get(cid, 0) + cnt

    class_counts = [
        {"id":    i,
         "name":  names[i] if i < len(names) else f"class_{i}",
         "count": total_counts.get(i, 0)}
        for i in range(nc)
    ]
    class_counts.sort(key=lambda x: x["count"], reverse=True)

    warnings:    list[str] = []
    needs_split: list[str] = []

    sample_vals = [c["count"] for c in class_counts if c["count"] > 0]
    if sample_vals:
        mx, mn = max(sample_vals), min(sample_vals)
        empty  = [c["name"] for c in class_counts if c["count"] == 0]
        if empty:
            warnings.append(
                f"⚠️  Classes with zero samples: {', '.join(empty)}. "
                "These will not be learned.")
        elif mn > 0 and mx / mn > 10:
            warnings.append(
                f"⚠️  Severe imbalance — ratio {mx/mn:.0f}:1. "
                "Consider oversampling minority classes.")
        sparse = [c["name"] for c in class_counts if 0 < c["count"] < 20]
        if sparse:
            warnings.append(
                f"⚠️  Classes with < 20 samples: {', '.join(sparse)}.")

    if "val" not in splits and "test" not in splits:
        needs_split = ["val", "test"]
        warnings.append(
            "ℹ️  Only train split found. "
            "Will auto-create val (10%) and test (10%) from training data.")
    elif "val" not in splits:
        needs_split = ["val"]
        warnings.append(
            "ℹ️  Missing val split. Will auto-create from train (10%).")
    elif "test" not in splits:
        needs_split = ["test"]
        warnings.append(
            "ℹ️  Missing test split. Will auto-create from val (10%).")

    return {
        "ok":           True,
        "yaml_path":    str(yaml_path),
        "dataset_dir":  str(yaml_base),
        "nc":           nc,
        "names":        names,
        "splits":       splits,
        "class_counts": class_counts,
        "warnings":     warnings,
        "needs_split":  needs_split,
        "total_images": sum(v["images"] for v in splits.values()),
        "source_format": "coco" if "yolo_converted" in str(yaml_path) else "yolo",
    }


# ── Auto split creation ───────────────────────────────────────────────────────

def prepare_splits(analysis: dict) -> str:
    needs = analysis.get("needs_split", [])
    if not needs:
        return analysis["yaml_path"]

    base   = Path(analysis["dataset_dir"])
    tr_img = Path(analysis["splits"]["train"]["img_dir"])
    exts   = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    all_img = sorted(f for f in tr_img.rglob("*") if f.suffix.lower() in exts)
    random.shuffle(all_img)
    n = len(all_img)

    def _copy_split(name: str, files: list[Path]) -> None:
        out_img = base / "images" / name
        out_lbl = base / "labels" / name
        out_img.mkdir(parents=True, exist_ok=True)
        out_lbl.mkdir(parents=True, exist_ok=True)
        for img in files:
            shutil.copy2(img, out_img / img.name)
            lbl = Path(str(img).replace("images", "labels", 1)).with_suffix(".txt")
            if lbl.exists():
                shutil.copy2(lbl, out_lbl / lbl.name)

    if "val" in needs and "test" in needs:
        _copy_split("val",  all_img[int(n * .80): int(n * .90)])
        _copy_split("test", all_img[int(n * .90):])
    elif "val" in needs:
        _copy_split("val", all_img[int(n * .90):])
    elif "test" in needs:
        val_img = Path(analysis["splits"]["val"]["img_dir"])
        v_files = sorted(f for f in val_img.rglob("*") if f.suffix.lower() in exts)
        random.shuffle(v_files)
        _copy_split("test", v_files[:max(1, len(v_files) // 10)])

    with open(analysis["yaml_path"], encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.update({"path":  str(base),
                 "train": "images/train",
                 "val":   "images/val",
                 "test":  "images/test"})
    new_yaml = base / "data.yaml"
    with open(new_yaml, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
    return str(new_yaml)


# ── Fine-tune helpers ─────────────────────────────────────────────────────────

def _yaml_for_base(base_model_str: str) -> Optional[str]:
    """
    Given a base model filename like 'yolo11n.pt', return the matching
    architecture yaml 'yolo11n.yaml'. Returns None if not determinable.
    """
    if not base_model_str:
        return None
    stem = Path(base_model_str).stem  # e.g. 'yolo11n'
    yaml_candidate = f"{stem}.yaml"
    return yaml_candidate


def remap_dataset_for_finetune(
    analysis: dict,
    source_names: list[str],
    keep_new_class_indices: set,  # dataset class indices the user approved adding
) -> tuple:
    """
    Remap a dataset's label files so they align with source_names + any
    approved new classes appended at the end.

    Returns (new_yaml_path, final_names_list).

    Process:
      1. Build mapping: dataset_class_index → final_class_index
         - Matches by case-insensitive name to source_names
         - Approved new classes appended to source_names in dataset order
         - Non-approved new classes mapped to -1 (dropped)
      2. Copy images and rewrite label files into data/datasets/<run>_remapped/
      3. Write a new data.yaml with nc + names
    """
    dataset_names: list[str] = analysis["names"]
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DATASETS_DIR / f"finetune_remapped_{run_ts}"

    # Build source index lookup (case-insensitive)
    src_lower = {n.lower(): i for i, n in enumerate(source_names)}

    # Build final names list
    final_names = list(source_names)  # start with all source classes
    dataset_to_final: dict = {}  # dataset idx → final idx

    for ds_idx, ds_name in enumerate(dataset_names):
        src_idx = src_lower.get(ds_name.lower())
        if src_idx is not None:
            dataset_to_final[ds_idx] = src_idx
        elif ds_idx in keep_new_class_indices:
            # Append as new class
            final_idx = len(final_names)
            final_names.append(ds_name)
            dataset_to_final[ds_idx] = final_idx
        else:
            dataset_to_final[ds_idx] = -1  # drop

    # Copy images + rewrite labels for each split
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    for split, info in analysis.get("splits", {}).items():
        img_src = Path(info["img_dir"])
        lbl_src = Path(info["lbl_dir"])
        out_img = out_dir / "images" / split
        out_lbl = out_dir / "labels" / split
        out_img.mkdir(parents=True, exist_ok=True)
        out_lbl.mkdir(parents=True, exist_ok=True)

        for img_file in img_src.rglob("*"):
            if img_file.suffix.lower() in exts:
                shutil.copy2(img_file, out_img / img_file.name)

        for lbl_file in lbl_src.rglob("*.txt"):
            new_lines = []
            for line in lbl_file.read_text(errors="ignore").splitlines():
                parts = line.strip().split()
                if not parts:
                    continue
                try:
                    ds_cls = int(parts[0])
                except ValueError:
                    continue
                final_cls = dataset_to_final.get(ds_cls, -1)
                if final_cls >= 0:
                    new_lines.append(f"{final_cls} " + " ".join(parts[1:]))
            (out_lbl / lbl_file.name).write_text("\n".join(new_lines), encoding="utf-8")

    # Write data.yaml
    yaml_data = {
        "path":  str(out_dir),
        "train": "images/train",
        "val":   "images/val",
        "test":  "images/test",
        "nc":    len(final_names),
        "names": final_names,
    }
    new_yaml = out_dir / "data.yaml"
    with open(new_yaml, "w", encoding="utf-8") as f:
        yaml.dump(yaml_data, f, allow_unicode=True)

    return str(new_yaml), final_names


# ── Training worker ───────────────────────────────────────────────────────────

class TrainingWorker:
    """
    Runs YOLO fine-tuning in a daemon thread.

    Persistent state (survives page navigation)
    ────────────────────────────────────────────
    epoch_history   — list of epoch dicts (replayed when page reloads)
    batch_progress  — latest batch dict (polled directly by UI timer)
    current_status  — latest status string
    """

    def __init__(self) -> None:
        self.progress_q:   queue.Queue[dict] = queue.Queue()
        self._thread:      Optional[threading.Thread] = None
        self._trainer      = None
        self.is_running    = False
        self.is_done       = False
        self.config:       dict = {}

        # Persistent across page navigation
        self.epoch_history:  list[dict] = []
        self.current_status: str        = "Initializing…"
        self.batch_progress: dict       = {
            "batch": 0, "total_batches": 0,
            "epoch": 0, "total_epochs":  0,
        }

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self, config: dict) -> None:
        self.config     = config
        self.is_running = True
        self.is_done    = False
        self._thread    = threading.Thread(
            target=self._run, daemon=True, name="yolo-train")
        self._thread.start()

    def cancel(self) -> None:
        self.is_running = False
        if self._trainer is not None:
            try:
                self._trainer.stop = True
            except Exception:
                pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _push_status(self, message: str) -> None:
        self.current_status = message
        self.progress_q.put({"type": "status", "message": message})

    def _push_epoch(self, msg: dict) -> None:
        self.epoch_history.append(msg)
        self.current_status = (
            f"Epoch {msg['epoch']}/{msg['total']}  |  "
            f"mAP50 {msg['map50']:.3f}  |  "
            f"box {msg['box_loss']:.4f}"
        )
        self.progress_q.put(msg)

    def _update_batch(self, batch: int, total: int,
                      epoch: int, total_epochs: int) -> None:
        """Update batch progress in-place — NOT pushed to queue (too noisy)."""
        self.batch_progress = {
            "batch":        batch,
            "total_batches": total,
            "epoch":        epoch,
            "total_epochs": total_epochs,
        }

    # ── Training ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        import torch
        from ultralytics import YOLO

        cfg      = self.config
        device   = "cuda" if torch.cuda.is_available() else "cpu"
        run_name = cfg["run_name"]
        MODELS_DIR.mkdir(parents=True, exist_ok=True)

        source_pt = cfg.get("source_pt_path")
        if source_pt:
            source_nc = cfg.get("source_model_nc", cfg["nc"])
            if source_nc == cfg["nc"]:
                # Scenario A — same class count, direct fine-tune
                self._push_status(f"Loading source model for fine-tuning: {source_pt} …")
            else:
                # Scenario B — different class count, rebuild head, load backbone
                base_yaml = _yaml_for_base(cfg.get("source_base_model", ""))
                if not base_yaml:
                    self.progress_q.put({"type": "error",
                        "message": "Cannot fine-tune: source model has unknown architecture. "
                                   "Only models trained from known YOLO bases support class extension."})
                    return
                self._push_status(f"Rebuilding model head ({source_nc} → {cfg['nc']} classes) …")
        else:
            self._push_status(f"Loading base model  {cfg['base_model']} …")

        try:
            source_pt = cfg.get("source_pt_path")
            if source_pt:
                source_nc = cfg.get("source_model_nc", cfg["nc"])
                if source_nc == cfg["nc"]:
                    model = YOLO(source_pt)
                else:
                    base_yaml = _yaml_for_base(cfg.get("source_base_model", ""))
                    model = YOLO(base_yaml).load(source_pt)
            else:
                model = YOLO(cfg["base_model"])

            # Per-batch progress (stored in attrs, not queue)
            _batch_idx    = [0]
            _total_batches = [0]

            def _on_epoch_start(trainer) -> None:
                _batch_idx[0] = 0
                try:
                    _total_batches[0] = len(trainer.train_loader)
                except Exception:
                    pass

            def _on_batch_end(trainer) -> None:
                _batch_idx[0] += 1
                self._update_batch(
                    batch       = _batch_idx[0],
                    total       = _total_batches[0],
                    epoch       = int(trainer.epoch) + 1,
                    total_epochs = int(trainer.epochs),
                )

            def _on_fit_epoch_end(trainer) -> None:
                """
                Fires after both training and validation each epoch.
                Read metrics from trainer + results.csv for accuracy.
                """
                self._trainer = trainer
                ep  = int(trainer.epoch) + 1
                tot = int(trainer.epochs)

                # Val metrics from trainer.metrics (available after validation)
                m      = trainer.metrics or {}
                map50  = float(m.get("metrics/mAP50(B)",    0))
                m5095  = float(m.get("metrics/mAP50-95(B)", 0))
                prec   = float(m.get("metrics/precision(B)", 0))
                rec    = float(m.get("metrics/recall(B)",    0))

                # Training losses from results.csv (most reliable source)
                box_loss = cls_loss = dfl_loss = 0.0
                try:
                    csv_path = Path(trainer.save_dir) / "results.csv"
                    if csv_path.exists():
                        with open(csv_path, newline="") as f:
                            rows = list(csv.DictReader(f))
                        if rows:
                            last = {k.strip(): v.strip()
                                    for k, v in rows[-1].items()}
                            box_loss = float(last.get("train/box_loss", 0) or 0)
                            cls_loss = float(last.get("train/cls_loss", 0) or 0)
                            dfl_loss = float(last.get("train/dfl_loss", 0) or 0)
                            # If CSV doesn't have train losses, try val losses
                            if box_loss == 0:
                                box_loss = float(
                                    last.get("val/box_loss", 0) or 0)
                except Exception:
                    pass

                self._push_epoch({
                    "type":      "epoch",
                    "epoch":     ep,
                    "total":     tot,
                    "map50":     map50,
                    "map50_95":  m5095,
                    "precision": prec,
                    "recall":    rec,
                    "box_loss":  box_loss,
                    "cls_loss":  cls_loss,
                    "dfl_loss":  dfl_loss,
                })

            model.add_callback("on_train_epoch_start", _on_epoch_start)
            model.add_callback("on_train_batch_end",   _on_batch_end)
            model.add_callback("on_fit_epoch_end",     _on_fit_epoch_end)

            self._push_status(f"Training on {device}  ({cfg['epochs']} epochs) …")

            train_kwargs = dict(
                data     = cfg["yaml_path"],
                epochs   = cfg["epochs"],
                imgsz    = cfg["imgsz"],
                batch    = cfg["batch"],
                device   = device,
                project  = str(MODELS_DIR),
                name     = run_name,
                exist_ok = True,
                verbose  = False,
            )
            if cfg.get("learning_rate"):
                train_kwargs["lr0"] = float(cfg["learning_rate"])
            if cfg.get("freeze_backbone"):
                train_kwargs["freeze"] = 10

            results = model.train(**train_kwargs)

            if not self.is_running:
                self.progress_q.put({"type": "cancelled"})
                return

            self._push_status("Exporting to ONNX …")
            onnx_path = model.export(format="onnx", imgsz=cfg["imgsz"])

            rd    = results.results_dict
            m50   = float(rd.get("metrics/mAP50(B)",      0))
            m5095 = float(rd.get("metrics/mAP50-95(B)",   0))
            prec  = float(rd.get("metrics/precision(B)",  0))
            rec   = float(rd.get("metrics/recall(B)",     0))
            pt_p  = str(MODELS_DIR / run_name / "weights" / "best.pt")

            from app.db.database import create_ml_model
            model_id = create_ml_model(
                name            = run_name,
                nc              = cfg["nc"],
                class_names     = cfg["names"],
                pt_path         = pt_p,
                onnx_path       = str(onnx_path),
                map50           = m50,
                map50_95        = m5095,
                precision       = prec,
                recall          = rec,
                status          = "ready",
                imgsz           = cfg["imgsz"],
                dataset_path    = cfg["yaml_path"],
                base_model      = cfg.get("base_model") or cfg.get("source_base_model"),
                epochs          = cfg["epochs"],
                batch           = cfg["batch"],
                device          = device,
                parent_model_id = cfg.get("source_model_id"),
                finetune_lr     = cfg.get("learning_rate"),
                finetune_frozen = 10 if cfg.get("freeze_backbone") else None,
            )

            self.progress_q.put({
                "type":      "done",
                "model_id":  model_id,
                "onnx_path": str(onnx_path),
                "map50":     m50,
                "map50_95":  m5095,
                "precision": prec,
                "recall":    rec,
            })

        except Exception as exc:
            self.progress_q.put({"type": "error", "message": str(exc)})
        finally:
            self.is_running = False
            self.is_done    = True