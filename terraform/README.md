# Terraform — Grafana provisioning

Provisions the NVIS datasources (VictoriaMetrics default, Prometheus, Loki) and **every dashboard**
in [`../grafana/dashboards/`](../grafana/dashboards) into a single Grafana folder, using the
[`grafana/grafana`](https://registry.terraform.io/providers/grafana/grafana/latest) provider.

This is an **alternative** to the docker-compose file-provider — use whichever fits your workflow.

## Usage
```bash
cp terraform.tfvars.example terraform.tfvars   # then edit (gitignored)
terraform init
terraform plan
terraform apply
```
`terraform apply` uploads all dashboards; re-running is idempotent (`overwrite = true`). Adding a new
dashboard JSON to `../grafana/dashboards/` and re-applying picks it up automatically (`for_each`).

## Notes
- Datasource UIDs (`nvis-victoriametrics`, `nvis-prometheus`, `nvis-loki`) match what the dashboards
  reference — don't rename them.
- Dashboards are pre-generalized: they use a `$cluster` template variable (no hardcoded cluster), so
  they work against any cluster label. Pick your cluster from the dashboard's Cluster dropdown.
- Never commit `terraform.tfvars` or `*.tfstate` (both gitignored).
