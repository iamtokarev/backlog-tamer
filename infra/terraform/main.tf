data "aws_caller_identity" "current" {}

locals {
  name              = var.project_name
  app_image_tag_uri = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  app_image_uri     = "${aws_ecr_repository.app.repository_url}@${data.aws_ecr_image.app.id}"

  common_tags = {
    Project = var.project_name
  }
}

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

data "aws_ecr_image" "app" {
  repository_name = aws_ecr_repository.app.name
  image_tag       = var.image_tag
}

resource "aws_secretsmanager_secret" "app" {
  name        = "${local.name}/runtime"
  description = "Runtime secrets for Backlog Tamer. Populate this JSON secret outside Terraform."

  tags = local.common_tags
}

resource "aws_sqs_queue" "updates_dlq" {
  name                      = "${local.name}-updates-dlq"
  message_retention_seconds = 1209600

  tags = local.common_tags
}

resource "aws_sqs_queue" "updates" {
  name                       = "${local.name}-updates"
  visibility_timeout_seconds = var.worker_lambda_timeout_seconds + 30
  message_retention_seconds  = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.updates_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "webhook" {
  name              = "/aws/lambda/${local.name}-webhook"
  retention_in_days = var.log_retention_days

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = var.log_retention_days

  tags = local.common_tags
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_runtime" {
  statement {
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [aws_secretsmanager_secret.app.arn]
  }

  statement {
    actions = [
      "sqs:SendMessage",
    ]
    resources = [aws_sqs_queue.updates.arn]
  }

  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.updates.arn]
  }
}

resource "aws_iam_policy" "lambda_runtime" {
  name   = "${local.name}-lambda-runtime"
  policy = data.aws_iam_policy_document.lambda_runtime.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_runtime" {
  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.lambda_runtime.arn
}

resource "aws_lambda_function" "webhook" {
  function_name = "${local.name}-webhook"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.app_image_uri
  architectures = [var.lambda_architecture]
  memory_size   = var.webhook_lambda_memory_mb
  timeout       = 15

  image_config {
    command = ["backlog_tamer.integrations.telegram.lambda_handlers.webhook_handler"]
  }

  environment {
    variables = {
      BACKLOG_TAMER_SECRET_ARN   = aws_secretsmanager_secret.app.arn
      TELEGRAM_UPDATES_QUEUE_URL = aws_sqs_queue.updates.url
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.webhook,
    aws_iam_role_policy_attachment.lambda_basic,
    aws_iam_role_policy_attachment.lambda_runtime,
  ]

  tags = local.common_tags
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.app_image_uri
  architectures = [var.lambda_architecture]
  memory_size   = var.worker_lambda_memory_mb
  timeout       = var.worker_lambda_timeout_seconds

  image_config {
    command = ["backlog_tamer.integrations.telegram.lambda_handlers.worker_handler"]
  }

  environment {
    variables = {
      BACKLOG_TAMER_SECRET_ARN   = aws_secretsmanager_secret.app.arn
      TELEGRAM_UPDATES_QUEUE_URL = aws_sqs_queue.updates.url
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.worker,
    aws_iam_role_policy_attachment.lambda_basic,
    aws_iam_role_policy_attachment.lambda_runtime,
  ]

  tags = local.common_tags
}

resource "aws_lambda_function_url" "webhook" {
  function_name      = aws_lambda_function.webhook.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn        = aws_sqs_queue.updates.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.worker_maximum_concurrency
  }
}
