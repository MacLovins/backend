"""GoldenOracleLLM — an offline stand-in for the extraction model, built from the golden labels.

It answers extract_signals prompts by citing every labelled evidence quote that appears in a shown snippet —
"accept" items and the traps code must catch (reason wrong_subject or homonym) alike — always claiming
subject = target_company, strength strong, confidence 0.9. It is a perfect reader but a gullible one about
who the text is about. Semantic traps (vendor, other_company, not_an_attack, …) are judgement calls of the
model (prompt rules 3 and 4): the oracle follows those rules and does not cite them. Run through the real
pipeline it measures what the code controls, without network or quota:
- recall — did the prefilter show the snippet with the evidence, and did verification keep it?
- precision — did the entity filter and the code checks of verification (V2 subject, homonyms) stop the
  traps the model fell for?
It says nothing about the real model's judgement; that is what `lr-ai eval --live` / `--cache-only` measure.
"""

import re
from collections import defaultdict

from pydantic import BaseModel

from leadradar_ai.evals.golden import CompanyCase, EvidenceLabel, GoldenLabel
from leadradar_ai.llm.types import LLMRequest, LLMResult
from leadradar_ai.presets import load_preset
from leadradar_ai.verification.quotes import find_quote, normalize

MODEL = "golden-oracle"
MAX_EVIDENCE = 3  # schema limit per answer
CODE_CHECKED_TRAPS = frozenset({"wrong_subject", "homonym"})

_COMPANY = re.compile(r"<company>[^·]*·\s*([^\s·]+)")
_SNIPPET = re.compile(r'<snippet id="(S\d+)"[^>]*?title="([^"]*)">(.*?)</snippet>', re.DOTALL)
_QUESTION = re.compile(r'<question id="(Q\d+)"[^>]*>(.*?)</question>', re.DOTALL)


class GoldenOracleLLM:
    def __init__(self, labels: list[GoldenLabel], companies: dict[str, CompanyCase] | None = None) -> None:
        self.evidence: dict[tuple[str, str, str], list[EvidenceLabel]] = defaultdict(list)
        for label in labels:
            self.evidence[(label.company_domain, label.service, label.question_key)].extend(
                e for e in label.evidence if e.expected == "accept" or e.reason in CODE_CHECKED_TRAPS
            )
        self.by_text: dict[str, tuple[str, str]] = {}  # normalized question text → (service, key)
        for service in sorted({label.service for label in labels}):
            for q in load_preset(service).questions:
                self.by_text[normalize(q.text)] = (service, q.key)
        self.domains = {c.domain for c in (companies or {}).values()}
        self.calls: list[LLMRequest] = []

    def _answer(self, qid: str, text: str, domain: str, snippets: list[tuple[str, str, str]]) -> dict:
        service_key = self.by_text.get(normalize(text))
        items = self.evidence.get((domain, *service_key), []) if service_key else []
        evidence = []
        for item in items:
            for sid, title, body in snippets:
                if find_quote(item.quote, body) or find_quote(item.quote, title):
                    evidence.append(
                        {
                            "snippet_id": sid,
                            "quote": item.quote,
                            "subject": "target_company",
                            "event_date": None,
                            "strength": "strong",
                            "summary": item.quote[:200],
                        }
                    )
                    break
            if len(evidence) == MAX_EVIDENCE:
                break
        return {
            "question_id": qid,
            "answer": "yes" if evidence else "unclear",
            "confidence": 0.9 if evidence else 0.3,
            "evidence": evidence,
            "rationale": "golden evidence found in the snippets" if evidence else "no golden evidence shown",
        }

    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        if request.purpose != "extract_signals":
            raise ValueError(f"GoldenOracleLLM answers extract_signals only, not {request.purpose}")
        self.calls.append(request)
        user = request.user.split("</examples>")[-1]
        company = _COMPANY.search(user)
        domain = company.group(1).strip() if company else ""
        snippets = [(sid, title, body) for sid, title, body in _SNIPPET.findall(user)]
        answers = [self._answer(qid, text, domain, snippets) for qid, text in _QUESTION.findall(user)]
        return LLMResult(output=request.output_model.model_validate({"answers": answers}), model=MODEL)
