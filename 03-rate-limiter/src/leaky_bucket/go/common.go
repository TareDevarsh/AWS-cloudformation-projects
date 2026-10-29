// Pieces shared by the Go leaky bucket limiter (limiter.go) and worker (worker.go).
//
// Only plain helpers live here: config from environment variables, reading
// rules.yaml (from S3 or a local file), and a few name builders. Everything
// that talks to a user (HTTP) is in limiter.go; everything that paces jobs is
// in worker.go. Same job as python/common.py.
//
// Config (environment variables):
//
//	REDIS_ADDR         host:port of Redis (default 127.0.0.1:6379)
//	REDIS_PASSWORD     Redis password (empty = none)
//	LANGUAGE           go (part of every queue and Redis key name)
//	ACCEPT_LUA         path to accept.lua
//	FINISH_LUA         path to finish.lua
//	TOKEN_BUCKET_LUA   path to the EXISTING token bucket script (polling limit)
//	RULES_FILE         local rules.yaml (used if RULES_BUCKET is empty)
//	RULES_BUCKET       S3 bucket holding the rules (optional)
//	RULES_KEY          S3 object key (default rules.yaml)
//	RULES_REFRESH_S    seconds between S3 ETag checks (default 30)
//	SQS_PREFIX         queue URL up to the language, e.g.
//	                   https://sqs.<region>.amazonaws.com/<account>/rate-limiter-dev-
//	                   (queue url = SQS_PREFIX + "<language>-t<tier>.fifo")
package main

import (
	"context"     // deadlines/cancellation for Redis and AWS calls
	"errors"      // inspects AWS errors (S3 304)
	"fmt"         // string formatting and error messages
	"io"          // reads the S3 response body
	"log"         // startup and error logs (not per request)
	"net/http"    // status code constant for "not modified"
	"os"          // environment variables and files
	"regexp"      // validates ids
	"strconv"     // number <-> text
	"sync/atomic" // swap the rules safely while requests are reading them
	"time"        // the refresh timer

	"github.com/aws/aws-sdk-go-v2/aws"        // AWS helper types (aws.String)
	"github.com/aws/aws-sdk-go-v2/service/s3" // reads rules.yaml from S3
	"gopkg.in/yaml.v3"                        // parses rules.yaml
)

// --- Config ------------------------------------------------------------------

// env returns an environment variable, or a default if it's not set.
func env(name, def string) string {
	// Look the variable up.
	if v := os.Getenv(name); v != "" {
		// Set: use it.
		return v
	}
	// Not set: use the default.
	return def
}

var (
	// Redis address. In Docker Compose: redis:6379.
	redisAddr = env("REDIS_ADDR", "127.0.0.1:6379")
	// Which language this process is; every queue and Redis key name contains it.
	language = env("LANGUAGE", "go")
	// The two Lua scripts of this algorithm (by default the files one folder up).
	acceptLua = env("ACCEPT_LUA", "../accept.lua")
	finishLua = env("FINISH_LUA", "../finish.lua")
	// The EXISTING token bucket script, reused as-is for the polling limit.
	tokenBucketLua = env("TOKEN_BUCKET_LUA", "../../token_bucket/token_bucket.lua")
	// Local rules file, used when no S3 bucket is set (local testing).
	rulesFile = env("RULES_FILE", "../rules.yaml")
	// S3 bucket with the rules. Empty = use the local file.
	rulesBucket = env("RULES_BUCKET", "")
	// Name of the rules object inside the bucket.
	rulesKey = env("RULES_KEY", "rules.yaml")
	// Queue URL up to (and including) the "-" before the language.
	sqsPrefix = env("SQS_PREFIX", "")

	// Caller ids from headers: safe characters, max 128. Junk is rejected.
	callerIDRe = regexp.MustCompile(`^[A-Za-z0-9_.:]{1,128}$`)
	// Job ids are UUIDs made by the limiter: lowercase hex in 8-4-4-4-12 groups.
	jobIDRe = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
)

const (
	// Seconds a job record lives in Redis (5 minutes), counted again from the end of the job.
	jobTTLS = 300
	// Extra seconds a user's line count lives beyond the SQS retention (retries take a few seconds).
	lineTTLCushionS = 60
)

// --- Rules (kept in memory) ---------------------------------------------------

// QueueCfg is the "queue:" section of rules.yaml.
type QueueCfg struct {
	RetentionS         int // SQS drops a waiting job after this many seconds
	MaxAttempts        int // a job whose backend call failed this many times is marked "failed"
	VisibilityTimeoutS int // a received job stays hidden this long
	RetryVisibilityS   int // after a backend error, hide the job only this long
}

// PollCfg is the "poll:" section: token bucket size and refill rate per bucket.
type PollCfg struct {
	JobSize, JobRate   float64 // bucket per job id
	UserSize, UserRate float64 // bucket per user
}

// Tier is one entry of "tiers:" in rules.yaml, ready to use.
type Tier struct {
	ID         int     // tier number, 0-255
	Name       string  // label for logs and docs only
	LeakRate   float64 // jobs started per second (the worker's pace)
	MaxWaiting int     // jobs one user may have waiting at once
	IntervalMs float64 // 1000 / LeakRate
}

