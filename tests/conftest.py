import pytest

from agent_team import herdr


@pytest.fixture(autouse=True)
def _no_watch_daemon(monkeypatch):
    """测试里绝不脱离会话拉起真实 watch 守护进程（tick 有在途任务时会尝试拉起）。"""
    monkeypatch.setenv("TEAM_NO_WATCH", "1")


@pytest.fixture(autouse=True)
def notifications(monkeypatch):
    """herdr 通知打桩：只记录 (title, body, sound)，绝不在真实 herdr 会话里弹 toast。"""
    rec: list[tuple[str, str, str]] = []
    monkeypatch.setattr(herdr, "notify", lambda title, body="", sound="none": rec.append((title, body, sound)))
    return rec
