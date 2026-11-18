# Ghashem — Azure FinOps Intelligence

A multi-tenant Django application that connects to a client's Azure tenant, collects cost and
usage data across Cost Management, Monitor, and Resource Graph, and turns it into plain-English
cost-optimization findings with estimated dollar savings. Built for mid-size organizations that
have real Azure spend but no in-house FinOps or DevOps function to chase it down themselves.

## 1. What it does

Most Azure cost tooling is built for the person who already knows what a Reserved Instance is.
Ghashem is built for the person who manages the relationship with that vendor but doesn't read
Terraform diffs or Cost Management exports for fun. You give it Reader-level read access to an
Azure subscription; it runs a scheduled (or on-demand) pipeline that scans the tenant and comes
back with a list of specific, dollar-denominated findings — "this VM has been stopped but not
deallocated for 40 days, you're paying ~$140/month for nothing," not a dashboard full of raw
usage graphs.

The output surfaces in four places: an **Overview** page with a resource-grouped findings table,
estimated monthly/annual savings, and spend-by-category breakdown; a **Findings** page with
full filtering and a resolve workflow; a **monthly executive brief** (a one-page PDF, generated
and emailed automatically after every pipeline run); and a dedicated **AI Governance** page that
scores Azure OpenAI / Cognitive Services resources on security posture (local auth, public
network exposure, private endpoints) and flags ungoverned "shadow AI" usage separately from cost
findings, since those are a security conversation, not a savings one.

Honest state of the project: this is a single, shared Django deployment serving every onboarded
client (tenant isolation is enforced at the application layer via an explicit per-user,
per-client access grant table — see `core/access.py` — not by spinning up a separate instance per
customer). There is no self-serve signup; onboarding a new client (`/onboard/new/`) is a
staff-only flow that provisions Key Vault secrets and walks through Entra admin consent by hand.
Billing is percentage-of-spend, negotiated and applied manually by staff — there is no Stripe
integration or self-serve upgrade path.

## 2. Architecture overview

- **Django 4.2 monolith** running on Azure App Service (Linux, Python 3.12, B1 plan). Four Django
  apps: `core` (auth, config, models, email, billing), `pipeline` (collectors, analyzers,
  orchestration), `dashboard` (every client-facing and staff-facing view), `contracts` (a
  separate, independent SaaS-contract-renewal tracker, unrelated to the Azure pipeline).
- **PostgreSQL Flexible Server** (B1ms) as the primary datastore. Falls back to local SQLite when
  no `DB_HOST` is configured, which is how local development works with zero external dependencies
  beyond Azure credentials for the pipeline itself.
- **Zero-credential-at-rest model.** Nothing in this repo — not `.env`, not the database, not
  settings.py — ever contains a tenant ID, subscription ID, or client secret for a real customer.
  The app authenticates to Azure via `DefaultAzureCredential` (`az login` locally, Managed Identity
  in Azure — `core/auth.py`), and every per-client secret (`client-{slug}-tenant-id`,
  `-subscription-id`, `-secret`, `-access-group-id`) lives exclusively in Azure Key Vault, looked
  up by slug at runtime. A hardcoded tenant/client ID in source would silently misdirect auth for
  every *other* client with no error raised — this is treated as a hard rule, not a style
  preference.
- **Two-tenant Entra ID model.** One multi-tenant Entra app registration lives in Ghashem's own
  ("company") tenant and is the *only* app registration in existence — client tenants never create
  their own; they grant it admin consent, which creates a service principal (Enterprise
  Application) inside their tenant. Staff and customer logins both go through this same app
  registration via `social-auth-app-django`, with `SOCIAL_AUTH_MICROSOFT_TENANT = "common"`
  accepting any Microsoft tenant at the OAuth layer — the actual access boundary is enforced
  afterward, in `core/sso_pipeline.py`, which matches the logging-in user's tenant ID against a
  known client and checks their Entra group membership before granting any `UserClientAccess` row.
  An unmatched tenant or unauthorized user still gets a Django account, just no data access.
