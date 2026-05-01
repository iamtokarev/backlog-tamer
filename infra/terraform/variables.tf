variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Name prefix for AWS resources."
  type        = string
  default     = "backlog-tamer"
}

variable "image_tag" {
  description = "ECR image tag used by both Lambda functions."
  type        = string
  default     = "latest"
}

variable "lambda_architecture" {
  description = "Lambda architecture for the container image."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.lambda_architecture)
    error_message = "lambda_architecture must be arm64 or x86_64."
  }
}

variable "webhook_lambda_memory_mb" {
  description = "Memory size for the webhook receiver Lambda."
  type        = number
  default     = 512
}

variable "worker_lambda_memory_mb" {
  description = "Memory size for the SQS worker Lambda."
  type        = number
  default     = 1024
}

variable "worker_lambda_timeout_seconds" {
  description = "Timeout for the SQS worker Lambda."
  type        = number
  default     = 300
}

variable "worker_maximum_concurrency" {
  description = "Maximum SQS event source concurrency for the worker Lambda. AWS requires at least 2."
  type        = number
  default     = 2

  validation {
    condition     = var.worker_maximum_concurrency >= 2
    error_message = "worker_maximum_concurrency must be at least 2."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for Lambda log groups."
  type        = number
  default     = 14
}
