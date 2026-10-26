import pytest


@pytest.fixture(autouse=True)
def _no_watch_daemon(monkeypatch):
    """测试里绝不脱离会话拉起真实 watch 守护进程（tick 有在途任务时会尝试拉起）。"""
    monkeypatch.setenv("TEAM_NO_WATCH", "1")
