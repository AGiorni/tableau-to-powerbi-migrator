"""Tests for locating the pinned external tools."""

from __future__ import annotations

from t2pbi import toolchain


def test_pinned_copy_wins_over_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "some-tool.cmd").write_text("@echo off\n")
    monkeypatch.setenv("T2PBI_TOOLS_DIR", str(tmp_path))
    monkeypatch.setattr(toolchain.shutil, "which", lambda _name: r"C:\elsewhere\some-tool.cmd")

    assert toolchain.find_node_tool("some-tool") == str(bin_dir / "some-tool.cmd")


def test_falls_back_to_path_then_none(tmp_path, monkeypatch):
    monkeypatch.setenv("T2PBI_TOOLS_DIR", str(tmp_path))  # no node_modules here
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert toolchain.find_node_tool("some-tool") == "/usr/bin/some-tool"

    monkeypatch.setattr(toolchain.shutil, "which", lambda _name: None)
    assert toolchain.find_node_tool("some-tool") is None


def test_dotnet_tool_prefers_launcher_then_dll(tmp_path, monkeypatch):
    publish = tmp_path / "my-tool" / "bin" / "publish"
    publish.mkdir(parents=True)
    monkeypatch.setenv("T2PBI_TOOLS_DIR", str(tmp_path))
    monkeypatch.setattr(toolchain.shutil, "which", lambda _name: "/usr/bin/dotnet")
    assert toolchain.find_dotnet_tool("my-tool") is None

    (publish / "my-tool.dll").write_bytes(b"")
    assert toolchain.find_dotnet_tool("my-tool") == ["/usr/bin/dotnet", str(publish / "my-tool.dll")]

    (publish / "my-tool.exe").write_bytes(b"")
    assert toolchain.find_dotnet_tool("my-tool") == [str(publish / "my-tool.exe")]


def test_windows_shims_run_through_cmd():
    assert toolchain.command("x.cmd", "a") == ["cmd", "/c", "x.cmd", "a"]
    assert toolchain.command("/usr/bin/x", "a") == ["/usr/bin/x", "a"]
