#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-latest}"
PLATFORM="${PLATFORM:-linux/arm64}"
TF_DIR="${TF_DIR:-infra/terraform}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command aws
require_command docker
require_command terraform

ECR_REPO="$(terraform -chdir="$TF_DIR" output -raw ecr_repository_url)"
AWS_REGION="${AWS_REGION:-}"

if [[ -z "$AWS_REGION" ]]; then
  if [[ "$ECR_REPO" =~ ^[0-9]+\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/ ]]; then
    AWS_REGION="${BASH_REMATCH[1]}"
  else
    echo "Could not derive AWS region from ECR repository URL: $ECR_REPO" >&2
    echo "Set AWS_REGION explicitly and rerun." >&2
    exit 1
  fi
fi

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="$ECR_REPO:$IMAGE_TAG"

echo "Logging Docker into ECR for account $AWS_ACCOUNT_ID in $AWS_REGION"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login \
    --username AWS \
    --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

echo "Building and pushing $IMAGE_URI for $PLATFORM"
docker buildx build \
  --platform "$PLATFORM" \
  --tag "$IMAGE_URI" \
  --push \
  .

echo "Pushed $IMAGE_URI"
