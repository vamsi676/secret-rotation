# Single public subnet + IGW. No NAT gateway. The instance has a public IP for
# outbound HTTPS to AWS APIs (SSM, Secrets Manager, Logs) but NO inbound rules.

# Pick an AZ that actually offers the instance type (t3.micro isn't in every AZ).
data "aws_ec2_instance_type_offerings" "azs" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

locals {
  az = sort(data.aws_ec2_instance_type_offerings.azs.locations)[0]
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name_prefix}-vpc" }
}

# Lock down the VPC's default SG so nothing accidentally uses an open group.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-default-locked" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = local.az
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name_prefix}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-public-rt" }
}

resource "aws_route" "default_v4" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-app"
  description = "Demo EC2: no inbound, HTTPS outbound only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name_prefix}-app" }
}

resource "aws_vpc_security_group_egress_rule" "https_out" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS to AWS APIs (SSM, Secrets Manager, CloudWatch Logs) and OS repos"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}
