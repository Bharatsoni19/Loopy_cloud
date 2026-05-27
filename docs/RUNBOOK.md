# Runbook

Operational playbook for the Loopy stack — local dev, AWS provisioning,
release, common operations and troubleshooting.

---

## 1. Local development

### 1.1 Run the whole stack (recommended)

```bash
cd loopy-cloud
docker compose up -d --build
open http://localhost                      # the SPA
curl  http://localhost/healthz             # gateway liveness
curl  http://localhost/api/pay/health      # payments + trial balance
```

### 1.2 Seed demo data

```bash
python3 scripts/seed_demo.py http://localhost/api/pay
# or, if running services directly:
python3 scripts/seed_demo.py http://localhost:8100
```

### 1.3 Run a single service without docker

```bash
cd services/payments
pip install -r requirements.txt
LOOPY_JWT_SECRET=dev uvicorn app:app --port 8100 --reload
```

### 1.4 Tests

```bash
cd services/payments && pytest -v
cd glue && python3 test_etl_local.py
```

---

## 2. AWS provisioning (one-time per environment)

> Pre-requisites: AWS account, an EC2 key-pair, your public IP, Terraform ≥1.5.

```bash
cd infra/terraform
cp variables.tf.example terraform.tfvars   # then edit:
#   key_name = "your-ec2-keypair"
#   my_ip    = "203.0.113.42/32"
#   repo_url = "https://github.com/<you>/loopy-cloud.git"

terraform init
terraform plan -out plan.tfplan
terraform apply plan.tfplan
```

Outputs printed at the end include:

* `app_url` — visit `http://<alb-dns>/` once user-data finishes (~3 min).
* `alb_dns` — the load balancer DNS (this is what end users hit).
* `app_public_ips` — list of public IPs of every app EC2 (SSH from operator).
* `obs_public_ip` — public IP of the observability box.
* `grafana_url` — Grafana UI URL + login (admin / `var.grafana_admin_password`).
* `prometheus_url` — Prometheus UI URL (for verifying scrape targets).
* `cloudwatch_dashboard_url` — direct link to the CloudWatch dashboard.
* `raw_bucket`, `curated_bucket`, `frontend_bucket`.
* `glue_database`, `glue_job`.
* `ssh_app`, `ssh_obs` — copy-pasteable SSH commands per host.

### 2.1 What Terraform creates (summary)

| Group | Resources |
|---|---|
| Network | VPC `10.20.0.0/16`, IGW, **two** public subnets in `${region}a/b`, route table |
| Security | **3 SGs**: ALB (80/443 from world), app (80 from ALB & obs only, SSH from `my_ip`), obs (Grafana/Prometheus from `my_ip` only) |
| Load balancer | **Application Load Balancer** + target group (HTTP /healthz) + listener |
| Compute · app | **N × EC2** (default 2) in different AZs, registered to the ALB target group |
| Compute · obs | **1 × EC2** running Prometheus + Grafana via `observability/docker-compose.yml` |
| Storage | S3 ×3 (raw / curated / frontend) — block-public, AES256 |
| IAM | App role (S3 RW + Logs + Bedrock), obs role (CloudWatch read), Glue role |
| Logs | CloudWatch log group `/loopy/dev/app` (14-day retention) |
| Dashboards | CloudWatch dashboard `<name>-overview` with 4 widgets |
| Alarms | ALB 5xx high · ALB unhealthy host · EC2 CPU > 80% (one per app instance) |
| Data | Glue catalog DB, hourly crawler, PySpark ETL job (G.1X ×2) |

### 2.2 Scaling the app fleet

```bash
terraform apply -var="app_instance_count=4"
```

That single change adds two more app EC2s, attaches them to the ALB target
group, and provisions two more CPU alarms. No code changes.

### 2.3 Opening the dashboards

```bash
# Grafana
open "$(terraform output -raw grafana_url | awk '{print $1}')"
# (login: admin / loopy-admin  — change with -var="grafana_admin_password=…")

# Prometheus (verify all scrape targets are UP)
open "$(terraform output -raw prometheus_url)"

# CloudWatch dashboard (in the AWS console)
open "$(terraform output -raw cloudwatch_dashboard_url)"
```

---

## 3. Release / deploy

### 3.1 Continuous

Every push to `main` runs `.github/workflows/deploy.yml`:

1. **lint** — ruff (advisory).
2. **test** — pytest + local ETL test.
3. **security** — Bandit + Gitleaks + Trivy (gating).
4. **iac-validate** — `terraform fmt -check` + `terraform validate`.
5. **build** — docker buildx, push to GHCR.
6. **deploy** — SSH into the EC2 box, run `scripts/deploy_ec2.sh`.

Required GitHub secrets:

| Secret | Used for |
|---|---|
| `EC2_HOST` | SSH host (the Terraform output `app_public_ip`). |
| `EC2_SSH_KEY` | Private key matching the EC2 key-pair. |
| `LOOPY_JWT_SECRET` | Production JWT secret, written into `.env` on the box. |
| `LOOPY_RAW_BUCKET` | The Terraform output `raw_events_bucket`. |
| `GITHUB_TOKEN` | Provided by Actions; pull from GHCR on the box. |

