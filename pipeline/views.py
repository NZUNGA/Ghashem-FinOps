import threading
from datetime import timedelta
from django.http import JsonResponse, Http404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.http import require_POST
from core.models import PipelineRun, UserClientAccess
from core.access import require_client_admin
from core.config import load_config
from core.auth import load_client_context
from pipeline.runner import run_pipeline
from pipeline.live_check import run_live_check

STALE_RUN_TIMEOUT = timedelta(minutes=10)


def _run_in_thread(client_slug: str, run_id: int):
    """Runs the pipeline in a background thread so the HTTP response returns immediately."""
    run_pipeline(client_slug=client_slug, triggered_by="web_ui")


@login_required
@require_POST
@require_client_admin
def trigger_pipeline(request, slug):
    # require_client_admin attaches request.client and has already
    # verified this user has 'admin' role (or is superuser) for this
    # client — triggering a pipeline run is a state-changing action
    # and viewer-role users must not be able to do it.
    client_obj = request.client

    already = PipelineRun.objects.filter(
        client=client_obj, status="running"
    ).order_by("-id").first()
    if already:
        return JsonResponse(
            {"status": "already_running", "run_id": already.pk}, status=409
        )

    # Created here, synchronously, in the request thread — NOT inside
    # run_pipeline() on the background thread. Fixes a race where this
    # view previously queried PipelineRun.objects.order_by("-started_at")
    # for "the row the background thread just created" immediately after
    # calling Thread.start(), which gives no guarantee the thread has
    # run yet — that query could return None (a client's first-ever run)
    # or a stale previous run's id (any later run), so the JSON response
    # could report run_id: null even though the pipeline started fine,
    # which is exactly what made the frontend's progress bar show an
    # error while the pipeline succeeded in the background.
    run_obj = PipelineRun.objects.create(
        client=client_obj,
        status="running",
        triggered_by=f"user:{request.user}",
    )

    t = threading.Thread(
        target=run_pipeline,
        kwargs={
            "client_slug": slug,
            "triggered_by": f"user:{request.user}",
            "existing_run_id": run_obj.pk,
        },
        daemon=True,
    )
    t.start()

    # Prevents serving a stale "running"/pre-run cached summary if the
    # cache happened to be (re)populated right before this trigger fired.
    try:
        from django.core.cache import cache
        cache.delete(f"summary_{client_obj.id}")
        cache.delete(f"ai_governance_{client_obj.id}")
    except Exception:
        pass  # cache bust failure must never break triggering the pipeline

    return JsonResponse({
        "status": "started",
        "run_id": run_obj.pk,
    })


@login_required
@require_POST
@require_client_admin
def trigger_live_check(request, slug):
    # require_client_admin attaches request.client — live check is
    # state-changing (marks findings resolved, updates ResourceStatus),
    # so this carries the same admin-role requirement as trigger_pipeline.
    # Unlike the full pipeline, this only re-checks resources we already
    # know about — it runs in seconds, so it's done synchronously with
    # no background thread or PipelineRun polling.
    client_slug = slug
    client_obj = request.client

    config = load_config()
    client_cfg = config.get_client(client_slug)
    subscription_ids = [s.id for s in client_cfg.subscriptions]

    ctx = load_client_context(
        client_slug=client_slug,
        app_client_id=config.app_client_id,
        vault_url=config.vault_url,
        subscription_id=subscription_ids[0],
    )

    result = run_live_check(client_obj, ctx, subscription_ids)

    if result.get("error"):
        return JsonResponse({"status": "error", "error": result["error"]}, status=502)

    # run_live_check() just updated ResourceStatus/Finding directly in the
    # DB — without this, the cached summary()/ai_governance_page() context
    # (see dashboard/views.py CACHES) would keep serving the stale,
    # pre-check state for up to its 5-minute TTL. trigger_pipeline and
    # runner.py already bust this cache on their own completion paths;
    # this was the one state-changing entry point that didn't.
    try:
        from django.core.cache import cache
        cache.delete(f"summary_{client_obj.id}")
        cache.delete(f"ai_governance_{client_obj.id}")
    except Exception:
        pass  # cache bust failure must never break live check

    return JsonResponse({"status": "completed", **result})


@login_required
def run_status(request, run_id: int):
    try:
        run = PipelineRun.objects.select_related("client").get(pk=run_id)
    except PipelineRun.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    # A pipeline run's log can contain resource names and cost figures —
    # treat it with the same access control as the client's own data.
    has_access = (
        request.user.is_superuser
        or UserClientAccess.objects.filter(
            user=request.user, client=run.client
        ).exists()
    )
    if not has_access:
        raise Http404()

    # Server-side stale-run detection: if a run has been "running" for
    # longer than the frontend's own safety timeout would allow, mark it
    # failed here so the next poll returns "failed" immediately instead
    # of leaving the button spinning until the client-side timeout fires.
    if run.status == "running" and run.started_at:
        elapsed = timezone.now() - run.started_at
        if elapsed > STALE_RUN_TIMEOUT:
            PipelineRun.objects.filter(id=run.id).update(
                status="failed",
                current_step="",
                error="Pipeline timed out — no completion signal received "
                      "after 10 minutes",
            )
            run.status = "failed"
            run.current_step = ""

    return JsonResponse({
        "status":       run.status,
        "current_step": run.current_step or "",
        "log":          run.log,
        "error":        run.error,
        "started_at":   run.started_at.isoformat() if run.started_at else None,
        "finished_at":  str(run.finished_at) if run.finished_at else None,
    })
