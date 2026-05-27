variable "project" {
  type    = string
  default = "loopy"
}
variable "env" {
  type    = string
  default = "dev"
}
variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai
}
variable "instance_type" {
  type        = string
  description = "EC2 size for the app instances"
  default     = "t3.small"
}
variable "obs_instance_type" {
  type        = string
  description = "EC2 size for the observability box (Prometheus + Grafana)"
  default     = "t3.small"
}
variable "app_instance_count" {
  type        = number
  description = "How many app EC2 instances to run behind the ALB"
  default     = 2
}
variable "observability_enabled" {
  type        = bool
  description = "If true, provision a third EC2 running Prometheus + Grafana"
  default     = true
}
variable "key_name" {
  type        = string
  description = "Existing EC2 key pair name for SSH access"
}
variable "my_ip" {
  type        = string
  description = "Your public IP in CIDR form for SSH/Grafana, e.g. 203.0.113.7/32"
  default     = "0.0.0.0/0"
}
variable "repo_url" {
  type        = string
  description = "Git URL of this repo, cloned by EC2 user-data at boot"
  default     = "https://github.com/bharatsoni/loopy-cloud.git"
}
variable "grafana_admin_password" {
  type        = string
  description = "Grafana admin password (default: 'loopy-admin'; change for anything other than a local demo)"
  default     = "loopy-admin"
  sensitive   = true
}
