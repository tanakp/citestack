"""Deterministic engine demonstration; no API keys, network, models, or RAG index."""

import json

from pydantic import BaseModel, Field

from citestack.structured import StructuredOutputEngine


class DeploymentRequest(BaseModel):
    service: str
    replicas: int = Field(ge=1, le=20)


def main():
    responses = iter(
        [
            '{"service": "checkout", "replicas":',  # truncated JSON
            '{"service": "checkout", "replicas": "3"}',  # strict type mismatch
            '{"service": "checkout", "replicas": 3}',
        ]
    )
    recovered = StructuredOutputEngine(
        lambda request: next(responses), max_attempts=3, retry_delay=0
    ).run(DeploymentRequest, "Create a checkout deployment with 3 replicas.")

    fallback = StructuredOutputEngine(lambda request: "invalid", retry_delay=0).run(
        DeploymentRequest,
        "Create a checkout deployment with 3 replicas.",
        fallback={"service": "manual-review", "replicas": 1},
    )
    # Only data is returned; this example does not execute deployment operations.
    print(
        json.dumps(
            {"recovered": recovered.model_dump(), "fallback": fallback.model_dump()}, indent=2
        )
    )


if __name__ == "__main__":
    main()
