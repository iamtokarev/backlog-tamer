output "ecr_repository_url" {
  description = "ECR repository URL for the Lambda container image."
  value       = aws_ecr_repository.app.repository_url
}

output "webhook_function_name" {
  description = "Name of the webhook receiver Lambda, used by the post-deploy smoke test."
  value       = aws_lambda_function.webhook.function_name
}

output "worker_function_name" {
  description = "Name of the SQS worker Lambda, used by the post-deploy smoke test."
  value       = aws_lambda_function.worker.function_name
}

output "image_uri" {
  description = "Container image digest URI Terraform expects for Lambda."
  value       = local.app_image_uri
}

output "image_tag_uri" {
  description = "Mutable container image tag URI used by the build-and-push script."
  value       = local.app_image_tag_uri
}

output "webhook_function_url" {
  description = "Public Lambda Function URL to register as the Telegram webhook."
  value       = aws_lambda_function_url.webhook.function_url
}

output "runtime_secret_arn" {
  description = "Secrets Manager ARN for app runtime JSON secrets."
  value       = aws_secretsmanager_secret.app.arn
}

output "updates_queue_url" {
  description = "SQS queue URL for Telegram updates."
  value       = aws_sqs_queue.updates.url
}

output "updates_dlq_url" {
  description = "SQS DLQ URL for failed Telegram updates."
  value       = aws_sqs_queue.updates_dlq.url
}
