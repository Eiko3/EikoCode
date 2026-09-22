import pytest

from eikocode.errors import (
    ErrorKind,
    EikoCodeError,
    describe_unexpected,
    exit_code_for,
    message_for,
)


def test_missing_credential_message_lists_both_variables():
    text = message_for(ErrorKind.MISSING_CREDENTIAL)
    assert "EIKOCODE_ANTHROPIC_API_KEY" in text
    assert "EIKOCODE_OPENAI_API_KEY" in text


def test_timeout_message_states_the_first_byte_deadline():
    assert "30 秒未收到首个字节" in message_for(ErrorKind.TIMEOUT)


def test_rate_limit_message_states_retry_count():
    assert "重试 2 次" in message_for(ErrorKind.RATE_LIMIT)


@pytest.mark.parametrize(
    "kind,code",
    [
        (ErrorKind.MISSING_CREDENTIAL, 2),
        (ErrorKind.AUTH, 2),
        (ErrorKind.CONFIG, 2),
        (ErrorKind.NETWORK, 1),
        (ErrorKind.TIMEOUT, 1),
        (ErrorKind.RATE_LIMIT, 1),
        (ErrorKind.UNKNOWN, 1),
    ],
)
def test_exit_codes(kind, code):
    assert exit_code_for(kind) == code
    assert EikoCodeError(kind).exit_code == code


@pytest.mark.parametrize(
    "kind", [ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.TIMEOUT_IDLE]
)
def test_network_class_is_retryable(kind):
    assert EikoCodeError(kind).retryable is True


@pytest.mark.parametrize(
    "kind", [ErrorKind.AUTH, ErrorKind.RATE_LIMIT, ErrorKind.PROTOCOL, ErrorKind.CONFIG]
)
def test_non_network_class_is_not_retryable(kind):
    assert EikoCodeError(kind).retryable is False


def test_describe_unexpected_never_echoes_the_exception_message():
    """兜底文案只暴露类型名——第三方异常的信息里可能带着请求头，而请求头里有 Key。"""
    secret = "Bearer sk-ant-secret-value-1234"
    text = describe_unexpected(RuntimeError(secret))

    assert "sk-ant-secret-value-1234" not in text
    assert "RuntimeError" in text


def test_detail_is_never_part_of_the_user_message():
    error = EikoCodeError(ErrorKind.AUTH, detail="Authorization: sk-leaked")
    assert "sk-leaked" not in error.user_message


def test_interupt_message_is_stable():
    from eikocode.errors import INTERRUPT_MESSAGE

    assert INTERRUPT_MESSAGE == "已中断，本次回复未记入上下文。"