- **A multi-phase analysis pipeline** — collectors pull raw Azure data, analyzers turn it into
  findings. See [Section 4](#4-the-analysis-pipeline) for the full walkthrough.
- **Import-direction rule, enforced by convention, not tooling:** `core/` must never import from
  `dashboard/`. `dashboard/` imports from `core/`, never the reverse. This is why
  `core/savings.py` *duplicates* a small amount of logic that also lives in `dashboard/views.py`
  rather than importing it directly — see [Section 6](#6-savings-calculation).

## 3. Repo structure

```
ghashem/
├── core/                         # Cross-cutting: auth, config, models, billing, email — no views
│   ├── models.py                 # Client, Finding, DailyCost, DailyAIUsage, AIResourcePosture, etc.
│   ├── auth.py                   # load_client_context() — per-client Azure credential from Key Vault
│   ├── access.py                 # @require_client_access / @require_client_admin / @require_tier
│   ├── config.py                 # ClientConfig loading — DB-first, clients.yaml fallback
│   ├── sso_pipeline.py           # Matches an SSO login's tenant ID to a Client, checks group membership
│   ├── savings.py                # canonical_monthly_savings() — the one savings number every page shares
│   ├── billing.py                 # Percentage-of-spend tier fee calculation
│   ├── brief.py                   # Monthly PDF executive brief (reportlab)
│   └── email.py                   # Anomaly alerts, welcome email, monthly brief email, renewal alerts
│
├── pipeline/                     # Everything that talks to Azure or turns data into findings
│   ├── runner.py                  # run_pipeline() — the orchestration sequence, see Section 4
│   ├── collector_cost.py          # Azure Cost Management → DailyCost
│   ├── collector_metrics.py       # Azure Monitor → DailyMetric (9 resource namespaces)
│   ├── collector_zombie.py        # Resource Graph KQL → phantom/zombie resource candidates
│   ├── collector_ai.py            # Cognitive Services discovery + token usage → DailyAIUsage
│   ├── collector_shadow_ai.py     # AI-adjacent resource discovery → shadow_ai_resource findings
│   ├── analyzer_*.py              # zombie, idle, resource_utilization, token, anomaly,
│   │                               # reservations, ai_waste, ai_posture — DB-only, write Finding rows
│   ├── finding_utils.py           # upsert_finding() — the single shared Finding write path
│   ├── live_check.py              # Re-verifies existing findings' resources still exist
│   └── management/commands/       # run_pipeline, live_check, grant_access, import_clients_yaml
│
├── dashboard/                     # Every user-facing view
│   ├── views.py                   # Overview, Findings, AI Governance, Spend, onboarding, admin
│   ├── urls.py
│   └── templates/dashboard/        # Server-rendered Django templates, Chart.js for charts
│
├── contracts/                     # Independent SaaS-contract renewal tracker (not Azure-cost-related)
│
├── ghashem/                        # Django project package: settings.py, urls.py, wsgi.py
├── config/
│   ├── clients.yaml.example        # Placeholder — legacy YAML-based client config path
│   └── clients.yaml                 # Gitignored — real client config, if the YAML path is used at all
│
├── 00-setup-sso-secret.ps1/.sh      # One-time: store the SSO app credential in the company Key Vault
├── 01-provision.ps1/.sh             # Provisions the full Azure stack (see Section 8)
├── 02-deploy-code.ps1/.sh           # Zips and deploys application code
├── 03-post-deploy.sh                # Migrations, superuser, first pipeline run (via SSH console)
├── 04-smoke-test.ps1/.sh            # Post-deploy checklist + automated smoke checks
│
├── requirements.txt
├── .env.example                     # Template for required environment variables
└── CLAUDE.md                        # Engineering runbook / incident history (gitignored — see below)
```

## 4. The analysis pipeline

`pipeline/runner.py::run_pipeline()` is the single entry point, called either from the web UI
(`pipeline/views.py::trigger_pipeline`, run on a background thread) or the `run_pipeline`
management command. It runs in a fixed sequence, rolling the client's analysis window to the most
recent 90 days in memory before anything else happens:

1. **Cost collection** (`collector_cost.py`), once per subscription.
2. **Resource inventory + metrics** (`collector_metrics.py`) — inventory first, then metrics for
   every discovered resource.
3. **Zombie/phantom resource scan** (`collector_zombie.py`) — pure Resource Graph, no Monitor calls.
4. Every existing `open`/`stale` `Finding` for the client is deleted (resolved findings are kept),
   and the five core analyzers run in sequence: `analyzer_token`, `analyzer_idle`,
   `analyzer_resource_utilization`, `analyzer_zombie`, `analyzer_anomaly`. Each is individually
   wrapped in its own try/except — a bug in one analyzer no longer wipes out the findings every
   other analyzer would have produced; the run is marked `completed`, `completed_with_errors`, or
   `failed` depending on how many of the five raised.
5. **Reservation finder** (`analyzer_reservations.py`), then the **AI Cost Intelligence** path
   (`collector_ai.py` → `analyzer_ai_waste.py`) and **AI Governance** path (`analyzer_ai_posture.py`
   → `collector_shadow_ai.py`) — all four wrapped so an Advisor outage or AI-specific failure can
   never fail the overall run.
6. **Live check** (`pipeline/live_check.py`) — re-verifies every tracked resource still exists in
   Azure and resolves findings for ones that don't.
7. Window roll-forward, service-principal object ID resolution, the welcome email (first
   completed run only), and the monthly PDF brief — all best-effort, all non-fatal to the run.

### Collectors (Azure-only, write raw data)

**`collector_cost.py` — Azure Cost Management.** Calls the Cost Management Query API
(`CostManagementClient.query.usage`) once per subscription, grouped by `ServiceName`,
`ResourceGroupName`, `ResourceId`, with daily granularity. It first tries the FOCUS-aligned
grouping dimensions (`ServiceCategory`, `ChargeCategory`, `ProviderName`, `PricingCategory`) and
falls back to the legacy three-dimension query on a 400 response, since not every
subscription/API version accepts the FOCUS dimensions yet. Tax/Credit/Adjustment rows are
filtered out — but only when the FOCUS query actually succeeded and returned a `ChargeCategory`
column to filter on. 429 (throttling) responses get exponential backoff (10s, 20s, 40s... up to 5
attempts), honoring Azure's `Retry-After` header when present. Writes one `DailyCost` row per
(date, resource) via `update_or_create`.

**`collector_metrics.py` — Azure Monitor.** Discovers resources across nine namespaces (VMs,
Storage, Web Apps, Cognitive Services, SQL, Redis, PostgreSQL Flexible Server, AKS) via a single
Resource Graph inventory query, then pulls the relevant metrics per namespace from
`MonitorManagementClient.metrics.list` in 29-day chunks (Monitor's query window has practical
limits well under its 93-day hard retention cap). Web App metrics are fetched one metric at a time
rather than batched, since `CpuTime` needs an hourly interval while the others need daily. Writes
are batched via `bulk_create(update_conflicts=True, ...)` rather than per-row
`update_or_create` — this eliminates the SQLite lock contention that surfaced when the pipeline
ran on a background thread alongside normal request handling, and requires de-duplicating the
pending batch by `(client, resource_id, metric_name, date)` first, since PostgreSQL errors on a
conflict target hit twice in the same `bulk_create` call.

**`collector_zombie.py` — Resource Graph only, no Monitor calls at all.** See
[Section 5](#5-kql-queries) for the actual queries. The design reasoning for skipping Monitor
entirely: every signal needed to decide "is this a zombie" — attachment state, power state,
backend pool membership — is already present in a resource's ARM properties, so there's nothing
Monitor metrics would add, and Resource Graph has much higher rate limits than Cost Management,
making it the cheaper, faster choice for all eight zombie categories.

**`collector_ai.py` — Cognitive Services discovery + Azure OpenAI token usage.** Discovers *every*
Cognitive Services account in a subscription via Resource Graph, regardless of `kind` — not just
OpenAI/AIServices — because the governance checks downstream (local auth, public network, private
endpoint) are meaningful for a Vision or Speech resource just as much as an OpenAI one. Token-usage
metric collection, however, only runs for `kind in {"openai", "aiservices"}` (`TOKEN_TRACKED_KINDS`),
since every other kind is billed per-transaction and never produces token metrics — attempting it
would just be a wasted Monitor call. Two metric-name sets are tried in order (`PRIMARY`: newer
model generations' `InputTokens`/`OutputTokens`/`TotalTokens`; `FALLBACK`:
`ProcessedPromptTokens`/`GeneratedTokens`/`TokenTransaction`), each first attempted split by the
`ModelDeploymentName` dimension and falling back to an unsplit, resource-level read if that
dimension isn't supported. Per-deployment model names come from a real
`CognitiveServicesManagementClient.deployments.list()` call (not inferred from the deployment
name), used to estimate cost against a static `AI_PRICING` table. Returns `(rows_saved,
resources)` — the full resource list (every kind, including the governance fields it already
projected) is handed straight to `analyzer_ai_posture.py` by `runner.py`, so there's no second
Resource Graph discovery call just for governance.

### Analyzers (DB-only, write `Finding` rows)

**`analyzer_zombie.py`** turns `collector_zombie.py`'s raw category lists into findings. Where a
real `DailyCost` match exists for a resource (case-insensitive, prorated `total × 30/window_days`),
it's used; otherwise a conservative flat-rate estimate fills in (e.g. $3.65/month for an
unattached static Public IP, SKU-based per-GB rates for orphaned disks). `stopped_not_deallocated`
and `no_autoshutdown` findings are skipped entirely rather than reported with a guessed cost when
no billing data exists yet, since a guess on compute cost specifically is considered worse than no
finding.

**`analyzer_resource_utilization.py`** is the general-purpose rightsizing analyzer: a
per-namespace `RESOURCE_PROFILES` table defines the "is this being used" signal for each resource
type (CPU% for VMs, transactions/day for Storage, requests/day for Web Apps, API calls/day for
*any* kind of Cognitive Services account) with an idle threshold and a looser underutilized
threshold. A resource below the idle threshold gets a high-severity `idle_account` finding; between
idle and underutilized gets a medium-severity `wrong_tier` finding at 35% of its cost. A
`has_cost_data` existence check (not a `monthly_cost == 0` check) gates every finding — a resource
with real but small/credit-covered billing still gets evaluated, but one with no cost rows at all
is skipped rather than producing an unexplainable $0 finding.

**`analyzer_idle.py`** is the Cognitive-Services-specific idle detector, keyed on the
`TotalCalls` metric: fully idle (zero calls ever) is high severity at full prorated cost; idle for
30+ consecutive trailing days is also high severity; a low-but-nonzero 30-day average is a
medium-severity finding at 60% of cost. `S0_FLOOR_COST` ($10/month) is used as the cost estimate
whenever no `DailyCost` match exists — a flat figure, not prorated, since it's already monthly.

**`analyzer_token.py`** measures *efficiency*, not just usage: for each Cognitive Services
resource with real prompt-token metrics, it infers a use case from the resource's name
(summarization, classification, chat, code generation, etc., each with its own healthy
generated-to-prompt token ratio threshold) and flags accounts whose actual ratio falls well below
that threshold — a sign of excessive, poorly-compressed context/system-prompt overhead rather than
genuine model usage. Monthly spend is computed from *both* prompt and generated token volume
(`PROMPT_COST_PER_1K` and `GENERATED_COST_PER_1K`, since generated tokens bill at roughly double
the rate), and the finding's months-active denominator counts distinct `(year, month)` pairs, not
bare month numbers — a resource with data in November 2024 and November 2025 correctly counts as
two months of history, not one.

**`analyzer_anomaly.py`** is the one analyzer whose finding isn't tied to a single Azure resource
— it compares the current week's total spend (annualized from however many days of the week have
elapsed so far) against an 8-week trailing median baseline, flagging `medium` at 1.5× and `high`
at 3×. Two guards exist specifically to prevent false positives on thin data: at least 4 of the 8
baseline weeks must show more than $1 of spend, and the baseline itself must be at least
$5/week — a brand-new client with one week of real billing history would otherwise show an
arbitrarily huge "spike" purely from going from ~$0 to anything. The top 3 resources contributing
to the increase are surfaced by name in the finding's evidence text and in a cost-spike alert
email, sent automatically when this analyzer fires.

**`analyzer_reservations.py`** doesn't compute reservation economics itself — it calls
`AdvisorManagementClient.recommendations.list()` live, per subscription, and translates Azure
Advisor's own Cost-category "reservation" recommendations into `reservation_opportunity` findings.
This is a deliberate choice: Advisor already has access to Azure's internal historical-usage-vs-
reservation-pricing models that aren't otherwise exposed via Cost Management or Resource Graph, so
re-deriving that math independently would be both more work and less accurate. Every
subscription's call is individually wrapped — a missing RBAC role or an Advisor outage on one
subscription never blocks the others.

**`analyzer_ai_posture.py`** scores security/governance posture, not cost, for every Cognitive
Services resource `collector_ai.py` found (any kind). Three findings are possible per resource —
API-key auth still enabled, public network access not disabled, no private endpoint configured —
all high/medium severity with `monthly_savings` hardcoded to `0`, since this is a security
conversation, not a savings one. A `posture_score` (0–10) is computed from six boolean controls,
of which only four are actually evaluated this phase; content-filter and Defender-for-AI detection
both require API calls this phase deliberately doesn't make yet, so those two controls always
count as failing rather than being excluded from the denominator — a 10/10 score is not currently
reachable by design, not by bug.

**`collector_shadow_ai.py`** ("shadow AI" discovery) is named `collector_*` but is one of two
deliberate exceptions to the collector/analyzer split (the other being
`analyzer_reservations.py`): it also writes `Finding` rows directly, since splitting a
single-pass discover-and-flag check into a separate raw-data model used exactly once wasn't worth
the overhead. It scans four AI-*adjacent* resource types beyond what `collector_ai.py` tracks
(Cognitive Services accounts, AI Search, Bot Service, Azure ML workspaces) and flags a resource
`shadow_ai_resource` only if it's **both** absent from `DailyAIUsage` (ungoverned) **and** missing
both a `CostCenter` and an `Owner` tag (unattributable) — a resource that's merely untracked but
properly tagged is treated as a deliberate, attributable provisioning choice, not shadow usage.

## 5. KQL queries

All eight queries below run through `collector_zombie.py`'s `ResourceGraphClient`, scanning every
subscription in one call via Resource Graph's cross-subscription query support.

**Unattached public IP:**
```kql
resources
| where type =~ 'microsoft.network/publicipaddresses'
| extend ipConfigId = tostring(properties.ipConfiguration.id)
| extend nicId = tolower(extract(@'(.+)/ipConfigurations/', 1, ipConfigId))
| join kind=leftouter (
    resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | project nicId = tolower(id), nicVm = properties.virtualMachine
) on nicId
| where isnull(properties.ipConfiguration) or isnull(nicVm)
| project id, name, resourceGroup, location, sku = tostring(sku.name),
          allocationMethod = tostring(properties.publicIPAllocationMethod)
```
An IP is orphaned two different ways, and this query catches both in one pass: either it has no
`ipConfiguration` at all (never attached to anything), or its `ipConfiguration` points to a NIC
that is *itself* detached from any VM (a dangling NIC still technically holds the IP, but nothing
is using it). The `join` against the `networkinterfaces` table is the part a plain REST call can't
do in one round trip — this is the exact case Resource Graph's cross-resource-type querying exists
for.

**Orphaned managed disk:**
```kql
resources
| where type =~ 'microsoft.compute/disks'
| where isnull(managedBy) or managedBy == ''
| project id, name, resourceGroup, location,
          diskSizeGb = toint(properties.diskSizeGb),
          sku = tostring(sku.name),
          timeCreated = tostring(properties.timeCreated)
```
`managedBy` is null for a disk that's never been attached, but an empty string for one that *was*
once attached and then detached — Azure is inconsistent about which, so both are checked
explicitly. Result rows are disks billing for storage nothing references anymore.

**Unattached network interface:**
```kql
resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isnull(properties.virtualMachine)
| project id, name, resourceGroup, location
```
A NIC with no `virtualMachine` reference. NICs don't bill directly, so this is flagged as
low-severity hygiene clutter, not a cost finding.

**Unused network security group:**
```kql
resources
| where type =~ 'microsoft.network/networksecuritygroups'
| where array_length(properties.networkInterfaces) == 0
    and array_length(properties.subnets) == 0
| project id, name, resourceGroup, location
```
An NSG attached to neither a NIC nor a subnet is pure audit noise — rules that protect nothing
but still have to be reviewed in a security audit.

**Aging snapshot:**
```kql
resources
| where type =~ 'microsoft.compute/snapshots'
| extend createdTime = todatetime(properties.timeCreated)
| where createdTime < ago(90d)
| project id, name, resourceGroup, location,
          diskSizeGb = toint(properties.diskSizeGb),
          timeCreated = tostring(properties.timeCreated)
```
Snapshots older than 90 days with no retention-policy signal — a one-off manual snapshot that
was taken and forgotten, as opposed to a managed backup lifecycle.

**Stopped but not deallocated VM:**
```kql
resources
| where type =~ 'microsoft.compute/virtualmachines'
| extend powerState = tostring(properties.extended.instanceView.powerState.code)
| where powerState == 'PowerState/stopped'
| project id, name, resourceGroup, location, powerState,
          vmSize = tostring(properties.hardwareProfile.vmSize)
```
The single highest-value zombie check in the set: Azure distinguishes an OS-level shutdown
(`PowerState/stopped`, compute billing continues) from a true deallocation
(`PowerState/deallocated`, compute billing stops) — a distinction almost no one outside
infrastructure engineering knows to check for, and exactly the kind of finding this product exists
to surface in plain English.

**Empty load balancer:**
```kql
resources
| where type =~ 'microsoft.network/loadbalancers'
| extend firstPoolBackendConfigs = properties.backendAddressPools[0].properties.backendIPConfigurations
| where array_length(properties.backendAddressPools) == 0
    or isnull(firstPoolBackendConfigs)
    or array_length(firstPoolBackendConfigs) == 0
| project id, name, resourceGroup, location, sku = tostring(sku.name)
```
Three different ways an LB can be "empty" are checked explicitly, because `array_length(null)`
returns `null`, not `0` — a backend pool whose `backendIPConfigurations` property is missing
entirely (as opposed to present-but-empty) would silently slip past a naive `== 0` check without
the explicit `isnull()` guard.

**No auto-shutdown schedule** (two queries — a candidate scan, then a cross-reference):
```kql
resources
| where type =~ 'microsoft.compute/virtualmachines'
| extend envTag = tolower(tostring(tags.environment))
| where envTag !in ('production', 'prod')
| project id, name, resourceGroup, location, envTag,
          vmSize = tostring(properties.hardwareProfile.vmSize)
```
```kql
resources
| where type =~ 'microsoft.devtestlab/schedules'
| where name contains 'shutdown-computevm'
| project targetResourceId = tostring(properties.targetResourceId)
```
Every non-production-tagged VM is fetched first, then checked in Python against the set of VMs
that already have a DevTest Lab auto-shutdown schedule targeting them — a VM is only flagged if
it's in the first set and absent from the second.

**Why Resource Graph/KQL instead of per-type REST calls:** every one of these checks either needs
a `join` across two resource types (public IP ↔ NIC), a cross-reference between two independent
result sets (VM ↔ shutdown schedule), or a property-path extraction that would otherwise require
fetching and parsing full ARM resource bodies one at a time via the Compute/Network/Storage REST
APIs. Resource Graph answers all eight in one or two round trips total, across every subscription
at once, at a rate limit far more generous than Cost Management's — collecting the same signals via
individual `GET` calls per resource would mean hundreds of REST calls per pipeline run instead of
eight KQL queries.

## 6. Savings calculation

`core/savings.py::canonical_monthly_savings(client)` is the one function every page that shows a
"savings" number calls — the Overview page, the Findings page topbar, the staff client list, the
Spend page, and the monthly PDF brief. Before this existed, each of those computed its own raw
`Sum(estimated_monthly_savings_usd)` independently, and they drifted: the Overview page excluded
certain finding types and capped per-resource totals while the others didn't, so the same client
could see three different "savings" figures depending which page they were on.

Three rules, applied in order:

1. **Exclude `cost_anomaly` and the AI-governance finding types.** A `cost_anomaly` finding's
   `estimated_monthly_savings_usd` is actually a projected *spike* — extra spend already
   happening, not something the client can act on to save money — so it would inflate, not
   represent, "savings." The eight AI-governance finding types (`ai_local_auth_enabled`,
   `shadow_ai_resource`, etc.) carry `$0` by design already, but are excluded explicitly anyway so
   the sum stays correct even if one of them is ever accidentally written with a non-zero value.
2. **Exclude findings on deleted resources.** `ResourceStatus` rows where `exists=False` (written
   by `live_check.py`) mark a resource as confirmed gone from Azure — its findings are excluded so
   a resource that no longer exists can't still be counted as "savings available."
3. **Cap each resource's combined savings at its own real monthly cost.** `Finding` is unique per
   `(client, resource_id, finding_type)`, so one VM can carry several findings at once — a
   `no_autoshutdown` finding at 65% of cost *and* a `wrong_tier` finding at 35% of cost, say — each
   computed independently against the same underlying bill. Summed naively, two findings on one
   resource can claim more than 100% of what that resource actually costs, which is impossible to
   realize. `_cap_savings()` clamps the per-resource total at its actual monthly cost; a resource
   with no billing data at all (nothing to cap against) keeps its raw estimate.

**Why `total × 30 / span` instead of `total × 30 / window_days`:** the nominal analysis window is
a rolling 90 days, but a resource created 10 days ago has only 10 days of real billing history —
dividing its 10-day total by 90 and multiplying by 30 would understate its true monthly run rate
by 9×. `_monthly_cost_by_resource()` instead computes `span` as that specific resource's actual
*billed* span (`last billed date − first billed date + 1`), with a 7-day floor so a resource with
only one or two days of billing isn't extrapolated 15–30× from a single day's charge. This is also
why the headline "Est. monthly spend" figure on the Overview page agrees with what each individual
resource card shows — both go through the same span-based calculation, rather than one using the
nominal window and the other using actual billed days.

**Why this file duplicates logic instead of importing it:** `dashboard/views.py` already has
nearly identical `_monthly_cost_by_resource()` / `_cap_savings()` / `_capped_total_savings()`
functions, written first for the Overview page. `core/savings.py` doesn't import them — it
reimplements the same logic — because of the project's import-direction rule: `core/` must never
depend on `dashboard/`. The functions are small enough (a few dozen lines total) that duplicating
them is a deliberately accepted tradeoff against adding a dependency in the wrong direction; any
future change to the exclusion or capping rules has to be applied in both places.

## 7. Local development setup

**Prerequisites:** Python 3.12, PostgreSQL (optional — SQLite works for UI-only development), and
the Azure CLI logged in (`az login`) if you want to run the pipeline against a real subscription.

```bash
git clone <this-repo>
cd ghashem
python3.12 -m venv .venv
source .venv/bin/activate          # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# Fill in at minimum: DJANGO_SECRET_KEY (any random string for local dev)
# Leave DB_HOST empty to use local SQLite instead of Postgres.

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then visit `http://localhost:8000/login/` and sign in with the superuser account you just
created.

**What works without Azure credentials:** the dashboard UI, admin, onboarding forms, and contract
tracker all run against whatever's already in the database. **What doesn't:** actually running the
pipeline (`/client/<slug>/` → "Run analysis", or `python manage.py run_pipeline --client <slug>`)
requires a real Azure credential with at least Reader + Cost Management Reader + Monitoring Reader
+ Billing Reader on a real subscription — there's no mock/offline mode for the collectors. If you
just want to see the UI with realistic-looking data, you'll need to either point the app at a
subscription you control, or manually create `Finding`/`DailyCost` rows via the Django shell/admin.

## 8. Deployment

`01-provision.ps1` through `04-smoke-test.ps1` provision and deploy the entire Azure stack from
scratch: resource group, App Service (Linux, Python 3.12), PostgreSQL Flexible Server, two Key
Vaults (one for per-client secrets, one for the SSO app credential), Log Analytics, Defender for
Cloud, and a Key Vault access-spike alert — in that order, each script idempotent against a
partial previous run. `00-setup-sso-secret.ps1` runs once, ahead of everything else, to store the
Entra app registration's client secret.

The App Service's system-assigned Managed Identity is granted `Key Vault Secrets User` on the
per-client vault and `Key Vault Secrets Officer` is granted to human operators — no secret is ever
written to an App Service application setting, a config file, or committed to source. The only
thing `DJANGO_SECRET_KEY`/`DB_PASSWORD`/etc. app settings hold is infrastructure plumbing (database
connection parameters, the Django signing key) — every Azure credential the pipeline itself uses
comes from `DefaultAzureCredential` resolving to the Managed Identity at runtime.

Both top-level config variables (`$AppName`, `$Env`, at the top of each script) derive every Azure
resource name, so forking this repo to provision your own instance means changing those two
values, not hunting through five scripts for hardcoded names.

## 9. Known limitations / roadmap

- **No task queue.** The pipeline runs as a bare `threading.Thread` inside the Django request
  worker process, with no retry logic and no durability guarantee if that worker recycles
  mid-run. Migrating to Celery/RQ against Azure Service Bus or Redis is the single most important
  pending infrastructure change.
- **No automated tests.** Zero unit or integration tests exist anywhere in the codebase as of this
  writing.
- **No CI/CD.** Deployment is manual: run `02-deploy-code.ps1` from a developer machine. There is
  no GitHub Actions pipeline building or testing on push.
- **Known proration inconsistency.** `analyzer_zombie.py` and `analyzer_resource_utilization.py`
  still prorate a resource's monthly cost as `total × 30 / window_days` (the nominal 90-day window)
  rather than the actual-billed-span calculation `core/savings.py` and the dashboard cards already
  use — tracked, fix pending, documented in `CLAUDE.md`.
- **Multi-subscription AI collection is incomplete.** `collector_ai.py` and `collector_shadow_ai.py`
  currently scope their Resource Graph discovery to a single subscription (the first one passed in)
  rather than every subscription a client has, unlike the cost/metrics/zombie collectors — tracked,
  fix pending.

## 10. License

MIT License — see [LICENSE](LICENSE).
