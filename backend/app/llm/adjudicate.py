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
        import json

        import boto3

        brt = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 300,
                "messages": [
                    {
                        "role": "user",
                        "content": _ADJUDICATE_PROMPT.format(
                            context=context, fact_a=fact_a, fact_b=fact_b
                        ),
                    }
                ],
            }
        )
        resp = brt.invoke_model(
            modelId="anthropic.claude-sonnet-4-6-v1:0",  # verify current Bedrock model id before use
            body=body,
        )
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"]
        return AdjudicationResult(value=text, rationale="[bedrock] see value")

    raise ValueError(f"Unknown QUORUM_MODE: {settings.quorum_mode}")
