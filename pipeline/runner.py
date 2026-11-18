import logging
from datetime import date, timedelta
from django.conf import settings
from django.utils import timezone as dj_tz
from core.models import Client, PipelineRun
from core.auth import load_client_context
from core.config import load_config, get_client_config_from_db
from pipeline import (
    collector_cost, collector_metrics, collector_zombie,
    analyzer_token, analyzer_idle, analyzer_resource_utilization, analyzer_zombie,
    analyzer_anomaly,
)
from pipeline.live_check import run_live_check

log = logging.getLogger(__name__)


def _update_step(run_obj, step_label):
    """Update current_step on PipelineRun for real-time progress display."""
    if run_obj is None:
        return
    try:
        PipelineRun.objects.filter(id=run_obj.id).update(current_step=step_label)
    except Exception:
        pass  # never let step tracking break the pipeline


def _bust_dashboard_cache(client_obj):
    """
    Invalidate the cached summary()/ai_governance_page() views for this
    client — called on both success and failure so a stale cached page
    (wrong last_run status, stale findings) never outlives a run whose
    status just changed. Cache bust failure must never break the pipeline.
    """
    if client_obj is None:
        return
    try:
        from django.core.cache import cache
        cache.delete(f"summary_{client_obj.id}")
        cache.delete(f"ai_governance_{client_obj.id}")
    except Exception:
        pass


