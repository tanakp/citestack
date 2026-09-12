"""Conservative scope rules for a documentation-only service with no cluster tools.

These rules address explicit private-state requests, not general semantic entailment.
Procedural documentation questions remain retrievable. Ambiguous formulations can
escape these rules; keep adversarial cases and model-answer review in evaluation.
"""

import re

PROCEDURAL = re.compile(
    r"^(?:how (?:can|do|should|would|could) (?:i|we)|how to|"
    r"what (?:command|steps|procedure)|which (?:command|tool)|explain|describe|"
    r"where (?:can|do) (?:i|we) (?:find|look|check)|"
    r"can you (?:explain|show (?:me )?how))\b"
)
PRIVATE = re.compile(r"\b(?:my|our)\b|\b(?:this|the) (?:production|staging) cluster\b")
OBSERVABLE = re.compile(
    r"\b(?:pods?|nodes?|containers?|cluster|deployment|service|secret|password|"
    r"logs?|cpu|memory|version|ip|yaml|crash|unhealthy|running|installed)\b"
)
DEFINITION = re.compile(r"^what (?:does|is|are)\b.*\b(?:mean|purpose|difference|used for)\b")


def requires_private_context(question: str) -> bool:
    normalized = " ".join(question.casefold().split())
    if PROCEDURAL.search(normalized) or DEFINITION.search(normalized):
        return False
    return bool(PRIVATE.search(normalized) and OBSERVABLE.search(normalized))
