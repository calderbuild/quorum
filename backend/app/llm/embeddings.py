"""Embedding backends.

QUORUM_MODE=sim (default) uses a deterministic, offline, zero-cost pseudo-embedding:
text is hashed into a seed, which drives a small PRNG to produce a unit vector.
Near-duplicate text (same words, minor edits) is NOT guaranteed to embed close
together the way a real model would -- this is a placeholder for correctness
testing and demos, not a semantic model. Real semantic retrieval requires
QUORUM_MODE=openai or QUORUM_MODE=bedrock.
"""

import hashlib
import math
import random

from app.config import get_settings


def _sim_embed(text: str, dim: int) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


async def embed(text: str) -> list[float]:
    settings = get_settings()

    if settings.quorum_mode == "sim":
        return _sim_embed(text, settings.embedding_dim)

    if settings.quorum_mode == "openai":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
            dimensions=settings.embedding_dim,
        )
        return resp.data[0].embedding

    if settings.quorum_mode == "bedrock":
        import json

        import boto3

        brt = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        body = json.dumps({"inputText": text})
        resp = brt.invoke_model(modelId="amazon.titan-embed-text-v2:0", body=body)
        payload = json.loads(resp["body"].read())
        return payload["embedding"]

    raise ValueError(f"Unknown QUORUM_MODE: {settings.quorum_mode}")


def vector_literal(vec: list[float]) -> str:
    """Render a python float list as a CockroachDB VECTOR literal, e.g. '[0.1,0.2]'."""
    return "[" + ",".join(repr(v) for v in vec) + "]"
