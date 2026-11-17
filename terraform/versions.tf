terraform {
  # Funciona con Terraform >= 1.5 (y con OpenTofu, que es compatible)
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}
