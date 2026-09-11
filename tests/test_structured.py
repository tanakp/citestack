import json
from datetime import date

import pytest
from pydantic import BaseModel, Field

from citestack.structured import ProviderFailure, StructuredOutputEngine


class Item(BaseModel):
    name: str
    count: int = Field(ge=0)


VALID = '{"name":"pods","count":3}'


def sequence(*outputs):
    pending, calls = iter(outputs), []

    def provider(request):
        calls.append(request)
        output = next(pending)
        if isinstance(output, Exception):
            raise output
        return output

    return provider, calls


def test_valid_json_returns_typed_model():
    provider, calls = sequence(VALID)
    result = StructuredOutputEngine(provider).run(Item, "input")
    assert isinstance(result.data, Item)
    assert result.status == "success" and result.attempts == 1 and not result.errors
    assert calls[0].json_schema == Item.model_json_schema()


def test_single_outer_code_fence_is_accepted():
    result = StructuredOutputEngine(lambda _: f"```json\n{VALID}\n```").run(Item, "input")
    assert result.status == "success" and result.data.count == 3


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"name":"pods",', "invalid_json"),
        ("Here is the answer: " + VALID, "invalid_json"),
        (VALID + VALID, "invalid_json"),
        ('{"name":"pods","count":1,"count":2}', "invalid_json"),
        ('{"name":"pods","count":NaN}', "invalid_json"),
        ('{"name":"pods","count":Infinity}', "invalid_json"),
        ('{"name":"pods","count":1e999}', "invalid_json"),
        ('{"name":"pods","count":"3"}', "invalid_schema"),
        ('{"name":"pods","count":true}', "invalid_schema"),
        ('{"name":"pods"}', "invalid_schema"),
        ('{"name":"pods","count":-1}', "invalid_schema"),
        ('{"name":"pods","count":3,"extra":"hidden"}', "invalid_schema"),
        ("null", "invalid_schema"),
        ("[]", "invalid_schema"),
    ],
)
def test_invalid_output_retries_with_safe_feedback(raw, code):
    provider, calls = sequence(raw, VALID)
    result = StructuredOutputEngine(provider, retry_delay=0).run(Item, "original input")
    assert result.status == "success" and result.attempts == 2
    assert result.errors[0].code == code
    assert code in calls[1].prompt and "original input" in calls[1].prompt
    assert calls[0].prompt == "original input"


def test_nested_extra_fields_are_rejected_even_with_default_model_config():
    class Nested(BaseModel):
        item: Item

    raw = '{"item":{"name":"pods","count":1,"secret":"do not return this"}}'
    result = StructuredOutputEngine(lambda _: raw, max_attempts=1).run(Nested, "input")
    assert result.status == "failed" and result.data is None
    assert "do not return this" not in result.model_dump_json()


def test_strict_json_supports_date_fields():
    class Dated(BaseModel):
        day: date

    result = StructuredOutputEngine(lambda _: '{"day":"2026-09-11"}').run(Dated, "input")
    assert result.data.day == date(2026, 9, 11)


def test_retry_budget_backoff_and_fallback_are_explicit():
    provider, calls = sequence("broken", "broken", "broken")
    sleeps = []
    result = StructuredOutputEngine(provider, max_attempts=3, sleep=sleeps.append).run(
        Item, "input", fallback={"name": "unknown", "count": 0}
    )
    assert result.status == "fallback" and result.data.count == 0
    assert result.attempts == len(calls) == 3 and sleeps == [0.25, 0.5]


def test_invalid_fallback_never_escapes_validation():
    invalid = Item.model_construct(name="pods", count="not a number")
    result = StructuredOutputEngine(lambda _: "broken", max_attempts=1).run(
        Item, "input", fallback=invalid
    )
    assert result.status == "failed" and result.data is None
    assert result.errors[-1].code == "fallback_invalid"


def test_business_validation_is_retried_and_applies_to_fallback():
    def validate(item):
        if item.count > 2:
            raise ValueError("sensitive internal diagnostic")

    provider, _ = sequence(VALID, '{"name":"pods","count":2}')
    result = StructuredOutputEngine(provider, retry_delay=0).run(Item, "input", validate=validate)
    assert result.status == "success" and result.errors[0].code == "invalid_semantics"
    failed = StructuredOutputEngine(lambda _: VALID, max_attempts=1).run(
        Item, "input", validate=validate, fallback={"name": "pods", "count": 4}
    )
    assert failed.status == "failed" and failed.data is None
    assert "sensitive internal diagnostic" not in failed.model_dump_json()


def test_business_validator_cannot_mutate_output_into_invalid_schema():
    def corrupt(item):
        item.count = -1

    result = StructuredOutputEngine(lambda _: VALID, max_attempts=1).run(
        Item, "input", validate=corrupt
    )
    assert result.status == "failed" and result.data is None


def test_oversized_output_rejected_before_parsing():
    result = StructuredOutputEngine(lambda _: "x" * 101, max_output_chars=100, max_attempts=1).run(
        Item, "input"
    )
    assert result.errors[0].code == "output_too_large"


def test_terminal_provider_failure_does_not_retry():
    provider, calls = sequence(ProviderFailure("provider_timeout"))
    result = StructuredOutputEngine(provider, max_attempts=5).run(Item, "input")
    assert len(calls) == 1 and result.status == "failed"


def test_transient_provider_failure_can_recover():
    provider, calls = sequence(ProviderFailure("provider_http_error", retryable=True), VALID)
    result = StructuredOutputEngine(provider, retry_delay=0).run(Item, "input")
    assert len(calls) == 2 and result.status == "success"


def test_diagnostics_and_logs_do_not_expose_input_or_raw_output(caplog):
    caplog.set_level("INFO", logger="citestack.structured")
    raw = json.dumps(
        {
            "name": "private customer name",
            "count": "private response value",
            "private field name": "private field value",
        }
    )
    provider, calls = sequence(raw, VALID)
    result = StructuredOutputEngine(provider, retry_delay=0).run(Item, "private prompt value")
    serialized = result.model_dump_json() + caplog.text
    for sensitive in [
        "private customer name",
        "private response value",
        "private field name",
        "private field value",
        "private prompt value",
    ]:
        assert sensitive not in serialized
    assert "private response value" not in calls[1].prompt


def test_unexpected_programming_errors_are_not_silently_swallowed():
    def bug(_):
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError):
        StructuredOutputEngine(bug).run(Item, "input")


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"max_attempts": 6}, {"retry_delay": -1}, {"max_output_chars": 0}],
)
def test_invalid_engine_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        StructuredOutputEngine(lambda _: VALID, **kwargs)
