# VPC design, Step 11 Part B2:
#
# - PUBLIC subnets host the Fargate tasks (assigned public IPs directly
#   on the ENI) and the ALB. Fargate needs outbound internet access
#   (Groq's API, Bedrock, pulling the image from ECR, hitting Hugging
#   Face if the baked model cache were ever missing) - a public subnet +
#   public IP gets that via the Internet Gateway directly, with
#   NO NAT GATEWAY. A NAT Gateway is ~$33/month (hourly charge, running
#   24/7) plus per-GB data processing, for a benefit (hiding the task's
#   IP / avoiding a public IP) that doesn't matter at this project's
#   scale - the task speaks outbound HTTPS to a handful of known APIs,
#   nothing sensitive is exposed by having a public IP on the ENI (the
#   task itself is unreachable from the internet except through the
#   ALB's security group, since the task's OWN security group only
#   accepts inbound from the ALB - see the security groups below).
# - PRIVATE subnets host RDS only. "Private" here just means no route to
#   the Internet Gateway - it does NOT require a NAT Gateway, because RDS
#   never needs to initiate outbound internet traffic at all, only
#   accept inbound connections from within the VPC (the ECS task's
#   security group). This is what makes "public subnets only for
#   Fargate" and "RDS in a private subnet" simultaneously true without a
#   NAT Gateway anywhere in this design.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-igw" }
}

resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name_prefix}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + var.az_count)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${local.name_prefix}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name_prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# No routes at all beyond the VPC-local default - deliberately not
# associated with the Internet Gateway or any NAT device. RDS in these
# subnets can reach (and be reached by) other resources inside the VPC,
# and nothing else, which is exactly the access RDS needs.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Security groups ---

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "ALB: public HTTP in, forward to the ECS task only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from anywhere - TLS is a documented follow-up, see docs/DEPLOYMENT.md."
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-alb-sg" }
}

resource "aws_security_group" "ecs_task" {
  name        = "${local.name_prefix}-ecs-task"
  description = "ECS task: inbound only from the ALB; outbound anywhere (Groq/Bedrock/ECR/Postgres)."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port from the ALB only"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-ecs-task-sg" }
}

resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-rds"
  description = "RDS: inbound Postgres only from the ECS task security group. No outbound rule needed/added."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from the ECS task only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_task.id]
  }

  tags = { Name = "${local.name_prefix}-rds-sg" }
}
