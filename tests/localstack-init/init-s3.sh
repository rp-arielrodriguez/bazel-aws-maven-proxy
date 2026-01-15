#!/bin/bash
# Initialize LocalStack S3 for testing

set -e

echo "Initializing LocalStack S3 buckets..."

# Create test bucket
awslocal s3 mb s3://test-maven-bucket || true

# Upload test Maven artifacts
mkdir -p /tmp/maven-test
echo "test jar content" > /tmp/maven-test/test-artifact-1.0.0.jar
echo "test pom content" > /tmp/maven-test/test-artifact-1.0.0.pom

awslocal s3 cp /tmp/maven-test/test-artifact-1.0.0.jar s3://test-maven-bucket/com/example/test-artifact/1.0.0/test-artifact-1.0.0.jar
awslocal s3 cp /tmp/maven-test/test-artifact-1.0.0.pom s3://test-maven-bucket/com/example/test-artifact/1.0.0/test-artifact-1.0.0.pom

echo "LocalStack S3 initialization complete"
awslocal s3 ls s3://test-maven-bucket --recursive
