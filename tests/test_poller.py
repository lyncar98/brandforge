import pytest

from brandforge.errors import PollTimeout
from brandforge.models import Generation, State
from brandforge.poller import PollConfig, poll_until_terminal


def _gen(state):
    return Generation(id="g1", state=state)


def test_polls_until_completed():
    states = [State.QUEUED, State.PROCESSING, State.COMPLETED]
    seq = iter(states)
    seen = []
    gen = poll_until_terminal(
        lambda gid: _gen(next(seq)),
        "g1",
        PollConfig(initial_delay=0, interval=0),
        sleep=lambda d: None,
        on_poll=lambda g: seen.append(g.state),
    )
    assert gen.state is State.COMPLETED
    assert seen == states


def test_returns_failed_state_without_raising():
    gen = poll_until_terminal(
        lambda gid: _gen(State.FAILED),
        "g1",
        PollConfig(initial_delay=0, interval=0),
        sleep=lambda d: None,
    )
    assert gen.state is State.FAILED


def test_timeout():
    clock = {"t": 0.0}

    def fake_clock():
        return clock["t"]

    def fake_sleep(d):
        clock["t"] += 10.0

    with pytest.raises(PollTimeout):
        poll_until_terminal(
            lambda gid: _gen(State.PROCESSING),
            "g1",
            PollConfig(initial_delay=0, interval=1, timeout=5),
            sleep=fake_sleep,
            clock=fake_clock,
        )
