# NVIS Observability — Grafana provisioning via Terraform (datasources + every dashboard).
# Alternative to the compose file-provider: `terraform init && terraform apply`.
# Dashboards are read from ../grafana/dashboards/*.json (the generalized, site-agnostic models).

resource "grafana_folder" "nvis" {
  title = var.folder_title
}

# ---- datasources (UIDs match what the dashboards reference) ----
resource "grafana_data_source" "victoriametrics" {
  type       = "prometheus"
  name       = "nvis-victoriametrics"
  uid        = "nvis-victoriametrics"
  url        = var.victoriametrics_url
  is_default = true
}

resource "grafana_data_source" "prometheus" {
  type = "prometheus"
  name = "nvis-prometheus"
  uid  = "nvis-prometheus"
  url  = var.prometheus_url
}

resource "grafana_data_source" "loki" {
  type = "loki"
  name = "nvis-loki"
  uid  = "nvis-loki"
  url  = var.loki_url
}

# ---- every dashboard in ../grafana/dashboards ----
resource "grafana_dashboard" "nvis" {
  for_each    = fileset("${path.module}/../grafana/dashboards", "*.json")
  folder      = grafana_folder.nvis.id
  overwrite   = true
  config_json = file("${path.module}/../grafana/dashboards/${each.value}")

  depends_on = [
    grafana_data_source.victoriametrics,
    grafana_data_source.prometheus,
    grafana_data_source.loki,
  ]
}