def run_pipeline(client_slug: str, triggered_by: str = "manual",
                  existing_run_id: int = None) -> PipelineRun:
    """
    Full pipeline: collect costs → collect metrics → run analyzers → live check.

    The live check runs automatically at the end of every successful pipeline
    run so ResourceStatus is always current after an analysis — zombie resources
    that were just found get protected, and genuinely deleted metered resources
    get resolved, without needing a separate manual live_check command.
    """
    # This setup block (config loading + run_obj creation) runs BEFORE
    # the main try/except further down that guards the pipeline body —
    # without its own safety net, a crash here (bad client config, DB
    # unavailable, etc.) would escape uncaught on the background thread
    # trigger_pipeline started, leaving the PipelineRun row it already
    # created (status="running") stuck forever — the frontend's "Run
    # analysis" button would then spin until the JS safety timeout
    # instead of recovering immediately.
    try:
        # DB-first: get_client_config_from_db() requires a Client row with at
        # least one Subscription row. Falls back to YAML for legacy clients
        # (e.g. new-demo-client) that may not have one yet — this fallback
        # stays in place indefinitely, not just "during transition"; removing
        # it would break any client that's in clients.yaml but was never
        # given a Subscription row. app_client_id/vault_url aren't part of
        # ClientConfig (see core/config.py) — they're the single global app
        # registration/vault, sourced from settings in both paths so the
        # YAML file's top-level app_client_id/vault_url keys are only used
        # when the YAML path itself is actually taken.
        try:
            client_cfg = get_client_config_from_db(client_slug)
            app_client_id = getattr(settings, "AZURE_APP_CLIENT_ID", "")
            vault_url = getattr(settings, "VAULT_URL", "")
            log.info(f"[{client_slug}] Using DB config")
        except ValueError:
            config = load_config()
            client_cfg = config.get_client(client_slug)
            app_client_id = config.app_client_id
            vault_url = config.vault_url
            log.info(f"[{client_slug}] Using YAML config (DB config unavailable)")

        client_obj, _ = Client.objects.update_or_create(
            slug=client_slug,
            defaults={
                "display_name":    client_cfg.display_name,
                "active":          client_cfg.active,
                "analysis_start":  client_cfg.analysis_start,
                "analysis_end":    client_cfg.analysis_end,
                "report_currency": client_cfg.report_currency,
            },
        )

        # Roll the analysis window forward to the most recent 90 days.
        # analysis_start/analysis_end are stored on Client (onboard_new and
        # import_clients_yaml write concrete dates once) and the DB config
        # loader only falls back to a rolling default when they're null, so
        # without this the window stayed frozen at onboarding and cost
        # collection never advanced past that date. Applied in memory HERE
        # so this run actually collects through today (persisting only at
        # completion would leave collection permanently one run behind);
        # persisted to the Client row only on success, further down.
        window_end = date.today()
        window_start = window_end - timedelta(days=90)
        client_cfg.analysis_start = window_start
        client_cfg.analysis_end = window_end
        client_obj.analysis_start = window_start
        client_obj.analysis_end = window_end

        # If trigger_pipeline (pipeline/views.py) already created the
        # PipelineRun row synchronously before starting this background
        # thread, reuse it instead of creating a second one — that's the
        # fix for the run_id race where the view used to query for "the
        # row this thread just created" with no guarantee the thread had
        # created it yet. started_at is still set here (not there), since
        # this is the point the pipeline body actually starts running.
        # Falls back to creating a new row for any caller that doesn't pass
        # existing_run_id (the run_pipeline management command, tests, etc.)
        # — started_at is set explicitly in both fallback branches too,
        # since PipelineRun.started_at has no auto_now_add and every other
        # view sorts by -started_at to find a client's most recent run.
        if existing_run_id is not None:
            try:
                run_obj = PipelineRun.objects.get(pk=existing_run_id)
                run_obj.started_at = dj_tz.now()
                run_obj.save(update_fields=["started_at"])
            except PipelineRun.DoesNotExist:
                run_obj = PipelineRun.objects.create(
                    client=client_obj,
                    status="running",
                    started_at=dj_tz.now(),
                    triggered_by=triggered_by,
                )
        else:
            run_obj = PipelineRun.objects.create(
                client=client_obj,
                status="running",
                started_at=dj_tz.now(),
                triggered_by=triggered_by,
            )
    except Exception as setup_exc:
        log.error(f"[{client_slug}] Pipeline setup crashed: {setup_exc}", exc_info=True)
        if existing_run_id is not None:
            PipelineRun.objects.filter(id=existing_run_id).update(
                status="failed",
                current_step="",
                error=str(setup_exc)[:2000],
                finished_at=dj_tz.now(),
            )
            _bust_dashboard_cache(Client.objects.filter(slug=client_slug).first())
            return PipelineRun.objects.get(pk=existing_run_id)
        # No pre-created run row and no usable client config — nothing to
        # mark as failed. The management-command path (no existing_run_id)
        # only reaches here if a client's config is broken in both the DB
        # and YAML; the web UI's trigger_pipeline can never hit this branch
        # since it requires the Client to already exist to resolve the
        # URL slug in the first place.
        raise

    log_lines = []

    def logline(msg: str):
        log.info(msg)
        log_lines.append(msg)
        run_obj.log = "\n".join(log_lines)
        run_obj.save(update_fields=["log"])

    try:
        logline(f"Pipeline started for {client_cfg.display_name}")

        # ── 1. Collect costs via the live Cost Management query API ─────────
        _update_step(run_obj, "Collecting billing data...")
        for sub in client_cfg.subscriptions:
            logline(f"Collecting costs: {sub.display_name} ({sub.id})")
            ctx = load_client_context(
                client_slug=client_slug,
                app_client_id=app_client_id,
                vault_url=vault_url,
                subscription_id=sub.id,
            )
            rows = collector_cost.collect_costs(
                client_obj=client_obj,
                ctx=ctx,
                subscription_id=sub.id,
                start_date=client_cfg.analysis_start,
                end_date=client_cfg.analysis_end,
            )
            logline(f"  → {rows} cost rows saved")

        # ── 2. Collect metrics (inventory first) ────────────────────────────
        all_subscription_ids = [s.id for s in client_cfg.subscriptions]
        ctx = load_client_context(
            client_slug=client_slug,
            app_client_id=app_client_id,
            vault_url=vault_url,
            subscription_id=client_cfg.subscriptions[0].id,
        )
        _update_step(run_obj, "Analysing resource utilization...")
        logline("Collecting resource inventory...")
        resources = collector_metrics.collect_inventory(ctx, all_subscription_ids)
        logline(f"  → {len(resources)} resources found")

        logline("Collecting metrics (this may take several minutes)...")
        metric_rows = collector_metrics.collect_metrics(
            client_obj=client_obj,
            ctx=ctx,
            resources=resources,
            start_date=client_cfg.analysis_start,
            end_date=client_cfg.analysis_end,
        )
        logline(f"  → {metric_rows} metric rows saved")

        # ── 2b. Collect zombie/phantom resources (Resource Graph only) ──────
        _update_step(run_obj, "Scanning for waste...")
        logline("Scanning for zombie resources (unattached IPs, orphaned disks, "
                "stopped-not-deallocated VMs, etc.)...")
        zombie_data = collector_zombie.collect_all_zombie_resources(ctx, all_subscription_ids)
        zombie_total_found = sum(len(v) for v in zombie_data.values())
        logline(f"  → {zombie_total_found} zombie resources found across 8 categories")

        # ── 3. Clear old OPEN findings for this client ───────────────────────
        from core.models import Finding
        old_count = Finding.objects.filter(
            client=client_obj, status__in=["open", "stale"]
        ).delete()[0]
        logline(f"Cleared {old_count} previous open findings "
                f"(resolved findings preserved)")

        # ── 4. Run analyzers ────────────────────────────────────────────────
        # Each core analyzer is isolated in its own try/except — same
        # reasoning as the reservations/AI blocks further down (an Advisor
        # outage must never fail the whole run): a bug or data edge case in
        # one analyzer (e.g. a single malformed resource) must not also
        # wipe out the findings every OTHER analyzer would have produced.
        # analyzer_failures drives the completed/completed_with_errors/failed
        # decision at the end of this function.
        analyzer_failures = []

        logline("Running token efficiency analyzer...")
        try:
            n = analyzer_token.run(client_obj, run_obj)
            logline(f"  → {n} token inefficiency findings")
        except Exception as e:
            logline(f"  ✗ Token efficiency analyzer error: {e}")
            log.exception(f"[{client_slug}] Token efficiency analyzer failed")
            analyzer_failures.append("token_efficiency")

        logline("Running idle account analyzer...")
        try:
            n = analyzer_idle.run(client_obj, run_obj)
            logline(f"  → {n} idle account findings")
        except Exception as e:
            logline(f"  ✗ Idle account analyzer error: {e}")
            log.exception(f"[{client_slug}] Idle account analyzer failed")
            analyzer_failures.append("idle_account")

        logline("Running resource utilization analyzer (VMs, Storage, Web Apps)...")
        try:
            n = analyzer_resource_utilization.run(client_obj, run_obj)
            logline(f"  → {n} resource utilization findings")
        except Exception as e:
            logline(f"  ✗ Resource utilization analyzer error: {e}")
            log.exception(f"[{client_slug}] Resource utilization analyzer failed")
            analyzer_failures.append("resource_utilization")

        logline("Running zombie resource analyzer...")
        try:
            n = analyzer_zombie.run(client_obj, run_obj, zombie_data)
            logline(f"  → {n} zombie resource findings")
        except Exception as e:
            logline(f"  ✗ Zombie resource analyzer error: {e}")
            log.exception(f"[{client_slug}] Zombie resource analyzer failed")
            analyzer_failures.append("zombie_resources")

        _update_step(run_obj, "Running anomaly detection...")
        logline("Running cost anomaly analyzer...")
        try:
            anomaly_count = analyzer_anomaly.run(client_obj, run_obj)
            logline(f"  → {anomaly_count} cost anomaly findings")
        except Exception as e:
            logline(f"  ✗ Cost anomaly analyzer error: {e}")
            log.exception(f"[{client_slug}] Cost anomaly analyzer failed")
            analyzer_failures.append("cost_anomaly")
            anomaly_count = 0

        if anomaly_count > 0:
            from core.email import send_anomaly_alert
            for anomaly_finding in Finding.objects.filter(
                client=client_obj, finding_type="cost_anomaly", status="open",
            ):
                try:
                    sent = send_anomaly_alert(client_obj, anomaly_finding)
                    logline(f"  → Cost anomaly alert email {'sent' if sent else 'skipped'}")
                except Exception as email_exc:
                    # A broken mail relay must never fail a pipeline run that
                    # otherwise completed successfully — the finding itself
                    # is still saved and visible on the dashboard either way.
                    logline(f"  → Cost anomaly alert email failed: {email_exc}")
                    log.warning(f"Cost anomaly alert email failed: {email_exc}")

        _update_step(run_obj, "Checking reservation opportunities...")
        logline("Running reservation finder (Azure Advisor)...")
        try:
            from pipeline import analyzer_reservations
            reservation_count = analyzer_reservations.run(client_obj, run_obj)
            logline(f"  → Reservation findings: {reservation_count}")
        except Exception as e:
            # Advisor being unavailable, unconfigured, or missing RBAC must
            # never fail a pipeline run that otherwise completed successfully.
            logline(f"  → Reservation finder error: {e}")
            log.warning(f"[{client_slug}] Reservation finder error: {e}")

        # AI Cost Intelligence — Azure OpenAI token usage + waste findings.
        # Both wrapped in their own try/except so an AI collection/
        # analysis error can never fail the main pipeline.
        _update_step(run_obj, "Checking AI resources...")
        logline("Collecting AI token usage (Azure OpenAI)...")
        ai_resources = []
        try:
            from pipeline import collector_ai
            ai_rows, ai_resources = collector_ai.run(client_obj, run_obj, ctx)
            logline(f"  → AI usage rows saved: {ai_rows}")
        except Exception as e:
            logline(f"  ✗ AI collector error: {e}")
            log.warning(f"[{client_slug}] AI collector error: {e}")

        try:
            from pipeline import analyzer_ai_waste
            ai_findings = analyzer_ai_waste.run(client_obj, run_obj)
            logline(f"  → AI waste findings: {ai_findings}")
        except Exception as e:
            logline(f"  ✗ AI analyzer error: {e}")
            log.warning(f"[{client_slug}] AI analyzer error: {e}")

        # AI Governance posture + Shadow AI discovery — Session 5b.
        # Reuses ai_resources from the collector_ai.run() call above
        # (already-fetched Resource Graph data, including the
        # governance fields it now projects) instead of a second
        # discovery call. Service principal audit is explicitly
        # deferred — needs azure-mgmt-authorization, not installed yet
        # (see CLAUDE.md Known Gaps).
        _update_step(run_obj, "Scoring AI governance posture...")
        try:
            from pipeline import analyzer_ai_posture
            posture_result = analyzer_ai_posture.run(
                client_obj, run_obj, ai_resources=ai_resources
            )
            logline(f"  → AI posture findings: {posture_result}")
        except Exception as e:
            logline(f"  ✗ AI posture error: {e}")
            log.warning(f"[{client_slug}] AI posture error: {e}")

        try:
            from pipeline import collector_shadow_ai
            shadow_result = collector_shadow_ai.run(client_obj, run_obj, ctx)
            logline(f"  → Shadow AI findings: {shadow_result}")
        except Exception as e:
            logline(f"  ✗ Shadow AI scan error: {e}")
            log.warning(f"[{client_slug}] Shadow AI scan error: {e}")

        # ── 5. Live check — runs automatically after every successful run ───
        # Updates ResourceStatus so the dashboard immediately reflects what
        # was just found — zombie resources get protected, genuinely deleted
        # metered resources get resolved. No separate command needed.
        logline("Running live check...")
        try:
            lc_result = run_live_check(client_obj, ctx, all_subscription_ids)
            if lc_result.get("error"):
                logline(f"  → Live check warning: {lc_result['error']}")
            else:
                logline(
                    f"  → {lc_result['checked']} resources checked — "
                    f"{lc_result['deleted']} deleted, "
                    f"{lc_result['still_exists']} exist, "
                    f"{lc_result.get('skipped', 0)} protected (zombie findings)"
                )
        except Exception as lc_exc:
            # Live check failure must never fail the pipeline itself
            logline(f"  → Live check skipped: {lc_exc}")
            log.warning(f"Live check failed after pipeline: {lc_exc}")

        # Mark complete. Collector failures (cost/metrics/zombie — the
        # prerequisites every analyzer depends on) never reach this point at
        # all: they raise straight to the outer except below, which still
        # marks the whole run "failed". From here on, cost/metric
        # collection is known-good, so status instead reflects how many of
        # the 5 core analyzers above raised:
        #   0 failures        → completed
        #   some, not all     → completed_with_errors (partial findings —
        #                       still useful, but worth a closer look)
        #   all 5 failed      → failed (no analyzer actually ran clean)
        CORE_ANALYZER_COUNT = 5
        if len(analyzer_failures) >= CORE_ANALYZER_COUNT:
            run_obj.status = "failed"
            run_obj.error = "All core analyzers failed: " + ", ".join(analyzer_failures)
            logline(f"Pipeline FAILED — every core analyzer raised: {', '.join(analyzer_failures)}")
        elif analyzer_failures:
            run_obj.status = "completed_with_errors"
            logline(
                f"Pipeline completed with errors — failed analyzers: "
                f"{', '.join(analyzer_failures)}"
            )
        else:
            run_obj.status = "completed"
            logline("Pipeline completed successfully.")
        run_obj.finished_at = dj_tz.now()
        # Persisted immediately (not just the in-memory object) because the
        # first-run welcome-email check right below counts completed
        # PipelineRun rows in the DB — without this, the current run
        # wouldn't be counted yet (only the final run_obj.save() at the
        # very end of this function persists status), so "previous_runs
        # == 1" would only ever become true on the client's SECOND
        # completed run instead of their first.
        run_obj.save(update_fields=["status", "finished_at", "error"])

        # Persist the rolled window — success only, so a failed run leaves
        # the previous window (and its data) untouched — and drop cost rows
        # that have fallen out of it. Several readers sum ALL of a client's
        # DailyCost rows with no date filter (zombie/idle/utilization
        # analyzers × 30/window_days, core/brief.py, the Spend page), which
        # was only correct while the window never moved; pruning keeps the
        # table equal to the window so those stay right. Cost history
        # beyond 90 days is re-fetchable from Cost Management.
        try:
            Client.objects.filter(id=client_obj.id).update(
                analysis_start=window_start, analysis_end=window_end,
            )
            from core.models import DailyCost
            pruned = DailyCost.objects.filter(
                client=client_obj, date__lt=window_start
            ).delete()[0]
            logline(f"  → Analysis window rolled to {window_start} → {window_end}"
                    f" ({pruned} cost rows older than the window pruned)")
        except Exception as roll_exc:
            logline(f"  → Could not roll analysis window: {roll_exc}")
            log.warning(f"[{client_slug}] Window roll failed: {roll_exc}")

        _bust_dashboard_cache(client_obj)

        # Resolve and store the service principal's object ID in this
        # client's tenant, if not already known — see
        # core.auth.decode_jwt_oid() for why this needs no new
        # permission. Only needs doing once; cheap either way since it
        # reuses the credential already obtained for live check above.
        if not client_obj.client_sp_object_id:
            try:
                from core.auth import decode_jwt_oid
                token = ctx.credential.get_token(
                    "https://management.azure.com/.default"
                ).token
                oid = decode_jwt_oid(token)
                if oid:
                    client_obj.client_sp_object_id = oid
                    client_obj.save(update_fields=["client_sp_object_id"])
                    logline(f"  → Resolved service principal object ID: {oid}")
            except Exception as sp_exc:
                logline(f"  → Could not resolve service principal object ID: {sp_exc}")
                log.warning(f"[{client_slug}] SP object ID resolution failed: {sp_exc}")

        previous_runs = PipelineRun.objects.filter(
            client=client_obj, status__in=["completed", "completed_with_errors"]
        ).count()
        if previous_runs == 1:
            logline("First completed run for this client — sending welcome email...")
            try:
                from core.brief import compute_brief_stats
                from core.email import send_welcome_email
                run_summary = compute_brief_stats(client_obj)
                sent = send_welcome_email(client_obj, run_summary)
                logline(f"  → Welcome email {'sent' if sent else 'skipped'}")
            except Exception as welcome_exc:
                logline(f"  → Welcome email failed: {welcome_exc}")
                log.error(f"Welcome email failed: {welcome_exc}")

        if client_obj.onboarding_status == "pending_pipeline":
            client_obj.onboarding_status = "active"
            client_obj.save(update_fields=["onboarding_status"])

        # ── 6. Monthly executive brief — generated and emailed after every
        # successful run. Wrapped locally so a PDF/email failure can never
        # flip an otherwise-successful run to failed, same reasoning as
        # the live check above.
        _update_step(run_obj, "Generating monthly brief...")
        logline("Generating monthly brief...")
        try:
            from core.brief import generate_monthly_brief
            from core.email import send_monthly_brief
            pdf = generate_monthly_brief(client_obj, run_obj)
            sent = send_monthly_brief(client_obj, pdf, run_obj)
            logline(
                f"  → Monthly brief {'emailed' if sent else 'generated (email skipped)'} "
                f"({len(pdf)} bytes)"
            )
        except Exception as brief_exc:
            logline(f"  → Monthly brief failed: {brief_exc}")
            log.error(f"Monthly brief generation/send failed: {brief_exc}")

    except Exception as exc:
        run_obj.status = "failed"
        run_obj.error  = str(exc)
        run_obj.finished_at = dj_tz.now()
        logline(f"Pipeline FAILED: {exc}")
        log.exception("Pipeline exception")
        _bust_dashboard_cache(client_obj)

    _update_step(run_obj, "")
    run_obj.save()
    return run_obj
