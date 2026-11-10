"""chat-api: the application tier in front of the GPU-hosted LLM.

Request path:  client -> chat-api (CPU) -> vLLM (GPU) via the OpenAI-compatible API.

Why a separate app instead of calling vLLM directly:
  * owns input validation, the system prompt and response shape
  * enforces timeouts and a concurrency limit so the GPU isn't flooded
  * holds the LLM API key; callers never see it
  * scales on cheap CPU nodes independently of the GPU
"""

import asyncio
import os
import socket
import time

from fastapi import FastAPI, HTTPException, Response
from openai import APIError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, Field

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://llm.ai.svc.cluster.local:8000/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-0.5b-instruct")
LLM_API_KEY = os.environ["LLM_API_KEY"]            # fail fast if the Secret isn't mounted
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "60"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "32"))
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a concise assistant for AI infrastructure questions. Answer in at most 5 sentences.",
)
POD = socket.gethostname()

llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT_S, max_retries=1)
slots = asyncio.Semaphore(MAX_CONCURRENCY)
app = FastAPI(title="chat-api")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    max_tokens: int = Field(default=256, ge=1, le=1024)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


@app.get("/healthz")
async def healthz():
    """Liveness: the app process is running (does not depend on the LLM)."""
    return {"status": "ok", "pod": POD}


@app.get("/readyz")
async def readyz(response: Response):
    """Readiness: the LLM is reachable and serving the expected model."""
    try:
        models = await asyncio.wait_for(llm.models.list(), timeout=3)
        ids = [m.id for m in models.data]
        if LLM_MODEL not in ids:
            response.status_code = 503
            return {"ready": False, "reason": f"model {LLM_MODEL} not served", "served": ids}
        return {"ready": True, "model": LLM_MODEL, "pod": POD}
    except Exception as exc:  # LLM down, loading, or unreachable
        response.status_code = 503
        return {"ready": False, "reason": type(exc).__name__}


@app.post("/v1/ask")
async def ask(req: AskRequest):
    if slots.locked():
        # Backpressure: fail fast instead of queueing until timeouts.
        raise HTTPException(status_code=503, detail="busy, retry shortly", headers={"Retry-After": "1"})
    async with slots:
        start = time.perf_counter()
        try:
            resp = await llm.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": req.question},
                ],
                max_tokens=req.max_tokens,
                temperature=req.temperature,
            )
        except APITimeoutError:
            raise HTTPException(status_code=504, detail="LLM timed out")
        except APIError as exc:
            raise HTTPException(status_code=502, detail=f"LLM error: {type(exc).__name__}")
        latency = time.perf_counter() - start
        usage = resp.usage
        return {
            "answer": resp.choices[0].message.content,
            "model": resp.model,
            "finish_reason": resp.choices[0].finish_reason,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "latency_ms": round(latency * 1000),
            "tokens_per_s": round(usage.completion_tokens / latency, 1) if usage and latency > 0 else None,
            "served_by_app_pod": POD,
        }
