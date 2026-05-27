###############################################################################
#  Loopy Cloud — Infrastructure as Code (Terraform / AWS)   v2 — multi-EC2
#
#  Provisions:
#    · Networking      : VPC, IGW, TWO public subnets (across 2 AZs for ALB),
#                        route table.
#    · Security        : ALB SG, app SG, observability SG, IAM least-privilege.
#    · Load balancing  : Application Load Balancer + target group.
#    · Compute (app)   : N × EC2 (default 2) behind the ALB — each runs the
#                        full Docker Compose stack (gateway + payments +
#                        neural + rag).
#    · Compute (obs)   : 1 × EC2 running Prometheus + Grafana, scraping the
#                        app instances' /api/pay/metrics endpoints and
#                        pulling CloudWatch metrics.
#    · Storage         : 3 × S3 buckets (raw events, curated parquet, site).
#    · Data / ETL      : Glue Catalog DB + crawler + PySpark ETL job.
#    · Observability   : CloudWatch log group, CloudWatch dashboard, three
#                        CloudWatch alarms (CPU, ALB 5xx, unhealthy hosts).
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" { region = var.region }

locals {
  name = "${var.project}-${var.env}"
  tags = {
    Project   = var.project
    Env       = var.env
    ManagedBy = "terraform"
    Owner     = "bharat-soni"
  }
  azs = ["${var.region}a", "${var.region}b"]
}

############################  NETWORKING  #####################################
resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${local.name}-vpc" })
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${local.name}-igw" })
}

# Two public subnets in different AZs — required by ALB.
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 1) # 10.20.1.0/24, 10.20.2.0/24
  map_public_ip_on_launch = true
  availability_zone       = local.azs[count.index]
  tags                    = merge(local.tags, { Name = "${local.name}-public-${count.index}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = merge(local.tags, { Name = "${local.name}-rt" })
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

############################  SECURITY GROUPS  ################################
# 1) ALB — open to the internet on 80/443.
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb-sg"
  description = "ALB ingress: HTTP/HTTPS from anywhere"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.tags, { Name = "${local.name}-alb-sg" })
}

# 2) App — only the ALB can hit port 80; only the obs box can scrape;
#    SSH only from the operator's IP. App internal ports (8000/8100/8200) are
#    NEVER exposed externally — they're reachable only on the docker bridge.
resource "aws_security_group" "app" {
  name        = "${local.name}-app-sg"
  description = "Loopy app: HTTP from ALB only, SSH from admin only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP from the ALB only"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    description = "SSH from admin only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }
  ingress {
    description     = "Prometheus scrape from observability box"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.obs.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.tags, { Name = "${local.name}-app-sg" })
}

# 3) Observability — Prometheus & Grafana are reachable only from operator.
resource "aws_security_group" "obs" {
  name        = "${local.name}-obs-sg"
  description = "Observability: Grafana + Prometheus, accessible to operator"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from admin only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }
  ingress {
    description = "Grafana from admin only"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }
  ingress {
    description = "Prometheus UI from admin only"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.tags, { Name = "${local.name}-obs-sg" })
}

