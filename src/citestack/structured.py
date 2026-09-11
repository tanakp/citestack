"""Provider-independent structured generation. Never returns unvalidated model data."""

import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from citestack.runtime import remaining_seconds, request_id_var

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("citestack.structured")
ErrorCode = Literal[
    "invalid_json",
    "invalid_schema",
    "invalid_semantics",
    "output_too_large",
    "provider_timeout",
    "generation_deadline",
    "provider_unavailable",
    "provider_http_error",
    "provider_response_invalid",
    "fallback_invalid",
]


class AttemptError(BaseModel):
    attempt: int
    code: ErrorCode
    # Field names come from the trusted schema, never arbitrary model output.
    fields: list[str] = Field(default_factory=list)


class StructuredResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "fallback", "failed"]
    data: T | None
    attempts: int
    errors: list[AttemptError]
    schema_name: str
    elapsed_ms: float


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    system: str
    json_schema: dict
    attempt: int
    timeout_seconds: float = 90


class ProviderFailure(Exception):
    """Adapters expose safe error codes, not provider response bodies or credentials."""

    def __init__(self, code: ErrorCode, *, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class InvalidJSON(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidJSON("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise InvalidJSON("Non-finite JSON number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise InvalidJSON("Non-finite JSON number")
    return parsed


def parse_output(raw: str, schema: type[T], max_chars: int = 32_000) -> T:
    """Allow a single outer code fence, but never guess at missing JSON or extract prose."""
    if not isinstance(raw, str):
        raise InvalidJSON("Provider output must be a string")
    if len(raw) > max_chars:
        raise ProviderFailure("output_too_large", retryable=True)
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.S | re.I)
    if fence:
        text = fence[1].strip()
    try:
        json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as error:
        raise InvalidJSON("Invalid JSON") from error
    # Validate from JSON so JSON-native dates/UUIDs retain Pydantic's strict semantics.
    # Runtime extra=forbid applies to nested models too, independent of their defaults.
    return schema.model_validate_json(text, strict=True, extra="forbid")


class StructuredOutputEngine:
    def __init__(
        self,
        provider: Callable[[GenerationRequest], str],
        *,
        max_attempts: int = 2,
        max_output_chars: int = 32_000,
        retry_delay: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        budget_seconds: float = 90,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if not 1 <= max_output_chars <= 1_000_000:
            raise ValueError("max_output_chars must be between 1 and 1,000,000")
        if not 0 <= retry_delay <= 5:
            raise ValueError("retry_delay must be between 0 and 5 seconds")
        if not 0 < budget_seconds <= 300:
            raise ValueError("budget_seconds must be between 0 and 300 seconds")
        self.budget_seconds, self.clock = budget_seconds, clock
        self.provider = provider
        self.max_attempts, self.max_output_chars = max_attempts, max_output_chars
        self.retry_delay, self.sleep = retry_delay, sleep

    def run(
        self,
        schema: type[T],
        prompt: str,
        *,
        system: str = "Return only JSON matching the supplied schema.",
        validate: Callable[[T], None] | None = None,
        fallback: T | dict | None = None,
    ) -> StructuredResult[T]:
        start = time.perf_counter()
        deadline = self.clock() + remaining_seconds(self.budget_seconds)
        errors: list[AttemptError] = []
        json_schema = schema.model_json_schema()

        def checked(raw: str) -> T:
            value = parse_output(raw, schema, self.max_output_chars)
            if validate:
                validate(value)
                # Business validators may canonicalize values, such as source quotes.
                # Revalidate their result so mutation cannot bypass the output schema.
                value = parse_output(
                    value.model_dump_json(warnings=False), schema, self.max_output_chars
                )
            return value

        def finish(status, data, attempts):
            result = StructuredResult[schema](
                status=status,
                data=data,
                attempts=attempts,
                errors=errors,
                schema_name=schema.__name__,
                elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            logger.info(
                json.dumps(
                    {
                        "event": "structured_output",
                        "request_id": request_id_var.get(),
                        "schema": schema.__name__,
                        "status": status,
                        "attempts": attempts,
                        "error_codes": [error.code for error in errors],
                        "elapsed_ms": result.elapsed_ms,
                    }
                )
            )
            return result

        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            remaining = min(deadline - self.clock(), remaining_seconds(self.budget_seconds))
            if remaining <= 0:
                errors.append(AttemptError(attempt=attempts, code="generation_deadline"))
                break
            attempts = attempt
            request_prompt = prompt
            if errors:
                # Only safe diagnostics are fed back; no raw failed output is replayed.
                request_prompt += (
                    "\nReturn a corrected, complete JSON object matching the schema. "
                    "Use the original input. Do not invent missing facts. "
                    "Previous validation feedback: " + errors[-1].model_dump_json()
                )
            retryable = True
            try:
                raw = self.provider(
                    GenerationRequest(
                        request_prompt,
                        system,
                        json_schema,
                        attempt,
                        remaining,
                    )
                )
                if self.clock() >= deadline:
                    raise ProviderFailure("generation_deadline")
                value = checked(raw)
                return finish("success", value, attempt)
            except ProviderFailure as error:
                failure = AttemptError(attempt=attempt, code=error.code)
                retryable = error.retryable
            except InvalidJSON:
                failure = AttemptError(attempt=attempt, code="invalid_json")
            except ValidationError as error:
                known_fields = set(schema.model_fields)
                fields = sorted(
                    {
                        issue["loc"][0]
                        for issue in error.errors(include_input=False, include_context=False)
                        if issue["loc"] and issue["loc"][0] in known_fields
                    }
                )
                failure = AttemptError(attempt=attempt, code="invalid_schema", fields=fields)
            except ValueError:
                failure = AttemptError(attempt=attempt, code="invalid_semantics")
            errors.append(failure)
            if not retryable or attempt == self.max_attempts:
                break
            delay = min(self.retry_delay * 2 ** (attempt - 1), 5)
            if deadline - self.clock() <= delay:
                errors.append(AttemptError(attempt=attempts, code="generation_deadline"))
                break
            self.sleep(delay)

        if fallback is not None:
            try:
                raw = (
                    fallback.model_dump_json(warnings=False)
                    if isinstance(fallback, BaseModel)
                    else json.dumps(fallback, allow_nan=False)
                )
                return finish("fallback", checked(raw), attempts)
            except (ValueError, TypeError, ProviderFailure):
                errors.append(AttemptError(attempt=attempts, code="fallback_invalid"))
        return finish("failed", None, attempts)
