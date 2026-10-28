"""scripts/reap-kimi-workspaces forgets Kimi workspaces of removed trees."""

import json
from pathlib import Path

from tests._script import load_script

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reap-kimi-workspaces"


def _setup(tmp_path, monkeypatch):
    mod = load_script("reap_kimi_workspaces", SCRIPT)
    repo = tmp_path / "repo"
    (repo / ".claude" / "worktrees" / "alive").mkdir(parents=True)
    trees = repo / ".claude" / "worktrees"
    home = tmp_path / "kimi"
    home.mkdir()
    catalog = {
        "version": 1,
        "workspaces": {
            "wd_repo_1": {"root": str(repo), "name": "repo"},
            "wd_alive_2": {"root": str(trees / "alive"), "name": "alive"},
            "wd_gone_3": {"root": str(trees / "gone"), "name": "gone"},
            "wd_else_4": {"root": str(tmp_path / "elsewhere"), "name": "else"},
        },
        "deleted_workspace_ids": ["wd_old_0"],
    }
    (home / "workspaces.json").write_text(json.dumps(catalog), encoding="utf-8")
    index = [
        {
            "workDir": str(trees / "gone"),
            "sessionDir": str(home / "sessions/wd_gone_3/s1"),
        },
        # Only in the index: Kimi would re-add it from here on next start.
        {
            "workDir": str(trees / "idx"),
            "sessionDir": str(home / "sessions/wd_idx_5/s2"),
        },
        {
            "workDir": str(trees / "alive"),
            "sessionDir": str(home / "sessions/wd_alive_2/s3"),
        },
    ]
    (home / "session_index.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in index), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(mod, "main_checkout", lambda: str(repo))
    return mod, home


def test_forgets_only_gone_trees(tmp_path, monkeypatch, capsys):
    mod, home = _setup(tmp_path, monkeypatch)
    assert mod.main([]) == 0
    out = json.loads((home / "workspaces.json").read_text(encoding="utf-8"))
    assert set(out["workspaces"]) == {"wd_repo_1", "wd_alive_2", "wd_else_4"}
    assert out["deleted_workspace_ids"] == ["wd_gone_3", "wd_idx_5", "wd_old_0"]
    assert "forgot 2: gone idx" in capsys.readouterr().out
    assert mod.main(["--for-hook"]) == 0
    assert capsys.readouterr().out == ""


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    mod, home = _setup(tmp_path, monkeypatch)
    before = (home / "workspaces.json").read_text(encoding="utf-8")
    assert mod.main(["--dry-run"]) == 0
    assert (home / "workspaces.json").read_text(encoding="utf-8") == before
    assert "would forget 2" in capsys.readouterr().out


def test_no_kimi_install_is_noop(tmp_path, monkeypatch, capsys):
    mod = load_script("reap_kimi_workspaces", SCRIPT)
    monkeypatch.setattr(mod, "HOME", tmp_path / "absent")
    assert mod.main(["--for-hook"]) == 0
    assert capsys.readouterr().out == ""
