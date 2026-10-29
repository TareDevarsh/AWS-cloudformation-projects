// Go module for the token bucket limiter.
// Direct dependencies are pinned to exact versions. go.sum (checksums of
// every dependency) is generated during the Docker build on the EC2, where
// Go's checksum database verifies each download.
module ratelimiter/tokenbucket

// Minimum Go version.
go 1.27

require (
	// AWS SDK core types.
	github.com/aws/aws-sdk-go-v2 v1.47.5
	// Loads AWS credentials (EC2 instance role).
	github.com/aws/aws-sdk-go-v2/config v1.33.12
	// Invokes the backend Lambda.
	github.com/aws/aws-sdk-go-v2/service/lambda v1.113.4
	// Reads rules.yaml from S3.
	github.com/aws/aws-sdk-go-v2/service/s3 v1.114.6
	// Redis client; runs token_bucket.lua via EVALSHA.
	github.com/redis/go-redis/v9 v9.23.0
	// Parses rules.yaml.
	gopkg.in/yaml.v3 v3.0.1
)
