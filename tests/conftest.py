"""Shared safety net for the test suite.

The only thing here is a rule: a test may not talk to anything real on this
machine. It exists because a test that reached the developer's own running
ngrok agent produced a failure in an unrelated test, once, and could not be
reproduced afterwards — which is the worst kind of test result.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_real_ngrok_agent(monkeypatch):
    """Point the agent API at a port nothing listens on.

    Tests that care about the agent stub `agent_tunnels` or `retarget_agent`
    directly. Everything else gets "no agent running", which is the honest
    answer for a machine that has not been set up for tunnels — and, more to
    the point, an answer that does not depend on whose machine it is.
    """
    unreachable = "http://127.0.0.1:1/api/tunnels"
    monkeypatch.setattr("trance.preview.NGROK_API", unreachable, raising=False)
    monkeypatch.setattr("trance.preview.agent_tunnels",
                        lambda api=unreachable, timeout=0.4: [])
    monkeypatch.setattr("trance.preview.agent_running",
                        lambda api=unreachable, timeout=0.4: False)
