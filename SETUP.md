# Ghashem — Setup & Configuration Guide

## Prerequisites

- Python 3.11+
- PyCharm (Community or Professional)
- Azure CLI (`az`) installed and authenticated (`az login`)
- Access to an Azure Key Vault in your own tenant

---

## 1. Clone / open the project in PyCharm

1. Open PyCharm → **File → Open** → select the `ghashem/` folder
2. PyCharm will detect it as a Python project

---

## 2. Create a virtual environment

In PyCharm:
1. **File → Settings → Project: ghashem → Python Interpreter**
2. Click the gear icon → **Add Interpreter → Add Local Interpreter**
3. Choose **Virtualenv** → **New** → Location: `ghashem/.venv` → Python: 3.11+
4. Click **OK**

Or from the PyCharm terminal:
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

---

## 3. Install dependencies

In the PyCharm terminal (venv active):
```bash
pip install -r requirements.txt
```

---

## 4. Configure your client in clients.yaml

Edit `config/clients.yaml`:

```yaml
app_client_id: "YOUR-APP-REGISTRATION-CLIENT-ID"
vault_url: "https://YOUR-KEYVAULT-NAME.vault.azure.net/"

clients:
  - slug: acme-corp                        # URL-safe identifier, no spaces
    display_name: Acme Corporation
    active: true
    analysis_start: "2024-12-01"           # 6-month window start
    analysis_end: "2025-06-01"
    subscriptions:
      - id: "SUBSCRIPTION-UUID-HERE"
        display_name: Production
    focus_areas:
      - cognitive_services
    report_currency: CAD
```

**Slug rules:** lowercase, hyphens only, no spaces (e.g. `acme-corp`, `globex-inc`)

---

## 5. Set up the Azure app registration

### In your Azure tenant (one-time):

1. Azure Portal → **Microsoft Entra ID → App registrations → New registration**
2. Name: `Ghashem-FinOps-Connector`
3. Supported account types: **"Accounts in any organizational directory (Multitenant)"**
4. Click **Register**
5. Copy the **Application (client) ID** → paste into `clients.yaml` as `app_client_id`

### Add a client secret (simplest for local dev):
1. **Certificates & secrets → New client secret**
2. Set expiry to 12 months → **Add**
3. Copy the value immediately — you won't see it again

### Store it in Key Vault:
```bash
az keyvault secret set \
  --vault-name YOUR-KEYVAULT-NAME \
  --name "client-acme-corp-secret" \
  --value "YOUR-SECRET-VALUE"

az keyvault secret set \
  --vault-name YOUR-KEYVAULT-NAME \
  --name "client-acme-corp-tenant-id" \
  --value "CLIENT-TENANT-UUID"

az keyvault secret set \
  --vault-name YOUR-KEYVAULT-NAME \
  --name "client-acme-corp-subscription-id" \
  --value "CLIENT-SUBSCRIPTION-UUID"
```

**Secret naming convention:** `client-{slug}-secret`, `client-{slug}-tenant-id`,
`client-{slug}-subscription-id`

---

## 6. Grant the client's admin consent

Send this URL to the client's Global Admin or Application Administrator
(replace both UUIDs):

```
https://login.microsoftonline.com/{CLIENT-TENANT-ID}/adminconsent?client_id={YOUR-APP-CLIENT-ID}
```

They open it, review the permissions screen, and click **Accept**.
This creates your app's service principal inside their tenant.

---

## 7. Assign RBAC roles in the client's subscription

The client (or you, if you have access) runs this in their tenant:

```bash
# Get your app's service principal object ID in their tenant
az ad sp show --id YOUR-APP-CLIENT-ID --query id -o tsv

# Assign the three read-only roles
az role assignment create \
  --assignee "SERVICE-PRINCIPAL-OBJECT-ID" \
  --role "Reader" \
  --scope "/subscriptions/CLIENT-SUBSCRIPTION-ID"

az role assignment create \
  --assignee "SERVICE-PRINCIPAL-OBJECT-ID" \
  --role "Cost Management Reader" \
  --scope "/subscriptions/CLIENT-SUBSCRIPTION-ID"

az role assignment create \
  --assignee "SERVICE-PRINCIPAL-OBJECT-ID" \
  --role "Monitoring Reader" \
  --scope "/subscriptions/CLIENT-SUBSCRIPTION-ID"
```

**Never assign Contributor or Owner.** Ghashem is read-only by design.

---

## 8. Run Django migrations

```bash
python manage.py migrate
```

---

## 9. Create a superuser (for the web login)

```bash
python manage.py createsuperuser
```

Follow the prompts to set username and password.

---

## 10. Configure PyCharm run configurations

### Django server (for the dashboard):
1. **Run → Edit Configurations → + → Django Server**
2. Name: `Ghashem Dev Server`
3. Host: `127.0.0.1` Port: `8000`
4. Environment variables: *(none needed for local)*
5. Click **OK**
6. Press **Run (▶)** — the dashboard will be at `http://127.0.0.1:8000`

### Pipeline runner:
1. **Run → Edit Configurations → + → Python**
2. Name: `Run Pipeline`
3. Script: `manage.py`
4. Parameters: `run_pipeline --client acme-corp`
5. Working directory: `ghashem/` (the project root)
6. Click **OK**
7. Press **Run (▶)** to trigger a full collect + analyze cycle

---

## 11. Run the pipeline for the first time

Via PyCharm run config (step 10), or in the terminal:
```bash
python manage.py run_pipeline --client acme-corp
```

Or via the web dashboard: open `http://127.0.0.1:8000` → sign in →
click **Run pipeline** on the Summary page.

The first run for a 6-month window typically takes **10–30 minutes**
depending on the number of resources in the tenant (the Monitor API
pull is the slow part). Progress is visible in the log modal.

---

## 12. Verify everything is working

```bash
# Check migrations are clean
python manage.py migrate --check

# Open the Django shell and verify a client loaded
python manage.py shell
>>> from core.config import load_config
>>> cfg = load_config()
>>> print(cfg.active_clients())
```

---

## Project structure reference

```
ghashem/
├── config/
│   └── clients.yaml          ← client config (edit this)
├── core/                     ← Django models, auth, config loader
│   ├── models.py
│   ├── auth.py
│   └── config.py
├── pipeline/                 ← collectors + analyzers
│   ├── collector_cost.py
│   ├── collector_metrics.py
│   ├── analyzer_token.py
│   ├── analyzer_idle.py
│   ├── runner.py
│   └── management/commands/run_pipeline.py
├── dashboard/                ← Django views + templates
│   ├── views.py
│   ├── urls.py
│   └── templates/dashboard/
│       ├── base.html
│       ├── summary.html
│       ├── findings.html
│       ├── deep_dive.html
│       └── spend.html
├── ghashem/                  ← Django project settings
│   ├── settings.py
│   └── urls.py
├── manage.py
├── requirements.txt
└── db.sqlite3                ← created on first migrate
```

---

## Common issues

| Symptom | Fix |
|---|---|
| `DefaultAzureCredential` fails | Run `az login` in the terminal before starting |
| Key Vault 403 | Add yourself: `az keyvault set-policy --name kv-name --upn you@domain.com --secret-permissions get list` |
| Cost API returns empty | Check that `Cost Management Reader` role is assigned on the subscription, not just a resource group |
| Monitor metrics all null | Some resource types need the `microsoft.insights` provider registered: `az provider register --namespace microsoft.insights` |
| `django.db.OperationalError` | Run `python manage.py migrate` |