############################  S3 BUCKETS  #####################################
resource "aws_s3_bucket" "raw" {
  bucket = "${local.name}-raw-events"
  tags   = merge(local.tags, { Zone = "raw" })
}
resource "aws_s3_bucket" "curated" {
  bucket = "${local.name}-curated"
  tags   = merge(local.tags, { Zone = "curated" })
}
resource "aws_s3_bucket" "site" {
  bucket = "${local.name}-frontend"
  tags   = merge(local.tags, { Zone = "site" })
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_public_access_block" "curated" {
  bucket                  = aws_s3_bucket.curated.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "curated" {
  bucket = aws_s3_bucket.curated.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.curated.id
  key    = "scripts/loopy_etl.py"
  source = "${path.module}/../../glue/loopy_etl.py"
  etag   = filemd5("${path.module}/../../glue/loopy_etl.py")
}

############################  IAM  ###########################################
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# App role: RW on the raw bucket, write logs, optional Bedrock for RAG.
resource "aws_iam_role" "app" {
  name               = "${local.name}-app-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.tags
}
data "aws_iam_policy_document" "app_policy" {
  statement {
    sid       = "RawBucketRW"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:CreateLogGroup"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
  statement {
    sid       = "BedrockOptional"
    actions   = ["bedrock:InvokeModel"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "app" {
  name   = "${local.name}-app-policy"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app_policy.json
}
resource "aws_iam_instance_profile" "app" {
  name = "${local.name}-app-profile"
  role = aws_iam_role.app.name
}

# Observability role: read CloudWatch metrics + logs so Grafana can query them.
resource "aws_iam_role" "obs" {
  name               = "${local.name}-obs-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.tags
}
data "aws_iam_policy_document" "obs_policy" {
  statement {
    sid = "CloudWatchRead"
    actions = [
      "cloudwatch:DescribeAlarmsForMetric", "cloudwatch:DescribeAlarmHistory",
      "cloudwatch:DescribeAlarms", "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
      "logs:DescribeLogGroups", "logs:GetLogGroupFields", "logs:StartQuery",
      "logs:StopQuery", "logs:GetQueryResults", "logs:GetLogEvents",
      "ec2:DescribeTags", "ec2:DescribeInstances", "ec2:DescribeRegions",
      "tag:GetResources",
    ]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "obs" {
  name   = "${local.name}-obs-policy"
  role   = aws_iam_role.obs.id
  policy = data.aws_iam_policy_document.obs_policy.json
}
resource "aws_iam_instance_profile" "obs" {
  name = "${local.name}-obs-profile"
  role = aws_iam_role.obs.name
}

# Glue role.
data "aws_iam_policy_document" "glue_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "glue" {
  name               = "${local.name}-glue-role"
  assume_role_policy = data.aws_iam_policy_document.glue_assume.json
  tags               = local.tags
}
resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}
data "aws_iam_policy_document" "glue_s3" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
    resources = [
      aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*",
      aws_s3_bucket.curated.arn, "${aws_s3_bucket.curated.arn}/*",
    ]
  }
}
resource "aws_iam_role_policy" "glue_s3" {
  name   = "${local.name}-glue-s3"
  role   = aws_iam_role.glue.id
  policy = data.aws_iam_policy_document.glue_s3.json
}

############################  CLOUDWATCH (logs)  ##############################
resource "aws_cloudwatch_log_group" "app" {
  name              = "/loopy/${var.env}/app"
  retention_in_days = 14
  tags              = local.tags
}

############################  ALB  ############################################
resource "aws_lb" "app" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  idle_timeout       = 120 # comfortable for our long WebSocket admin stream
  tags               = local.tags
}

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  tags = local.tags
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

############################  EC2 — APP INSTANCES  ############################
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_instance" "app" {
  count                  = var.app_instance_count
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public[count.index % length(aws_subnet.public)].id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  key_name               = var.key_name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    raw_bucket = aws_s3_bucket.raw.bucket
    region     = var.region
    repo_url   = var.repo_url
  })

  root_block_device {
    volume_size = 20
    encrypted   = true
  }
  tags = merge(local.tags, { Name = "${local.name}-app-${count.index + 1}", Role = "app" })
}

resource "aws_lb_target_group_attachment" "app" {
  count            = var.app_instance_count
  target_group_arn = aws_lb_target_group.app.arn
  target_id        = aws_instance.app[count.index].id
  port             = 80
}

