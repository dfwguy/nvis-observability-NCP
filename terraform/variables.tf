variable "grafana_url" {
  type        = string
  description = "Grafana base URL, e.g. http://<obsvm-host>:3000"
  default     = "http://localhost:3000"
}

variable "grafana_auth" {
  type        = string
  sensitive   = true
  description = "Grafana admin auth: 'admin:PASSWORD' or a service-account API token. NEVER commit a real value."
}

variable "folder_title" {
  type    = string
  default = "NVIS Observability"
}

variable "victoriametrics_url" {
  type    = string
  default = "http://victoriametrics:8428"
}

variable "prometheus_url" {
  type    = string
  default = "http://prometheus:9090"
}

variable "loki_url" {
  type    = string
  default = "http://loki:3100"
}
