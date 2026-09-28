"""T6 init 测试：备份 / 合并幂等 / 保留用户配置 / 精准移除 / undo。全用临时文件。"""

from __future__ import annotations

import json

import pytest

from arm.core import claude_hooks as ch


@pytest.fixture()
def settings_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "model": "opus",
        "language": "zh-CN",
        "env": {"FOO": "bar"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated_marker(tmp_path, monkeypatch):
    """隔离卸载抑制标记：绝不能写真实 ~/.arm（否则真实哨兵会停止执勤）。"""
    p = tmp_path / "hooks_suppress"
    monkeypatch.setattr(ch, "suppress_marker_path", lambda: p)
    return p


class TestMerge:
    def test_preserves_existing_keys(self, settings_file):
        before = ch.load_settings(settings_file)
        after = ch.build_merged(before)
        assert after["model"] == "opus"
        assert after["language"] == "zh-CN"
        assert after["env"] == {"FOO": "bar"}
        assert "hooks" in after

    def test_injects_all_four_events(self, settings_file):
        after = ch.build_merged(ch.load_settings(settings_file))
        for ev in ch.HOOK_EVENTS:
            assert ev in after["hooks"]
            cmd = after["hooks"][ev][0]["hooks"][0]["command"]
            assert "arm" in cmd and "hook-ingress" in cmd and ev in cmd

    def test_merge_is_idempotent(self, settings_file):
        once = ch.build_merged(ch.load_settings(settings_file))
        twice = ch.build_merged(once)
        # 二次合并不应叠加 arm 条目
        assert twice["hooks"]["Stop"] == once["hooks"]["Stop"]
        assert len(twice["hooks"]["Stop"]) == 1

    def test_preserves_users_own_hooks_on_same_event(self, settings_file):
        before = ch.load_settings(settings_file)
        before["hooks"] = {"Stop": [{"hooks": [{"type": "command", "command": "my-own-tool"}]}]}
        after = ch.build_merged(before)
        cmds = [h["command"] for g in after["hooks"]["Stop"] for h in g["hooks"]]
        assert "my-own-tool" in cmds                 # 用户的不丢
        assert any("hook-ingress" in c for c in cmds)  # arm 的也加上（命令带引号/路径）


class TestRemove:
    def test_remove_only_arm_entries(self, settings_file):
        before = ch.load_settings(settings_file)
        before["hooks"] = {"Stop": [
            {"hooks": [{"type": "command", "command": "my-own-tool"}]},
            {"hooks": [{"type": "command", "command": "arm hook-ingress --event Stop"}]},
        ]}
        cleaned = ch.remove_hooks(before)
        cmds = [h["command"] for g in cleaned["hooks"]["Stop"] for h in g["hooks"]]
        assert cmds == ["my-own-tool"]

    def test_remove_drops_empty_hooks_key(self, settings_file):
        after = ch.build_merged(ch.load_settings(settings_file))
        cleaned = ch.remove_hooks(after)
        assert "hooks" not in cleaned  # arm 是唯一 hook，移除后 hooks 键应消失
        # 但其它配置原样
        assert cleaned["model"] == "opus"


class TestInstallUndo:
    def test_install_creates_backup(self, settings_file):
        original = settings_file.read_text(encoding="utf-8")
        bak = ch.install(settings_file, backup=True)
        assert bak is not None and bak.exists()
        assert bak.read_text(encoding="utf-8") == original  # 备份=原内容
        assert "hooks" in _read(settings_file)              # 现文件已注入

    def test_undo_restores(self, settings_file):
        original = _read(settings_file)
        ch.install(settings_file, backup=True)
        assert ch.undo(settings_file) is True
        assert _read(settings_file) == original

    def test_undo_deletes_backup_and_suppresses_sentinel(self, settings_file):
        """undo 必须：还原 + 删备份 + 写抑制标记。

        不删备份的话哨兵按"备份存在=init 过"60s 内重注入，用户永远卸不掉
        hooks（2026-09-28 审查发现的对抗 bug）。重新 init 则清除标记恢复执勤。
        """
        ch.install(settings_file, backup=True)
        bak = ch.default_backup_path(settings_file)
        assert bak.exists()
        assert ch.undo(settings_file) is True
        assert not bak.exists()
        assert ch.suppress_marker_path().exists()
        ch.install(settings_file, backup=True)  # 改主意重新 init
        assert not ch.suppress_marker_path().exists()

    def test_undo_without_backup_returns_false(self, tmp_path):
        p = tmp_path / "fresh.json"
        p.write_text("{}", encoding="utf-8")
        assert ch.undo(p) is False

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            ch.load_settings(p)


class TestHooksInstalled:
    """hooks 健康检查：被第三方工具重写冲掉时能发现（2026-09-25 实际踩坑）。"""

    def test_all_installed(self, settings_file):
        ch.install(settings_file, backup=False)
        r = ch.hooks_installed(settings_file)
        assert r["installed"] is True
        assert r["missing"] == []

    def test_missing_after_third_party_rewrite(self, settings_file):
        ch.install(settings_file, backup=False)
        # 模拟第三方工具整体重写：丢掉 hooks 键、加自己的 $schema
        import json
        d = json.loads(settings_file.read_text(encoding="utf-8"))
        d.pop("hooks")
        d["$schema"] = "https://json.schemastore.org/claude-code-settings.json"
        settings_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        r = ch.hooks_installed(settings_file)
        assert r["installed"] is False
        assert set(r["missing"]) == set(ch.HOOK_EVENTS)

    def test_partial_missing(self, settings_file):
        ch.install(settings_file, backup=False)
        import json
        d = json.loads(settings_file.read_text(encoding="utf-8"))
        d["hooks"].pop("Stop")  # 只丢一个
        settings_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        r = ch.hooks_installed(settings_file)
        assert r["installed"] is False
        assert r["missing"] == ["Stop"]

    def test_invalid_json_file(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{broken", encoding="utf-8")
        r = ch.hooks_installed(p)
        assert r["installed"] is False