############################  EC2 — OBSERVABILITY  ############################
# Prometheus + Grafana, configured to scrape every app instance's payments
# /metrics endpoint and to pull EC2/ALB metrics from CloudWatch.
resource "aws_instance" "obs" {
  count                  = var.observability_enabled ? 1 : 0
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.obs_instance_type
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.obs.id]
  iam_instance_profile   = aws_iam_instance_profile.obs.name
  key_name               = var.key_name

  user_data = templatefile("${path.module}/user_data_observability.sh.tftpl", {
    repo_url     = var.repo_url
    region       = var.region
    app_targets  = join(",", [for ip in aws_instance.app[*].private_ip : "${ip}:80"])
    grafana_pwd  = var.grafana_admin_password
  })

  root_block_device {
    volume_size = 20
    encrypted   = true
  }
  tags = merge(local.tags, { Name = "${local.name}-obs", Role = "observability" })
}

############################  CLOUDWATCH (dashboard + alarms)  ################
# A single dashboard combining ALB and EC2 metrics for both app instances.
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${local.name}-overview"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric", x = 0, y = 0, width = 12, height = 6,
        properties = {
          title  = "ALB — request count & 5xx",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.app.arn_suffix],
            [".", "HTTPCode_Target_5XX_Count", ".", "."],
            [".", "HTTPCode_Target_4XX_Count", ".", "."],
          ],
          period = 60, stat = "Sum",
        }
      },
      {
        type   = "metric", x = 12, y = 0, width = 12, height = 6,
        properties = {
          title  = "ALB — target response time (avg & p99)",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.app.arn_suffix, { stat = "Average" }],
            ["...", { stat = "p99" }],
          ],
          period = 60,
        }
      },
      {
        type   = "metric", x = 0, y = 6, width = 12, height = 6,
        properties = {
          title  = "EC2 — CPU utilisation (all app instances)",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            for inst in aws_instance.app : ["AWS/EC2", "CPUUtilization", "InstanceId", inst.id]
          ],
          period = 60, stat = "Average",
        }
      },
      {
        type   = "metric", x = 12, y = 6, width = 12, height = 6,
        properties = {
          title  = "ALB — healthy host count",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            ["AWS/ApplicationELB", "HealthyHostCount",   "TargetGroup", aws_lb_target_group.app.arn_suffix, "LoadBalancer", aws_lb.app.arn_suffix],
            [".",                  "UnHealthyHostCount", ".",           ".",                                ".",            "."],
          ],
          period = 60, stat = "Average",
        }
      },
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name}-alb-5xx-high"
  alarm_description   = "Target 5xx errors exceeded threshold over 5 minutes"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = aws_lb.app.arn_suffix }
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy" {
  alarm_name          = "${local.name}-alb-unhealthy-host"
  alarm_description   = "An ALB target has been unhealthy"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Average"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.app.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  tags = local.tags
}

resource "aws_cloudwatch_metric_alarm" "ec2_cpu" {
  count               = var.app_instance_count
  alarm_name          = "${local.name}-app-${count.index + 1}-cpu-high"
  alarm_description   = "App EC2 CPU > 80% for 10 minutes"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  dimensions          = { InstanceId = aws_instance.app[count.index].id }
  tags                = local.tags
}

############################  GLUE ETL (unchanged)  ###########################
resource "aws_glue_catalog_database" "loopy" {
  name = "${replace(local.name, "-", "_")}_db"
}

resource "aws_glue_crawler" "raw" {
  name          = "${local.name}-raw-crawler"
  role          = aws_iam_role.glue.arn
  database_name = aws_glue_catalog_database.loopy.name
  s3_target { path = "s3://${aws_s3_bucket.raw.bucket}/payments/" }
  schedule = "cron(0 * * * ? *)" # hourly
  tags     = local.tags
}

resource "aws_glue_job" "etl" {
  name         = "${local.name}-etl"
  role_arn     = aws_iam_role.glue.arn
  glue_version = "4.0"
  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.curated.bucket}/scripts/loopy_etl.py"
    python_version  = "3"
  }
  default_arguments = {
    "--RAW_BUCKET"     = aws_s3_bucket.raw.bucket
    "--CURATED_BUCKET" = aws_s3_bucket.curated.bucket
    "--job-language"   = "python"
    "--enable-metrics" = "true"
  }
  number_of_workers = 2
  worker_type       = "G.1X"
  tags              = local.tags
}
