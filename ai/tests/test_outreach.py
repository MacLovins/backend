import asyncio
from uuid import uuid4

from leadradar_ai import (
    ICPConfig,
    OutreachRequest,
    ScoringProfile,
    ServiceBundle,
    VerifiedSignal,
    generate_outreach,
)
from leadradar_ai.testing import FakeLLM
from leadradar_ai.testing.factories import make_company, make_question, make_signal

SERVICE = ServiceBundle(
    service_id=uuid4(),
    key="robotics",
    name="Robotics Automation Suite",
    description="Autonomous sorting and warehouse robotics.",
    value_proposition="Reduces sorting errors by 40% and speeds fulfillment by 3x.",
    questions=[],
    icp=ICPConfig(),
    rules=[],
    scoring=ScoringProfile(id=uuid4(), version=1),
)
QUESTION = make_question(key="logistics_automation", category="ai_automation")


def signals():
    return [
        make_signal(
            QUESTION,
            quote="DHL Group expands autonomous warehouse sorting across 12 European hubs.",
            summary="DHL investing heavily in warehouse sorting automation across Europe",
            confidence=0.92,
        ),
        make_signal(
            QUESTION, quote="DHL pilots robotic picking in Leipzig.", summary="Robotic picking pilot."
        ),
    ]


def run(coro):
    return asyncio.run(coro)


def test_fallback_draft_references_signal_ids_not_question_ids() -> None:
    sigs = signals()
    llm = FakeLLM(lambda request: ValueError("model down"))
    draft = run(generate_outreach(llm, make_company(), SERVICE, sigs, OutreachRequest(channel="email")))

    assert draft.channel == "email"
    assert "DHL" in draft.subject
    assert "warehouse sorting automation" in draft.body
    assert draft.referenced_signals == [s.id for s in sigs]
    assert QUESTION.id not in draft.referenced_signals
    assert "warehouse sorting" in draft.referenced_quotes[0]


def test_llm_references_are_filtered_to_the_given_signals() -> None:
    sigs = signals()
    invented = uuid4()

    def answer(request):
        assert f"Signal id: {sigs[1].id}" in request.user  # the model sees the ids it may cite
        return {
            "channel": "email",
            "subject": "Hi",
            "body": "Body",
            "referenced_signals": [str(QUESTION.id), str(invented), str(sigs[1].id), str(sigs[1].id)],
            "call_to_action": "Call?",
        }

    draft = run(generate_outreach(FakeLLM(answer), make_company(), SERVICE, sigs))
    assert draft.referenced_signals == [sigs[1].id]

    nothing_valid = FakeLLM(lambda r: answer(r) | {"referenced_signals": [str(QUESTION.id)]})
    assert run(generate_outreach(nothing_valid, make_company(), SERVICE, sigs)).referenced_signals == [
        s.id for s in sigs
    ]


def test_signals_without_ids_give_no_references() -> None:
    plain = [VerifiedSignal(**s.model_dump(include=set(VerifiedSignal.model_fields))) for s in signals()]
    llm = FakeLLM(lambda request: ValueError("model down"))
    assert run(generate_outreach(llm, make_company(), SERVICE, plain)).referenced_signals == []
