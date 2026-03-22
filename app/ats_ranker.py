"""
AI-powered ATS Resume Ranking Engine.

Uses OpenAI GPT-4o-mini to:
  1. Extract candidate info (name, email, phone) from resume text
  2. Score each resume 0-100 against a job description
  3. Rank and select top N% of candidates

The ranking considers:
  - Skills match
  - Experience relevance
  - Education fit
  - Overall suitability
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── Prompts ─────────────────────────────────────────────────────

EXTRACT_AND_SCORE_PROMPT = """You are an expert HR recruiter and ATS (Applicant Tracking System) analyst.

Given a RESUME and a JOB DESCRIPTION, do the following:

1. **Extract candidate info** from the resume:
   - full_name: The candidate's full name
   - email: Their email address (or "" if not found)
   - phone: Their phone number exactly as written (or "" if not found)
   - current_title: Their most recent job title (or "" if not found)
   - years_experience: Estimated total years of work experience (integer, 0 if unclear)

2. **Score the resume** against the job description on these criteria (each 0-25):
   - skills_match: How well do their skills match what's required?
   - experience_relevance: How relevant is their work experience?
   - education_fit: How appropriate is their education/qualifications?
   - overall_suitability: Overall fit for this specific role?

3. **Total score**: Sum of all four scores (0-100)

4. **Reasoning**: A brief 1-2 sentence explanation of the score

Respond ONLY with valid JSON in this exact format:
{{
  "full_name": "...",
  "email": "...",
  "phone": "...",
  "current_title": "...",
  "years_experience": 0,
  "skills_match": 0,
  "experience_relevance": 0,
  "education_fit": 0,
  "overall_suitability": 0,
  "total_score": 0,
  "reasoning": "..."
}}

JOB DESCRIPTION:
---
{job_description}
---

RESUME:
---
{resume_text}
---

Respond with ONLY the JSON object, no markdown formatting."""


class ATSRanker:
    """AI-powered resume ranking engine using OpenAI."""

    def __init__(self, openai_api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = openai_api_key
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def score_resume(
        self,
        resume_text: str,
        job_description: str,
    ) -> dict:
        """
        Score a single resume against a job description.

        Returns dict with candidate info + scores.
        """
        # Truncate very long resumes to stay within token limits
        if len(resume_text) > 8000:
            resume_text = resume_text[:8000] + "\n...[truncated]"

        prompt = EXTRACT_AND_SCORE_PROMPT.format(
            job_description=job_description,
            resume_text=resume_text,
        )

        client = await self._get_client()

        try:
            resp = await client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You are an expert ATS resume analyser. Always respond with valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1024,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()

            # Clean markdown code blocks if present
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            # Extract JSON object if surrounded by extra text
            brace_start = content.find("{")
            brace_end = content.rfind("}")
            if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
                content = content[brace_start : brace_end + 1]

            result = json.loads(content)

            # Validate and clamp scores
            for key in ("skills_match", "experience_relevance", "education_fit", "overall_suitability"):
                result[key] = max(0, min(25, int(result.get(key, 0))))
            result["total_score"] = sum(
                result[k] for k in ("skills_match", "experience_relevance", "education_fit", "overall_suitability")
            )

            return result

        except json.JSONDecodeError as e:
            log.error("ats_json_parse_error", error=str(e), content=content[:200] if 'content' in dir() else "")
            return _empty_result(f"Failed to parse AI response: {e}")
        except httpx.HTTPStatusError as e:
            log.error("ats_api_error", status=e.response.status_code, error=str(e))
            return _empty_result(f"OpenAI API error: {e.response.status_code}")
        except Exception as e:
            log.error("ats_score_error", error=str(e))
            return _empty_result(f"Scoring error: {e}")

    async def rank_resumes(
        self,
        resumes: list[dict],
        job_description: str,
        top_percent: float = 0.30,
        concurrency: int = 5,
    ) -> dict:
        """
        Score and rank multiple resumes against a job description.

        Args:
            resumes: List of {"filename": str, "text": str}
            job_description: The job description to score against
            top_percent: Fraction of top candidates to select (0.30 = top 30%)
            concurrency: Max concurrent API calls

        Returns:
            {
                "all_ranked": [...],  # All resumes ranked by score (descending)
                "selected": [...],    # Top N% candidates
                "rejected": [...],    # Below cutoff
                "cutoff_score": int,
                "stats": {...}
            }
        """
        sem = asyncio.Semaphore(concurrency)
        results = []

        async def _score_one(resume: dict) -> dict:
            async with sem:
                try:
                    log.info("scoring_resume", filename=resume["filename"])
                    score_data = await self.score_resume(resume["text"], job_description)
                    return {
                        "filename": resume["filename"],
                        "resume_text": resume["text"][:500],  # Preview only
                        **score_data,
                    }
                except Exception as e:
                    log.error("score_one_error", filename=resume.get("filename"), error=str(e))
                    return {
                        "filename": resume.get("filename", "unknown"),
                        "resume_text": resume.get("text", "")[:500],
                        **_empty_result(f"Scoring failed: {e}"),
                    }

        # Score all resumes concurrently
        tasks = [_score_one(r) for r in resumes if r.get("text")]
        scored = await asyncio.gather(*tasks)

        # Sort by total_score descending
        scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

        # Select top N%
        n_select = max(1, int(len(scored) * top_percent))
        selected = scored[:n_select]
        rejected = scored[n_select:]

        cutoff = selected[-1]["total_score"] if selected else 0

        stats = {
            "total_resumes": len(resumes),
            "parseable": len(scored),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
            "top_percent": int(top_percent * 100),
            "cutoff_score": cutoff,
            "avg_score": round(sum(s["total_score"] for s in scored) / max(len(scored), 1), 1),
            "max_score": scored[0]["total_score"] if scored else 0,
            "min_score": scored[-1]["total_score"] if scored else 0,
        }

        log.info(
            "ranking_complete",
            total=len(scored),
            selected=len(selected),
            cutoff=cutoff,
            avg_score=stats["avg_score"],
        )

        return {
            "all_ranked": scored,
            "selected": selected,
            "rejected": rejected,
            "cutoff_score": cutoff,
            "stats": stats,
        }


def _empty_result(reason: str) -> dict:
    """Return a zeroed-out result dict for error cases."""
    return {
        "full_name": "",
        "email": "",
        "phone": "",
        "current_title": "",
        "years_experience": 0,
        "skills_match": 0,
        "experience_relevance": 0,
        "education_fit": 0,
        "overall_suitability": 0,
        "total_score": 0,
        "reasoning": reason,
    }
