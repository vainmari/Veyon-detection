"""
tests/test_file_browser.py
──────────────────────────
Tests for the _list_entries() helper in app/pages/_file_browser.py.
NiceGUI is imported by the module but _list_entries uses only pathlib — no
running server or UI mocking required.

Run:  pytest tests/test_file_browser.py -v
"""
from __future__ import annotations
from pathlib import Path


from app.pages._file_browser import _list_entries


# ── Helpers ───────────────────────────────────────────────────────────────────

def _names(entries, kind=None):
    """Extract entry names, optionally filtered by 'dir' or 'file'."""
    return [name for k, name, _ in entries if kind is None or k == kind]


# ── Ordering ──────────────────────────────────────────────────────────────────

class TestOrdering:
    def test_directories_listed_before_files(self, tmp_path):
        (tmp_path / "z_file.txt").touch()
        (tmp_path / "a_dir").mkdir()
        entries = _list_entries(tmp_path, None, "both")
        kinds = [k for k, name, _ in entries if name != ".."]
        assert kinds == ["dir", "file"]

    def test_entries_alphabetical_within_each_kind(self, tmp_path):
        for name in ["c.txt", "a.txt", "b.txt"]:
            (tmp_path / name).touch()
        for name in ["gamma", "alpha", "beta"]:
            (tmp_path / name).mkdir()
        entries = _list_entries(tmp_path, None, "both")
        file_names = _names(entries, "file")
        dir_names  = [n for n in _names(entries, "dir") if n != ".."]
        assert file_names == sorted(file_names, key=str.lower)
        assert dir_names  == sorted(dir_names,  key=str.lower)


# ── Parent entry ──────────────────────────────────────────────────────────────

class TestDotDot:
    def test_dotdot_prepended_for_non_root(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        entries = _list_entries(sub, None, "both")
        assert entries[0] == ("dir", "..", tmp_path)

    def test_no_dotdot_at_filesystem_root(self):
        root = Path(Path.cwd().anchor)   # C:\ on Windows, / on Linux
        entries = _list_entries(root, None, "both")
        assert all(name != ".." for _, name, _ in entries)


# ── Extension filtering ───────────────────────────────────────────────────────

class TestExtensionFilter:
    def test_only_matching_extension_shown(self, tmp_path):
        (tmp_path / "key.pem").touch()
        (tmp_path / "readme.txt").touch()
        (tmp_path / "cert.pem").touch()
        entries = _list_entries(tmp_path, [".pem"], "file")
        file_names = set(_names(entries, "file"))
        assert file_names == {"key.pem", "cert.pem"}
        assert "readme.txt" not in file_names

    def test_no_filter_shows_all_files(self, tmp_path):
        for name in ["a.pem", "b.txt", "c.onnx"]:
            (tmp_path / name).touch()
        file_names = set(_names(_list_entries(tmp_path, None, "file"), "file"))
        assert file_names == {"a.pem", "b.txt", "c.onnx"}

    def test_extension_match_is_case_insensitive(self, tmp_path):
        (tmp_path / "upper.PEM").touch()
        entries = _list_entries(tmp_path, [".pem"], "file")
        assert "upper.PEM" in _names(entries, "file")

    def test_directories_never_filtered_by_extension(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        (tmp_path / "other.txt").touch()
        entries = _list_entries(tmp_path, [".pem"], "both")
        assert "subdir" in _names(entries, "dir")


# ── Mode filtering ────────────────────────────────────────────────────────────

class TestMode:
    def test_folder_mode_excludes_all_files(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "file.txt").touch()
        entries = _list_entries(tmp_path, None, "folder")
        assert _names(entries, "file") == []
        assert "sub" in _names(entries, "dir")

    def test_file_mode_includes_files_and_dirs_for_navigation(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "key.pem").touch()
        entries = _list_entries(tmp_path, None, "file")
        assert "key.pem" in _names(entries, "file")
        assert "sub" in _names(entries, "dir")

    def test_both_mode_includes_everything(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "file.txt").touch()
        entries = _list_entries(tmp_path, None, "both")
        assert "sub"      in _names(entries, "dir")
        assert "file.txt" in _names(entries, "file")


# ── Error handling ────────────────────────────────────────────────────────────

class TestPermissionError:
    def test_permission_error_silenced(self, tmp_path, monkeypatch):
        def _raise(self):
            raise PermissionError("Access denied")
        monkeypatch.setattr(Path, "iterdir", _raise)
        # Should not raise — only the '..' entry (for non-root tmp_path) is returned
        entries = _list_entries(tmp_path, None, "both")
        assert all(name == ".." for _, name, _ in entries)
