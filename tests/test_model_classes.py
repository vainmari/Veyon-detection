"""
tests/test_model_classes.py
───────────────────────────
Tests for reading a model's embedded class names — the authoritative
index→name order used to build the model_class mapping at import time.

This guards against the History-mislabeling bug where model_class was built
from hand-typed names instead of the model's real class order (e.g. index 2
shown as "Narsykle" live but stored as "Notepad").
"""
from __future__ import annotations
import os

import pytest


# Embedded order of the reference model (matches weights/yolo26n.onnx).
EMBEDDED = ["DI", "Ekrano nuotraukos", "Narsykle", "Notepad",
            "Paint", "PowerPoint", "Word"]


class TestReadModelClassNames:

    def test_reads_embedded_onnx_names(self):
        from app.core.yolo import read_model_class_names
        path = "weights/yolo26n.onnx"
        if not os.path.exists(path):
            pytest.skip("reference ONNX weights not present")
        assert read_model_class_names(path) == EMBEDDED

    def test_missing_file_returns_none(self):
        from app.core.yolo import read_model_class_names
        assert read_model_class_names("nope/does-not-exist.onnx") is None

    def test_non_model_file_returns_none(self, tmp_path):
        from app.core.yolo import read_model_class_names
        junk = tmp_path / "not-a-model.onnx"
        junk.write_bytes(b"not really an onnx file")
        assert read_model_class_names(str(junk)) is None
