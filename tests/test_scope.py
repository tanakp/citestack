import pytest

from citestack.answers import AnswerService
from citestack.scope import requires_private_context


@pytest.mark.parametrize(
    "question",
    [
        "Can you tell me the Kubernetes version my cluster runs?",
        "List our failed pods, please.",
        "Tell me which nodes in the production cluster have failed.",
        "What password is inside my postgres Secret?",
        "Why is our checkout service returning 502 today?",
        "Give me the logs from my API container.",
    ],
)
def test_private_state_requires_tools(question):
    assert requires_private_context(question)


@pytest.mark.parametrize(
    "question",
    [
        "How do I check which Kubernetes version my cluster runs?",
        "What command lists my Pods?",
        "Can you explain how I can inspect my node health?",
        "How can I troubleshoot our failing checkout Pod?",
        "What does CPU usage mean for my container?",
        "Where can I find logs for my Pods?",
    ],
)
def test_procedural_questions_still_use_documentation(question):
    assert not requires_private_context(question)


def test_private_state_is_declined_before_retrieval_or_generation(settings):
    class NoTools:
        def search(self, *args, **kwargs):
            raise AssertionError("Private state cannot be inferred by searching documentation")

    result = AnswerService(NoTools(), settings).answer("List our running pods.")
    assert result.abstained and not result.citations
    assert result.abstention_reason == "requires_private_context"
