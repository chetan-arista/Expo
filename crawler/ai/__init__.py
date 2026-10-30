"""
Provider-neutral AI layer (free tiers first) and the AI-assisted features built on it.

Every feature that uses it must stay fully useful when the client returns None (no provider
configured, quota used up, provider error, job stopped): it keeps its rule-based result and says so
with fallback_note() (AI_OFF_NOTE, QUOTA_NOTE, STOPPED_NOTE ...). Background jobs wrap their work in
job_scope(stop_event) so a stopped job (or a new crawl) ends its AI calls at once.

Named call budgets (client.BUDGETS: crawl, assistant, ai_tab, embeddings, search) keep one feature from using up
another's calls; chat_tools() is the function-calling turn of Ask ARACHNIX. The AI tab (prompts.py, runner.py)
runs per-page prompts on its own "ai_tab" budget. embeddings.py turns page text into vectors (Gemini's embedding
API or a local Ollama model) for Semantically Similar / Low Relevance, topic clusters and semantic search
(crawler/seo/semantic.py), on the "embeddings" (per run) and "search" (per hour) budgets.
"""
from .client import (AI_OFF_NOTE, BUDGETS, QUOTA_NOTE, STOPPED_NOTE, ai_status, ask_json, budget_left, budgets,
                     cancel_check, cancelled, chat_tools, fallback_note, gather_evidence, job_scope, last_outcome,
                     quota_reached, research, reset_usage, start_budget, usage_summary, wrap_page)

__all__ = ["AI_OFF_NOTE", "BUDGETS", "QUOTA_NOTE", "STOPPED_NOTE", "ai_status", "ask_json", "budget_left", "budgets",
           "cancel_check", "cancelled", "chat_tools", "fallback_note", "gather_evidence", "job_scope",
           "last_outcome", "quota_reached", "research", "reset_usage", "start_budget", "usage_summary", "wrap_page"]
