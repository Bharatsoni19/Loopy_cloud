# Loopy — Cloud · DevOps · FinTech edition

**A neural music-streaming platform with a shareable, rechargeable, fully-audited payment layer — built as a BE-CSE Semester 6 capstone for the *API & Cloud · DevOps* module.**

Loopy started life as a single-file neural music player. This edition takes
that core and wraps a production-shaped cloud + DevOps stack around it:

* A bespoke **Loop Cards** payment microservice with a double-entry ledger,
  P2P transfers, idempotency keys, and ISO 20022-style end-to-end ids.
* An **nginx API gateway** that does path-based routing, rate-limit
  throttling, gzip compression and OWASP-flavoured security headers.
* A **RAG support service** ("Ask Loopy") with a pluggable LLM backend.
* The original **neural backend** (TensorFlow models + a websocket admin
  feed), now containerised and reachable through the gateway.
* **Multi-AZ AWS** — an Application Load Balancer fanning out to N app
  EC2s across two AZs (default 2), plus a dedicated **observability EC2**
  running Prometheus + Grafana.
* **Observability stack** — Grafana dashboard combining Prometheus
  scrapes of every app instance with CloudWatch infra metrics; a
  CloudWatch dashboard with 4 widgets; 3 CloudWatch alarms (ALB 5xx, ALB
  unhealthy host, EC2 CPU > 80%).
* Full **Terraform IaC** that provisions VPC + 2 subnets + ALB + N×app
  EC2 + obs EC2 + S3 ×3 + IAM + Glue + CloudWatch dashboard & alarms.
* A **GitHub Actions CI/CD pipeline** with lint, tests, DevSecOps scans
  (Bandit · Gitleaks · Trivy), Terraform validation, container build &
  push to GHCR, and SSH-based deploy.
* An **AWS Glue PySpark ETL** that turns raw payment events in S3 into
  partitioned curated Parquet ready for analytics.

> 📑 The complete topic-by-topic mapping from the syllabus to this code
> is in [`docs/SYLLABUS_MAPPING.md`](docs/SYLLABUS_MAPPING.md).
> The architecture and design rationale are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
> Operations live in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## The headline feature — *Loop Cards*

> *"Graphically fabulous cards friends can share. They recharge for 60
> minutes and sync along."*

Each Loop Card is a holographic prepaid pass for premium listening:

| | |
|---|---|
| **Themes** | Six hand-tuned gradient-mesh themes — aurora, midnight, sunset, matrix, candy, gold — with a moving sheen, chip and grain. |
| **Recharge** | 120 Loopy Coins → 60 minutes; capped at 1440 min/card. |
| **Share** | Add a friend via `/cards/{id}/share`. Both owner and shared friends can redeem. |
| **Transfer** | Send minutes peer-to-peer — generates an ISO 20022-style `end_to_end_id`. |
| **Sync along** | The player calls `/redeem` once per listening minute, so the live timer on the card matches the music. |
| **Auditable** | Every action is two balancing ledger entries. `/health` exposes the system trial balance — it **must** net to zero. |

---

## Quickstart (local, one command)

```bash
docker compose up -d --build
open http://localhost                   # Loopy SPA — tap the "Loop Cards" tab
python3 scripts/seed_demo.py http://localhost/api/pay   # optional demo data
```

What's actually running:

```
nginx :80   ──┬──► /             static SPA
              ├──► /api/pay/     payments :8100   (Loop Cards · ledger · JWT)
              ├──► /api/neural/  neural   :8000   (TF models · /ws/admin)
              └──► /api/rag/     rag      :8200   (Ask Loopy · TF-IDF)
```

## Quickstart (AWS, one command)

```bash
cd infra/terraform
terraform init
terraform apply -var="key_name=my-keypair" -var="my_ip=$(curl -s ifconfig.me)/32"

# Wait ~3 min for user-data, then open:
terraform output app_url               # ALB → app EC2s
terraform output grafana_url           # Grafana dashboard
terraform output cloudwatch_dashboard_url
```

Defaults you can tune with `-var=`: `app_instance_count` (default 2),
`observability_enabled` (default true), `instance_type` (`t3.small`),
`grafana_admin_password` (`loopy-admin`).

Full provisioning details, secrets, and rollback are in
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## Repo tour

