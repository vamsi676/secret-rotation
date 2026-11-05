terraform {
  # >= 1.11 for ephemeral resources + write-only arguments (keeps secret values out of state).
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project = "secret-rotation-demo"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