// Config is everything the limiter and worker need from rules.yaml.
type Config struct {
	Queue        QueueCfg       // queue settings
	Poll         PollCfg        // polling limits
	Tiers        []Tier         // in the order of the file
	Seed         map[string]int // TEST ONLY: user id -> tier written into the "tiers" hash
	AgentSleepMs int            // how long the fake chat agent takes
}

// rawRules mirrors the YAML layout before validation. Pointers tell "missing" from 0.
type rawRules struct {
	// The "queue:" section.
	Queue struct {
		RetentionS         *int `yaml:"retention_s"`          // seconds SQS keeps a waiting job
		MaxAttempts        *int `yaml:"max_attempts"`         // tries before "failed"
		VisibilityTimeoutS *int `yaml:"visibility_timeout_s"` // hidden time after receive
		RetryVisibilityS   *int `yaml:"retry_visibility_s"`   // hidden time after a backend error
	} `yaml:"queue"`
	// The "poll:" section.
	Poll struct {
		// Bucket per job id.
		Job struct {
			BucketSize *float64 `yaml:"bucket_size"` // max polls in a burst
			RefillRate *float64 `yaml:"refill_rate"` // polls per second
		} `yaml:"job"`
		// Bucket per user.
		User struct {
			BucketSize *float64 `yaml:"bucket_size"` // max polls in a burst
			RefillRate *float64 `yaml:"refill_rate"` // polls per second
		} `yaml:"user"`
	} `yaml:"poll"`
	// How long the fake agent takes, in ms.
	AgentSleepMs *int `yaml:"agent_sleep_ms"`
	// The list under "tiers:".
	Tiers []struct {
		ID         *int    `yaml:"id"`          // tier number
		Name       *string `yaml:"name"`        // label
		LeakRate   float64 `yaml:"leak_rate"`   // jobs per second
		MaxWaiting int     `yaml:"max_waiting"` // per-user line length
	} `yaml:"tiers"`
	// TEST ONLY: user id -> tier.
	SeedTiers map[string]int `yaml:"seed_tiers"`
}

// orInt returns *p, or def when the value is missing.
func orInt(p *int, def int) int {
	if p == nil {
		return def
	}
	return *p
}

// orFloat returns *p, or def when the value is missing.
func orFloat(p *float64, def float64) float64 {
	if p == nil {
		return def
	}
	return *p
}

// parseRules validates rules.yaml text and returns the settings.
func parseRules(data []byte) (*Config, error) {
	// Decode the YAML into the raw layout (an empty file gives an empty struct).
	var raw rawRules
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, err
	}

	// --- queue settings: whole numbers with the approved defaults ---
	q := QueueCfg{
		RetentionS:         orInt(raw.Queue.RetentionS, 120),
		MaxAttempts:        orInt(raw.Queue.MaxAttempts, 5),
		VisibilityTimeoutS: orInt(raw.Queue.VisibilityTimeoutS, 10),
		RetryVisibilityS:   orInt(raw.Queue.RetryVisibilityS, 1),
	}
	// All of them must be at least 1.
	if min(q.RetentionS, q.MaxAttempts, q.VisibilityTimeoutS, q.RetryVisibilityS) < 1 {
		return nil, errors.New("queue settings must all be >= 1")
	}

	// --- poll limits: defaults per job 3 / 1 per s, per user 20 / 10 per s ---
	p := PollCfg{
		JobSize:  orFloat(raw.Poll.Job.BucketSize, 3),
		JobRate:  orFloat(raw.Poll.Job.RefillRate, 1),
		UserSize: orFloat(raw.Poll.User.BucketSize, 20),
		UserRate: orFloat(raw.Poll.User.RefillRate, 10),
	}
	// Sizes and rates must be positive (the token bucket script checks too).
	if !(p.JobSize > 0 && p.JobRate > 0 && p.UserSize > 0 && p.UserRate > 0) {
		return nil, errors.New("poll bucket_size and refill_rate must be > 0")
	}

	// --- tiers ---
	var tiers []Tier
	// Tier numbers seen so far, to reject a tier listed twice.
	seen := map[int]bool{}
	// One entry per tier in the file.
	for _, t := range raw.Tiers {
		// The tier number is required, 0-255.
		if t.ID == nil || *t.ID < 0 || *t.ID > 255 {
			return nil, errors.New("tier id is required and must be 0-255")
		}
		id := *t.ID
		// Leak rate (jobs/s) and per-user line length.
		if !(t.LeakRate > 0) || t.MaxWaiting < 1 {
			return nil, fmt.Errorf("tier %d: leak_rate must be > 0 and max_waiting >= 1", id)
		}
		// Reject a second entry for the same tier.
		if seen[id] {
			return nil, fmt.Errorf("tier %d is listed twice", id)
		}
		seen[id] = true
		// The label defaults to the tier number.
		name := strconv.Itoa(id)
		if t.Name != nil {
			name = *t.Name
		}
		// Flat, ready-to-use version of the tier.
		tiers = append(tiers, Tier{ID: id, Name: name, LeakRate: t.LeakRate, MaxWaiting: t.MaxWaiting, IntervalMs: 1000 / t.LeakRate})
	}
	// Tier 0 is the fallback for unknown users.
	if !seen[0] {
		return nil, errors.New("tier 0 must be configured")
	}

	// --- test-only seed users ---
	seed := map[string]int{}
	// user id -> tier number.
	for user, tid := range raw.SeedTiers {
		// Same safe-id rule as the X-User-Id header.
		if !callerIDRe.MatchString(user) {
			return nil, fmt.Errorf("seed_tiers: bad user id %q", user)
		}
		// Only tiers that exist.
		if !seen[tid] {
			return nil, fmt.Errorf("seed_tiers: %s uses unknown tier %d", user, tid)
		}
		seed[user] = tid
	}

	// Everything the limiter and worker need.
	return &Config{Queue: q, Poll: p, Tiers: tiers, Seed: seed, AgentSleepMs: orInt(raw.AgentSleepMs, 200)}, nil
}

