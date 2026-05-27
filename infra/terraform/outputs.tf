output "alb_dns" {
  description = "Public DNS of the Application Load Balancer — this is the user-facing URL"
  value       = aws_lb.app.dns_name
}
output "app_url" {
  description = "User-facing URL"
  value       = "http://${aws_lb.app.dns_name}"
}
output "app_instance_ids" {
  description = "EC2 IDs of the app instances behind the ALB"
  value       = aws_instance.app[*].id
}
output "app_public_ips" {
  description = "Public IPs of the app instances (SSH from your operator IP)"
  value       = aws_instance.app[*].public_ip
}
output "obs_public_ip" {
  description = "Public IP of the observability box (Grafana + Prometheus)"
  value       = length(aws_instance.obs) > 0 ? aws_instance.obs[0].public_ip : "disabled"
}
output "grafana_url" {
  description = "Grafana UI — open in a browser from your operator IP"
  value       = length(aws_instance.obs) > 0 ? "http://${aws_instance.obs[0].public_ip}:3000" : "disabled"
}
output "grafana_admin_password" {
  description = "Grafana admin password (login: admin / <this value>)"
  value       = var.grafana_admin_password
  sensitive   = true
}
output "prometheus_url" {
  description = "Prometheus UI — useful for verifying scrape targets are healthy"
  value       = length(aws_instance.obs) > 0 ? "http://${aws_instance.obs[0].public_ip}:9090" : "disabled"
}
output "cloudwatch_dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}
output "raw_bucket"     { value = aws_s3_bucket.raw.bucket }
output "curated_bucket" { value = aws_s3_bucket.curated.bucket }
output "site_bucket"    { value = aws_s3_bucket.site.bucket }
output "glue_database"  { value = aws_glue_catalog_database.loopy.name }
output "glue_job"       { value = aws_glue_job.etl.name }
output "log_group"      { value = aws_cloudwatch_log_group.app.name }
output "ssh_app" {
  description = "SSH into each app instance"
  value       = [for ip in aws_instance.app[*].public_ip : "ssh -i <key>.pem ec2-user@${ip}"]
}
output "ssh_obs" {
  description = "SSH into the observability box"
  value       = length(aws_instance.obs) > 0 ? "ssh -i <key>.pem ec2-user@${aws_instance.obs[0].public_ip}" : "disabled"
}
