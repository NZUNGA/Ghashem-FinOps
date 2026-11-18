from datetime import timedelta
from django.db.models import Sum, Avg
from core.models import Client, Finding, PipelineRun, DailyMetric
from pipeline.finding_utils import upsert_finding

USE_CASE_THRESHOLDS = {
    "summar":  (0.08, "summarization"),
    "classif": (0.05, "classification"),
    "extract": (0.06, "extraction"),
    "embed":   (None, "embeddings"),
    "chat":    (0.25, "chat"),
    "assist":  (0.25, "assistant"),
    "code":    (0.40, "code generation"),
    "search":  (0.10, "search/retrieval"),
}
DEFAULT_THRESHOLD = (0.20, "general")
PROMPT_COST_PER_1K    = 0.003
GENERATED_COST_PER_1K = 0.006


def _infer_use_case(name: str):
    name_lower = name.lower()
    for kw, (thr, label) in USE_CASE_THRESHOLDS.items():
        if kw in name_lower:
            return thr, label
    return DEFAULT_THRESHOLD


def run(client: Client, run_obj: PipelineRun) -> int:
    """Runs token efficiency analysis. Returns number of findings created."""
    cog_ns = "Microsoft.CognitiveServices/accounts"

    # Get all OpenAI resources for this client with token metrics
    resource_ids = (
        DailyMetric.objects
        .filter(client=client, metric_namespace=cog_ns,
                metric_name="ProcessedPromptTokens")
        .values_list("resource_id", flat=True)
        .distinct()
    )

    created = 0

    for rid in resource_ids:
        prompt_qs = DailyMetric.objects.filter(
            client=client, resource_id=rid,
            metric_name="ProcessedPromptTokens",
        )
        gen_qs = DailyMetric.objects.filter(
            client=client, resource_id=rid,
            metric_name="GeneratedTokens",
        )

        total_prompt    = prompt_qs.aggregate(s=Sum("value"))["s"] or 0
        total_generated = gen_qs.aggregate(s=Sum("value"))["s"] or 0
        # Count distinct (year, month) pairs — not bare months — so a
        # resource with data in Nov 2024 and Nov 2025 counts as 2 months,
        # not 1. Using date__month alone collapses same-month-different-year.
        months_active   = prompt_qs.values("date__year", "date__month").distinct().count() or 1

        if total_prompt == 0:
            continue

        monthly_prompt    = total_prompt / months_active
        monthly_generated = total_generated / months_active
        ratio = total_generated / total_prompt

        resource_name = (prompt_qs.first().resource_name or rid)
        resource_group = (prompt_qs.first().resource_group or "")

        threshold, use_case = _infer_use_case(resource_name)
        if threshold is None:
            continue
        if ratio >= threshold:
            continue

        ideal_prompt     = monthly_generated / threshold if threshold else 0
        excess_prompt    = monthly_prompt - ideal_prompt
        wasted_pct       = max(0.0, excess_prompt / monthly_prompt)
        # Total monthly spend = prompt cost + generated token cost.
        # GENERATED_COST_PER_1K was defined above but previously unused —
        # generated tokens at ~2× the prompt rate are the other half of
        # what an over-prompting account spends.
        monthly_spend    = (
            (monthly_prompt / 1000) * PROMPT_COST_PER_1K
            + (monthly_generated / 1000) * GENERATED_COST_PER_1K
        )
        monthly_savings  = monthly_spend * wasted_pct * 0.50

        severity = "high" if wasted_pct >= 0.40 else "medium" if wasted_pct >= 0.20 else "low"

        evidence = (
            f"Over {months_active} months, this {use_case} account averaged "
            f"{monthly_prompt:,.0f} prompt tokens/month vs "
            f"{monthly_generated:,.0f} generated tokens/month "
            f"(ratio: {ratio:.2f}, healthy minimum: {threshold:.2f}). "
            f"Estimated {wasted_pct:.0%} of prompt spend is above the efficient baseline."
        )
        action = (
            f"Review system prompt and context injection in '{resource_name}'. "
            f"Target a {wasted_pct * 0.50:.0%} reduction via prompt compression, "
            f"context trimming, or Azure OpenAI prompt caching."
        )

        upsert_finding(
            client=client, run_obj=run_obj,
            resource_id=rid, resource_name=resource_name,
            resource_group=resource_group,
            finding_type="token_inefficiency", analyzer="cognitive_services",
            severity=severity, monthly_savings=monthly_savings,
            evidence=evidence, action=action,
            extra_data={
                "inferred_use_case":         use_case,
                "avg_efficiency_ratio":      round(ratio, 3),
                "healthy_threshold":         threshold,
                "monthly_prompt_tokens":     round(monthly_prompt),
                "monthly_generated_tokens":  round(monthly_generated),
                "monthly_prompt_spend_usd":  round(monthly_spend, 2),
                "estimated_wasted_pct":      round(wasted_pct, 3),
            },
        )
        created += 1

    return created
