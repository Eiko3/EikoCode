import pytest

from eikocode import providers
from eikocode.config import Config
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.providers import base


class FakeTime:
    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def fake_time(monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(base, "time", clock)
    return clock


def _counting_factory(fail_times, error_kind=ErrorKind.NETWORK):
    state = {"calls": 0}

    def factory():
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise EikoCodeError(error_kind, "boom")
        yield "ok"

    return factory, state


def test_retries_twice_then_gives_up(fake_time):
    factory, state = _counting_factory(fail_times=99)

    with pytest.raises(EikoCodeError):
        base.run_stream_with_retry(factory, lambda chunk: None)

    assert state["calls"] == base.MAX_RETRIES + 1
    assert fake_time.sleeps == [1.0, 2.0]


def test_succeeds_after_one_retry(fake_time):
    factory, state = _counting_factory(fail_times=1)
    collected: list[str] = []

    base.run_stream_with_retry(factory, collected.append)

    assert state["calls"] == 2
    assert collected == ["ok"]
    assert fake_time.sleeps == [1.0]


def test_no_retry_once_content_was_emitted(fake_time):
    state = {"calls": 0}

    def factory():
        state["calls"] += 1
        yield "半截内容"
        raise EikoCodeError(ErrorKind.NETWORK, "boom")

    with pytest.raises(EikoCodeError):
        base.run_stream_with_retry(factory, lambda chunk: None)

    assert state["calls"] == 1
    assert fake_time.sleeps == []


def test_auth_errors_are_never_retried(fake_time):
    factory, state = _counting_factory(fail_times=99, error_kind=ErrorKind.AUTH)

    with pytest.raises(EikoCodeError):
        base.run_stream_with_retry(factory, lambda chunk: None)

    assert state["calls"] == 1
    assert fake_time.sleeps == []


def test_rate_limit_errors_are_never_retried(fake_time):
    factory, state = _counting_factory(fail_times=99, error_kind=ErrorKind.RATE_LIMIT)

    with pytest.raises(EikoCodeError):
        base.run_stream_with_retry(factory, lambda chunk: None)

    assert state["calls"] == 1
    assert fake_time.sleeps == []


def _config(**overrides):
    kwargs = dict(
        model="claude-sonnet-5",
        temperature=0.7,
        max_tokens=4096,
        context_limit=200000,
        known_models=(),
        anthropic_api_key="test-ant",
        openai_api_key="test-oai",
        openai_base_url=None,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def test_claude_prefix_selects_anthropic():
    provider = providers.select_provider(_config(), "claude-sonnet-5")
    assert provider.name == "anthropic"


def test_other_names_select_compat_provider():
    provider = providers.select_provider(_config(), "gpt-4o")
    assert provider.name == "openai_compat"


def test_anthropic_does_not_accept_sampling_params():
    provider = providers.select_provider(_config(), "claude-sonnet-5")
    assert provider.supports_temperature is False


def test_compat_provider_accepts_sampling_params():
    provider = providers.select_provider(_config(), "gpt-4o")
    assert provider.supports_temperature is True


def test_missing_credential_for_selected_provider():
    cfg = _config(anthropic_api_key=None)
    with pytest.raises(EikoCodeError) as excinfo:
        providers.select_provider(cfg, "claude-sonnet-5")
    assert excinfo.value.kind is ErrorKind.MISSING_CREDENTIAL
