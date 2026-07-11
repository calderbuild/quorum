"""Conflict adjudication backends.

QUORUM_MODE=sim (default) uses a deterministic heuristic: prefer the longer /
more specific of the two conflicting values, and record an explicit rationale
noting this was a heuristic (not LLM) decision. This keeps tests and demos
fully offline. Real adjudication requires QUORUM_MODE=openai or =bedrock.
"""

from pydantic import BaseModel

from app.config import get_settings


class AdjudicationResult(BaseModel):
    value: str
    rationale: str


def _sim_adjudicate(fact_a: str, fact_b: str, context: str) -> AdjudicationResult:
    winner, loser = (fact_a, fact_b) if len(fact_a) >= len(fact_b) else (fact_b, fact_a)
    return AdjudicationResult(
        value=winner,
        rationale=(
            f"[sim heuristic] Chose the more specific/longer value. "
            f"Kept: {winner!r}. Discarded (retained in history): {loser!r}."
        ),
    )


_ADJUDICATE_PROMPT = """Two AI agents concurrently wrote conflicting facts to shared memory.
Context: {context}
Fact A: {fact_a}
Fact B: {fact_b}

Decide the single fact that should be kept as the current shared truth. If both
are partially correct, synthesize a combined value. Respond with the resolved
value and a one-sentence rationale."""


async def adjudicate(fact_a: str, fact_b: str, context: str) -> AdjudicationResult:
    settings = get_settings()

    if settings.quorum_mode == "sim":
        return _sim_adjudicate(fact_a, fact_b, context)

    if settings.quorum_mode == "openai":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": _ADJUDICATE_PROMPT.format(
                        context=context, fact_a=fact_a, fact_b=fact_b
                    ),
                }
            ],
        )
        text = resp.choices[0].message.content or ""
        return AdjudicationResult(value=text, rationale="[openai] see value")

    if settings.quorum_mode == "bedrock":
        # AWS retired the classic boto3 bedrock-runtime InvokeModel catalog
        # for current Anthropic models in favor of the "Bedrock Mantle"
        # endpoint, which is authenticated with a Bedrock API key (not an
        # IAM/SigV4 credential) and speaks the standard Anthropic Messages
        # API shape via the Anthropic SDK itself, just pointed at a
        # different base_url. Confirmed live against this account's Bedrock
        # console (Live API docs page) on 2026-07-11 -- do not revert to
        # boto3 invoke_model() for Anthropic models without re-checking the
        # console, since the old catalog shows zero Anthropic serverless
        # models in this account/region.
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(
            base_url=f"https://bedrock-mantle.{settings.aws_region}.api.aws/anthropic",
            api_key=settings.bedrock_api_key,
        )
        resp = await client.messages.create(
            model="anthropic.claude-sonnet-5",
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": _ADJUDICATE_PROMPT.format(
                        context=context, fact_a=fact_a, fact_b=fact_b
                    ),
                }
            ],
        )
        text = resp.content[0].text
        return AdjudicationResult(value=text, rationale="[bedrock] see value")

    raise ValueError(f"Unknown QUORUM_MODE: {settings.quorum_mode}")
