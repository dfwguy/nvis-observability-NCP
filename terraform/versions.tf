terraform {
  required_version = ">= 1.3.0"
  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = ">= 3.0.0, < 4.0.0"
    }
  }
}

provider "grafana" {
  url  = var.grafana_url
  auth = var.grafana_auth
}