```
loopy-cloud/
├── frontend/                           # the SPA (single-file HTML + injected CSS/JS for cards)
│   ├── loopy-mega-v1.html              # gateway-aware, served by nginx
│   ├── _loopcards.css.html             # holographic card CSS — six themes
│   └── _loopcards.js.html              # LoopPay client · live timer · sync-along loop
│
├── services/
│   ├── payments/                       # FastAPI · port 8100
│   │   ├── ledger.py                   # ★ double-entry ledger (SQLite WAL)
│   │   ├── app.py                      # routes · /metrics · idempotency · S3 archive
│   │   ├── security.py                 # dependency-free HS256 JWT + token-bucket throttle
│   │   ├── models.py                   # LoopCard, LedgerEntry, themes
│   │   └── tests/test_ledger.py        # 7 pytest cases — all pass
│   ├── neural/                         # original TF backend, containerised
│   └── rag/                            # TF-IDF retriever; Bedrock-pluggable
│
├── nginx/nginx.conf                    # the API gateway — routing · throttling · gzip · security
├── docker-compose.yml                  # one-command local stack
│
├── observability/                      # ★ NEW — Prometheus + Grafana stack
│   ├── docker-compose.yml              # runs on the obs EC2
│   ├── prometheus.yml                  # scrape config (file-SD from app instance IPs)
│   ├── prometheus.targets.json         # rendered at boot by Terraform user-data
│   └── grafana/
│       ├── provisioning/datasources/   # Prometheus + CloudWatch
│       ├── provisioning/dashboards/    # autoload provider
│       └── dashboards/loopy-overview.json  # 8-panel dashboard (Prom + CW combined)
│
├── infra/terraform/                    # IaC — VPC · ALB · 2×app EC2 · obs EC2 · S3 ×3 · IAM · Glue · CloudWatch (dashboard + alarms)
│   ├── main.tf  variables.tf  outputs.tf
│   ├── user_data.sh.tftpl              # app instance bootstrap
│   └── user_data_observability.sh.tftpl  # obs instance bootstrap (renders prometheus targets)
│
├── glue/loopy_etl.py                   # PySpark — raw S3 → curated Parquet, hourly
├── glue/test_etl_local.py              # local pandas mirror — verified
│
├── .github/workflows/deploy.yml        # CI/CD: lint · test · DevSecOps · IaC · build · deploy
│
├── scripts/
│   ├── deploy_ec2.sh                   # idempotent EC2 deploy (used by CI and by humans)
│   └── seed_demo.py                    # tells a small story to populate the demo
│
└── docs/
    ├── ARCHITECTURE.md                 # diagram · design decisions · what we deliberately didn't build
    ├── RUNBOOK.md                      # deploy · ops · troubleshooting · rollback · cost · RDS migration path
    └── SYLLABUS_MAPPING.md             # every syllabus topic → the file that implements it
```

---

## What's been live-tested

* **Ledger invariants** — 7 pytest cases (recharge banks 60 min, requires
  coins, idempotent, P2P conserves minutes, shared friend can redeem,
  outsiders blocked, trial balance always zero).
* **End-to-end API** — recharge → 60 min live; transfer 25 min → friend
  gets a new card with 25 min + `end_to_end_id`; trial balance balanced;
  shared friend can redeem.
* **Gateway** — routing with prefix-stripping (`/api/pay/health` →
  upstream `/health`), throttling (11 requests pass burst then 19 × 429),
  security headers, JWT auth route.
* **RAG** — 3 grounded answers + citations.
* **Glue ETL** — local pandas mirror dedups 1 duplicate `txn_id`, emits
  4 fact rows and 3 daily KPI rows.
* **nginx config** — `nginx -t` passes with real-resolvable upstreams.
* **Terraform** — braces balanced; brace-validated (the sandbox can't
  reach `releases.hashicorp.com`, so `terraform validate` is run by the
  CI pipeline).

Re-run locally with:

```bash
cd services/payments && pytest -v
cd ../../glue && python3 test_etl_local.py
```

---

## Tech inventory

| Layer | Tech |
|---|---|
| Frontend | Single-file HTML + ES2020 + Tailwind-free hand CSS (Syne · DM Mono) |
| Payments | Python 3.12 · FastAPI · Uvicorn · SQLite (WAL) · stdlib HMAC-SHA256 JWT |
| Neural | Python · TensorFlow (CPU) · FastAPI · WebSocket |
| RAG | Python · scikit-learn TF-IDF · pluggable Bedrock backend |
| Gateway | nginx 1.27-alpine · gzip · `limit_req_zone` · OWASP headers |
| Cloud | AWS — EC2 · S3 · IAM · CloudWatch · Glue |
| IaC | Terraform (AWS provider) |
| CI/CD | GitHub Actions — ruff · pytest · Bandit · Gitleaks · Trivy · `terraform validate` · docker buildx · GHCR · SSH deploy |
| Containers | Docker · docker-compose · multi-stage Dockerfiles · non-root users |

---

## Credits

Built by **Bharat Soni** as the Semester 6 capstone for the *API & Cloud · DevOps* module, BE Computer Science Engineering. The original Loopy neural music player is preserved intact under `services/neural/` — this edition wraps it in the cloud + DevOps tooling the brief asked for.
