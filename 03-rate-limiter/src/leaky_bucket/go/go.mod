// Go module for the leaky bucket limiter and worker (one binary, two modes).
// Direct dependencies are pinned to exact versions. go.sum (checksums of
// every dependency) is generated during the Docker build on the EC2, where
// Go's checksum database verifies each download.
module ratelimiter/leakybucket
// Minimum Go version.
go 1.27
require (
	// AWS SDK core types.
	github.com/aws/aws-sdk-go-v2 v1.47.5
	// Loads AWS credentials (EC2 instance role).
	github.com/aws/aws-sdk-go-v2/config v1.33.12
	// Invokes the backend Lambda (worker).
	github.com/aws/aws-sdk-go-v2/service/lambda v1.113.4
	// Reads rules.yaml from S3.
	github.com/aws/aws-sdk-go-v2/service/s3 v1.114.6
	// Sends jobs to the SQS queues (limiter) and takes them out again (worker).
	github.com/aws/aws-sdk-go-v2/service/sqs v1.52.7
	// Redis client; runs the Lua scripts via EVALSHA.
	github.com/redis/go-redis/v9 v9.23.0
	// Parses rules.yaml.
	gopkg.in/yaml.v3 v3.0.1
)