// rulesHolder keeps the current rules; atomic so refreshes never block requests.
type rulesHolder struct {
	current atomic.Pointer[Config] // the live rules
	etag    string                 // S3 version of the rules we hold
	s3      *s3.Client             // nil when using the local file
}

// get returns the current rules (lock-free read).
func (h *rulesHolder) get() *Config {
	return h.current.Load()
}

// loadInitial reads the rules once at startup (S3 if configured, else the local file).
func (h *rulesHolder) loadInitial(ctx context.Context) error {
	if h.s3 != nil {
		// From S3 (deployed).
		if _, err := h.fetchS3(ctx); err != nil {
			return fmt.Errorf("load rules from s3: %w", err)
		}
	} else {
		// From the local file (testing).
		data, err := os.ReadFile(rulesFile)
		if err != nil {
			return fmt.Errorf("read rules: %w", err)
		}
		cfg, err := parseRules(data)
		if err != nil {
			return fmt.Errorf("parse rules: %w", err)
		}
		h.current.Store(cfg)
	}
	// Confirm in the log how many tiers we have.
	log.Printf("loaded rules: %d tiers", len(h.get().Tiers))
	return nil
}

// fetchS3 reads the rules only if they changed (S3 returns 304 when the ETag matches).
func (h *rulesHolder) fetchS3(ctx context.Context) (bool, error) {
	// Which object to read.
	in := &s3.GetObjectInput{Bucket: aws.String(rulesBucket), Key: aws.String(rulesKey)}
	// Already have a version: ask for the file only if it's different.
	if h.etag != "" {
		in.IfNoneMatch = aws.String(h.etag)
	}
	// Read the object.
	obj, err := h.s3.GetObject(ctx, in)
	if err != nil {
		// AWS errors carry the HTTP status; 304 = unchanged.
		var status interface{ HTTPStatusCode() int }
		if errors.As(err, &status) && status.HTTPStatusCode() == http.StatusNotModified {
			return false, nil
		}
		// Any other error goes to the caller.
		return false, err
	}
	// Close the body when done.
	defer obj.Body.Close()
	// Read the whole file.
	data, err := io.ReadAll(obj.Body)
	if err != nil {
		return false, err
	}
	// Validate it.
	cfg, err := parseRules(data)
	if err != nil {
		return false, err
	}
	// Swap in the new rules in one step.
	h.current.Store(cfg)
	// Remember this version for the next check.
	h.etag = aws.ToString(obj.ETag)
	// Rules changed.
	return true, nil
}

// refreshForever checks S3 on a timer. Keeps the old rules on any error.
// onChange (optional) runs after a change, e.g. to re-apply the seed users.
func (h *rulesHolder) refreshForever(every time.Duration, onChange func()) {
	// Tick every N seconds.
	for range time.Tick(every) {
		// Each check gets 10 s at most.
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		changed, err := h.fetchS3(ctx)
		cancel()
		// Log the outcome; on error the previous rules stay in place.
		if err != nil {
			log.Printf("rules refresh failed; keeping previous rules: %v", err)
		} else if changed {
			log.Printf("rules changed: %d tiers", len(h.get().Tiers))
			// Let the caller react.
			if onChange != nil {
				onChange()
			}
		}
	}
}

// --- Small helpers ------------------------------------------------------------

// jobKey is the Redis key of a job record, e.g. job-6f1c.... Same name as in accept.lua.
func jobKey(jobID string) string {
	return "job-" + jobID
}

// queueURL is the SQS FIFO queue of this language and tier, e.g. <prefix>go-t1.fifo.
// One queue per language and tier; the prefix carries region, account and stage.
func queueURL(tier int) string {
	return sqsPrefix + language + "-t" + strconv.Itoa(tier) + ".fifo"
}

// readFile returns a small file's text (the Lua scripts); the caller decides how to fail.
func readFile(path string) (string, error) {
	// The whole file as bytes, then as text.
	b, err := os.ReadFile(path)
	return string(b), err
}
