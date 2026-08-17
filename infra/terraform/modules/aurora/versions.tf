# Provider requirements for the aurora module. No provider configuration or
# backend/cloud block here — child modules inherit the default provider
# configuration from whichever environment root module calls them.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    random = {
      source = "hashicorp/random"
    }
  }
}