### 3.2 Manual

SSH and run the same script:

```bash
ssh ec2-user@<ip>
sudo REPO_URL=https://github.com/<you>/loopy-cloud.git \
     LOOPY_JWT_SECRET=… LOOPY_RAW_BUCKET=… AWS_REGION=ap-south-1 \
     bash /opt/loopy/scripts/deploy_ec2.sh
```

---

## 4. Common ops

### 4.1 Tail logs

```bash
ssh ec2-user@<ip> 'docker compose -f /opt/loopy/docker-compose.yml logs -f --tail=200'
# or in CloudWatch:
aws logs tail /loopy/dev/app --follow --region ap-south-1
```

### 4.2 Check ledger integrity

```bash
curl -s http://<ip>/api/pay/health | jq .trial_balance
# {"minutes_net":0,"coins_net":0,"balanced":true}
```

If `balanced` is ever `false`, **freeze writes** and triage with
`/api/pay/cards/<id>/journal` — it is a sev-1.

### 4.3 Trigger the Glue ETL on demand

```bash
aws glue start-job-run --job-name loopy-payments-etl --region ap-south-1
aws glue get-job-runs  --job-name loopy-payments-etl --max-items 1
```

### 4.4 Reset demo data

```bash
ssh ec2-user@<ip> 'docker compose -f /opt/loopy/docker-compose.yml exec payments \
    rm -f /data/loopy_pay.db && \
    docker compose -f /opt/loopy/docker-compose.yml restart payments'
```

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser shows the SPA but Loop Cards tab is empty | Payments unreachable from the gateway | `docker compose logs payments`; check `/api/pay/health`. |
| All `/api/pay/*` requests return 429 | You're being rate-limited | Wait, or raise the `limit_req_zone api` rate in `nginx/nginx.conf`. |
| Gateway returns 502 on `/api/neural/*` | Neural container OOM (TF is hungry) | Bump to `t3.medium`, or set `TF_FORCE_GPU_ALLOW_GROWTH=true`. |
| `trial_balance.balanced == false` | A ledger leg was inserted bypassing the `with conn:` block | Stop writes immediately; inspect `journal` of recent txns; restore from S3 raw events. |
| Glue ETL writes nothing | Raw bucket empty | Confirm `LOOPY_RAW_BUCKET` is set in `.env` on the EC2 box and that `archive_to_s3` is not erroring (check CloudWatch). |
| WebSocket admin dashboard disconnects after a minute | nginx default `proxy_read_timeout` too low | Already set to `3600s` in `location /api/neural/`; verify your reverse proxy chain (CloudFront/ALB) doesn't shorten it. |
| User-data didn't run on the new EC2 box | Cloud-init cached an old run | `sudo cloud-init clean && sudo cloud-init init` then `sudo bash /opt/loopy/scripts/deploy_ec2.sh`. |

---

## 6. Rollback

The pipeline tags each container build with the git SHA. To roll back:

```bash
ssh ec2-user@<ip>
cd /opt/loopy
git checkout <previous-sha>
sudo bash scripts/deploy_ec2.sh
```

S3 raw events are immutable, so the ledger can be rebuilt from the
event stream if SQLite is ever lost (`/data` is on a docker volume that
survives container restarts but not instance termination — a future
hardening step is to mount it on EBS or move to RDS).

---

## 7. Cost note (student-friendly)

With the default layout (2 × `t3.small` app + 1 × `t3.small` obs + ALB +
3 small S3 buckets + Glue running ~720 short jobs/month) the monthly bill
on a free-tier-eligible account stays under ~₹2 500. Specifically:

* EC2: ~₹600/instance/month × 3 = ~₹1 800
* ALB: ~₹350/month (idle) + tiny LCU charges
* S3 + Glue catalog: pennies at this volume
* Glue ETL: ~₹2 per run × ~720 runs/month = ~₹1 400

**Stop the EC2 instances + delete the ALB when not demoing**:

```bash
# stop everything but keep the data and IaC state
terraform apply -var="app_instance_count=0" -var="observability_enabled=false"
# bring it back the day of the demo
terraform apply -var="app_instance_count=2"
```

S3 + Glue catalog cost essentially nothing at idle.

## 8. Moving payments to RDS (the production path)

Today each app EC2 has its own SQLite WAL — fine for a demo, but if two
users hit two different EC2s the wallets don't see each other. To fix:

1. Provision RDS PostgreSQL (`db.t3.micro`) in the same VPC, private subnets.
2. Add a SG rule allowing port 5432 ingress from the app SG.
3. Switch `services/payments/ledger.py` from `sqlite3` to `psycopg2`/SQLAlchemy
   — the schema is portable; the only SQLite-isms are `WAL` (drop) and
   `INTEGER PRIMARY KEY` (already standard SQL).
4. Set `LOOPY_PAY_DSN=postgresql://…` in `.env` on every app EC2.

The ledger's invariant (trial balance nets to zero) is database-agnostic,
and the existing pytest suite passes against either backend.
