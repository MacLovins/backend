"""Outreach message generation module (AI-19, SPEC §1.4.2).

Generates tailored cold emails, LinkedIn InMail, and call scripts grounded in
verified company signals and service value propositions.

referenced_signals holds signal ids (StoredSignal.id) — never question ids. Ids come only from the signals
passed in: ids the model returns are filtered against them. Plain VerifiedSignal inputs have no id, so
callers should pass StoredSignal to get references.
"""

from uuid import UUID

from leadradar_ai.contracts import (
    CompanyProfile,
    OutreachDraft,
    OutreachRequest,
    ServiceBundle,
    StoredSignal,
    VerifiedSignal,
)
from leadradar_ai.llm.types import LLMClient, LLMRequest

PROMPT_VERSION = "outreach@v2"  # v2: signal ids in the prompt, referenced_signals = signal ids

SYSTEM_PROMPT = """You are an elite B2B Sales Development Representative (SDR) and Copywriter.
Your task is to generate a highly personalized, compelling outreach message (Cold Email, LinkedIn InMail, or Call Script) to a target company.

CRITICAL RULES:
1. Ground your message in the VERIFIED SIGNALS provided. Reference specific triggers, facts, and challenges.
2. Align the value proposition of the offered service directly with the detected situation.
3. Keep the tone natural, crisp, and executive-level. Avoid generic buzzwords or hollow praise.
4. Provide a clear, low-friction Call to Action (CTA).
5. Output MUST match the requested OutreachDraft JSON schema exactly.
6. referenced_signals: only the "Signal id" values of the signals you used (never question names)."""


def build_outreach_prompt(
    company: CompanyProfile,
    service: ServiceBundle,
    signals: list[VerifiedSignal],
    request: OutreachRequest,
) -> str:
    signals_summary = []
    for s in signals:
        quote_str = f' | Quote: "{s.quote}"' if s.quote else ""
        signal_id = f"  Signal id: {s.id}\n" if isinstance(s, StoredSignal) else ""
        signals_summary.append(
            f"- Signal Question: {s.question_key}\n"
            f"{signal_id}"
            f"  Summary: {s.summary}\n"
            f"  Confidence: {s.confidence:.2f}{quote_str}\n"
            f"  Source: {s.source_name} ({s.url})"
        )

    signals_text = "\n".join(signals_summary) if signals_summary else "No specific triggers found."

    return (
        f"TARGET COMPANY:\n"
        f"Name: {company.name}\n"
        f"Domain: {company.domain}\n"
        f"Industry: {', '.join(company.industry_ids) if company.industry_ids else 'Enterprise'}\n"
        f"Employees: {company.employees or 'Unknown'}\n\n"
        f"OFFERED SERVICE:\n"
        f"Name: {service.name}\n"
        f"Description: {service.description}\n"
        f"Value Proposition: {service.value_proposition}\n\n"
        f"DETECTED VERIFIED SIGNALS & TIMING TRIGGERS:\n"
        f"{signals_text}\n\n"
        f"OUTREACH PREFERENCES:\n"
        f"Channel: {request.channel}\n"
        f"Tone: {request.tone}\n"
        f"Language: {request.language}\n"
        f"Sender: {request.sender_name or 'Account Executive'}, {request.sender_title or 'Growth Lead'} at {request.sender_company}\n\n"
        f"Write a high-converting {request.channel} addressing their situation."
    )


def signal_ids(signals: list[VerifiedSignal]) -> list[UUID]:
    return [s.id for s in signals if isinstance(s, StoredSignal)]


def _grounded(draft: OutreachDraft, signals: list[VerifiedSignal]) -> OutreachDraft:
    """Keep only references to the given signals; without valid ones, cite the top three."""
    known = signal_ids(signals)
    cited = [i for i in dict.fromkeys(draft.referenced_signals) if i in set(known)]
    return draft.model_copy(update={"referenced_signals": cited or known[:3]})


def fallback_draft(
    company: CompanyProfile,
    service: ServiceBundle,
    signals: list[VerifiedSignal],
    request: OutreachRequest,
) -> OutreachDraft:
    top_signal = signals[0] if signals else None
    hook = top_signal.summary if top_signal else f"observed strategic developments at {company.name}"
    quotes = [s.quote for s in signals if s.quote][:2]

    sender_name = request.sender_name or "Sales Team"
    sender_co = request.sender_company or "LeadRadar"

    if request.channel == "linkedin_inmail":
        subject = f"Connecting re: {company.name} and {service.name}"
        body = (
            f"Hi team at {company.name},\n\n"
            f"I noticed {hook.lower()}.\n\n"
            f"We help organizations in your space address this via {service.value_proposition.lower()}.\n\n"
            f"Worth a brief 10-minute exchange next week?\n\n"
            f"Best,\n{sender_name} | {sender_co}"
        )
    else:
        subject = f"Question regarding {company.name}'s initiatives in {service.name}"
        body = (
            f"Hi team,\n\n"
            f"I came across recent developments at {company.name} regarding: {hook}.\n\n"
            f"At {sender_co}, we help leaders tackle this challenge with {service.value_proposition.lower()}.\n\n"
            f"Would you be open to a quick 15-minute conversation on Thursday to see how we could assist?\n\n"
            f"Regards,\n{sender_name}\n{sender_co}"
        )

    return OutreachDraft(
        channel=request.channel,
        subject=subject,
        body=body,
        referenced_signals=signal_ids(signals[:3]),
        referenced_quotes=quotes,
        hook=hook,
        call_to_action="Brief 10-minute conversation this week",
        language=request.language,
    )


async def generate_outreach(
    llm: LLMClient,
    company: CompanyProfile,
    service: ServiceBundle,
    signals: list[VerifiedSignal],
    request: OutreachRequest | None = None,
) -> OutreachDraft:
    request = request or OutreachRequest()
    prompt = build_outreach_prompt(company, service, signals, request)

    llm_req = LLMRequest(
        purpose="outreach",
        prompt_version=PROMPT_VERSION,
        system=SYSTEM_PROMPT,
        user=prompt,
        output_model=OutreachDraft,
        pool="main",
    )

    try:
        res = await llm.generate(llm_req)
        if res.output is not None:
            return _grounded(res.output, signals)
    except Exception:
        pass

    return fallback_draft(company, service, signals, request)
