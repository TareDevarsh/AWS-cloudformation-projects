// Fixed window counter rate limiter server, Go version.
//
// Same behaviour as python/app.py:
//  1. nginx sends /go/<path> here.
//  2. Work out who the caller is: user id, session cookie, IP.
//  3. Pick the matching rules (kept in memory) and build one Redis key each.
//  4. One Redis call runs fixed_window_counter.lua on all keys (all-or-nothing).
//  5. Allowed -> invoke the backend Lambda. Denied -> 429.
//
// Every response carries a Server-Timing header with per-step durations (ms).
//
// Config (environment variables): REDIS_ADDR (host:port), REDIS_PASSWORD, LUA_PATH,
// RULES_FILE, RULES_BUCKET, RULES_KEY, RULES_REFRESH_S, BACKEND_FUNCTION,
// PATH_PREFIX (default /go), HOST, PORT (default 127.0.0.1:8000).
package main

import (
	"context"         // deadlines/cancellation for Redis and AWS calls
	"encoding/binary" // turns 4 IPv4 bytes into one number
	"encoding/json"   // builds JSON bodies
	"errors"          // inspects AWS errors (S3 304)
	"fmt"             // string formatting
	"io"              // reads S3 / Lambda response bodies
	"log"             // startup and error logs (not per request)
	"math"            // Ceil for Retry-After, Trunc for whole-number limits
	"net"             // parses IP addresses
	"net/http"        // the HTTP server
	"os"              // environment variables and files
	"path"            // wildcard matching for rule paths
	"regexp"          // validates ids
	"strconv"         // number <-> text
	"strings"         // prefix checks, joins
	"sync/atomic"     // swap the rules safely while requests are reading them
	"time"            // timings and the refresh timer

	"github.com/aws/aws-sdk-go-v2/aws"            // AWS helper types (aws.String)
	"github.com/aws/aws-sdk-go-v2/config"         // loads credentials (instance role on EC2)
	"github.com/aws/aws-sdk-go-v2/service/lambda" // invokes the backend Lambda
	"github.com/aws/aws-sdk-go-v2/service/s3"     // reads rules.yaml from S3
	"github.com/redis/go-redis/v9"                // Redis client
	"gopkg.in/yaml.v3"                            // parses rules.yaml
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
	// The shared Lua algorithm.
	luaPath = env("LUA_PATH", "../fixed_window_counter.lua")
	// Local rules file, used when no S3 bucket is set.
	rulesFile = env("RULES_FILE", "../rules.yaml")
	// S3 bucket with the rules. Empty = use the local file.
	rulesBucket = env("RULES_BUCKET", "")
	// Name of the rules object in the bucket.
	rulesKey = env("RULES_KEY", "rules.yaml")
	// Backend Lambda name. Empty = echo the request back.
	backendFunction = env("BACKEND_FUNCTION", "")
	// URL prefix nginx uses for this language; stripped before matching.
	pathPrefix = strings.TrimRight(env("PATH_PREFIX", "/go"), "/")

	// Rule ids: lowercase letters and digits only, so "<id>-u<caller>" has one meaning.
	ruleIDRe = regexp.MustCompile(`^[a-z0-9]+$`)
	// Caller ids from headers/cookies: safe characters, max 128. Junk is ignored.
	callerIDRe = regexp.MustCompile(`^[A-Za-z0-9_.:]{1,128}$`)
)

// --- Rules (kept in memory) ---------------------------------------------------

// Rule is one validated entry from rules.yaml.
type Rule struct {
	ID     string  // short id, e.g. "user"; first part of the Redis key
	Key    string  // identity type: user_id, cookie or ip
	Path   string  // path pattern: "*", "/x", "/x/*"
	Limit  int64   // requests allowed per window
	Window float64 // window_s: window length in seconds
}

// rawRules mirrors the YAML layout before validation.
type rawRules struct {
	// The list under "rules:".
	Rules []struct {
		ID    string `yaml:"id"` // rule id
		Match struct {
			Key  string `yaml:"key"`  // identity type
			Path string `yaml:"path"` // path pattern
		} `yaml:"match"`
		Limit   float64 `yaml:"limit"`    // requests per window
		WindowS float64 `yaml:"window_s"` // window length in seconds
	} `yaml:"rules"`
}

// parseRules validates rules.yaml text and returns the rules.
func parseRules(data []byte) ([]Rule, error) {
	// Decode the YAML into the raw layout.
	var raw rawRules
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	// Collected, validated rules.
	out := make([]Rule, 0, len(raw.Rules))
	// Check each rule.
	for _, r := range raw.Rules {
		// Reject ids that would make Redis keys ambiguous.
		if !ruleIDRe.MatchString(r.ID) {
			return nil, fmt.Errorf("rule id must be [a-z0-9]+: %q", r.ID)
		}
		// Reject unknown identity types.
		if r.Match.Key != "user_id" && r.Match.Key != "cookie" && r.Match.Key != "ip" {
			return nil, fmt.Errorf("rule %s: match.key must be user_id, cookie or ip", r.ID)
		}
		// Limit: whole number >= 1; window: > 0 (the Lua script checks this too).
		if r.Limit < 1 || r.Limit != math.Trunc(r.Limit) || r.WindowS <= 0 {
			return nil, fmt.Errorf("rule %s: limit must be a whole number > 0 and window_s > 0", r.ID)
		}
		// Missing path means every path.
		p := r.Match.Path
		if p == "" {
			p = "*"
		}
		// Store the flat, ready-to-use rule.
		out = append(out, Rule{ID: r.ID, Key: r.Match.Key, Path: p, Limit: int64(r.Limit), Window: r.WindowS})
	}
	// All rules valid.
	return out, nil
}

// pathMatches reports whether a request path matches a rule pattern.
func pathMatches(pattern, p string) bool {
	// "*" matches every path.
	if pattern == "*" {
		return true
	}
	// "/x/*" matches /x and everything under it.
	if strings.HasSuffix(pattern, "/*") {
		base := strings.TrimSuffix(pattern, "/*")
		return p == base || strings.HasPrefix(p, base+"/")
	}
	// Patterns with ? or [ use wildcard matching.
	if strings.ContainsAny(pattern, "?[") {
		ok, _ := path.Match(pattern, p)
		return ok
	}
	// Anything else must match exactly.
	return p == pattern
}

// rulesHolder keeps the current rules; atomic so refreshes never block requests.
type rulesHolder struct {
	current atomic.Pointer[[]Rule] // the live rule list
	etag    string                 // S3 version of the rules we hold
	s3      *s3.Client             // nil when using the local file
}

// get returns the current rules (lock-free read).
func (h *rulesHolder) get() []Rule {
	return *h.current.Load()
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
	rules, err := parseRules(data)
	if err != nil {
		return false, err
	}
	// Swap in the new rules in one step.
	h.current.Store(&rules)
	// Remember this version for the next check.
	h.etag = aws.ToString(obj.ETag)
	// Rules changed.
	return true, nil
}

// refreshForever checks S3 on a timer. Keeps the old rules on any error.
func (h *rulesHolder) refreshForever(every time.Duration) {
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
			log.Printf("rules changed: %d rules", len(h.get()))
		}
	}
}

// --- Caller identity ----------------------------------------------------------

// ids holds the identities found on a request; empty string = not present.
type ids struct {
	user   string // from X-User-Id (test only: callers can fake it)
	cookie string // from the "session" cookie, only when there is no user id
	ip     string // IPv4 as a number, IPv6 as text; always set
}

// callerIDs works out who is calling.
func callerIDs(r *http.Request) ids {
	// Collected ids.
	var out ids
	// User id from the header, kept only if it looks safe.
	if u := r.Header.Get("X-User-Id"); callerIDRe.MatchString(u) {
		out.user = u
	}
	// Cookie layer only applies when there's no user id.
	if out.user == "" {
		// The "session" cookie, kept only if it looks safe.
		if c, err := r.Cookie("session"); err == nil && callerIDRe.MatchString(c.Value) {
			out.cookie = c.Value
		}
	}
	// nginx sets X-Real-IP from the real TCP peer. This container publishes no
	// port, so only nginx can reach it and the header can be trusted here.
	ipText := r.Header.Get("X-Real-IP")
	// Fallback: the direct peer address (local testing without nginx).
	if ipText == "" {
		ipText, _, _ = net.SplitHostPort(r.RemoteAddr)
	}
	// Parse it.
	ip := net.ParseIP(strings.TrimSpace(ipText))
	switch {
	case ip == nil:
		// Not a valid IP: all such requests share one "unknown" key.
		out.ip = "unknown"
	case ip.To4() != nil:
		// IPv4 -> number (203.0.113.7 -> 3405803783).
		out.ip = strconv.FormatUint(uint64(binary.BigEndian.Uint32(ip.To4())), 10)
	default:
		// IPv6 -> text with "_" instead of ":".
		out.ip = strings.ReplaceAll(ip.String(), ":", "_")
	}
	// Every identity we found.
	return out
}

// keysFor turns the matching rules into Redis keys and Lua args.
func keysFor(rules []Rule, p string, id ids) (keys []string, args []interface{}, names []string) {
	// Check every rule.
	for _, r := range rules {
		// The caller id this rule needs.
		var caller string
		switch r.Key {
		case "user_id":
			caller = id.user
		case "cookie":
			caller = id.cookie
		case "ip":
			caller = id.ip
		}
		// Skip if we don't have that id, or the path doesn't match.
		if caller == "" || !pathMatches(r.Path, p) {
			continue
		}
		// Key "<rule>-u<caller>", e.g. "user-u42".
		keys = append(keys, r.ID+"-u"+caller)
		// Limit and window for this key, in the order the Lua expects.
		args = append(args, r.Limit, r.Window)
		// Remember the rule id at the same position.
		names = append(names, r.ID)
	}
	// Everything the Redis call needs.
	return keys, args, names
}

// --- Server -------------------------------------------------------------------

// server holds the long-lived clients shared by all requests.
type server struct {
	rdb    *redis.Client  // Redis connection pool
	script *redis.Script  // fixed_window_counter.lua (EVALSHA, reloads if Redis lost it)
	rules  *rulesHolder   // in-memory rules
	lambda *lambda.Client // nil = echo backend
}

// timing collects named durations for the Server-Timing header.
type timing []string

// add records one step's duration in ms.
func (t *timing) add(name string, d time.Duration) {
	*t = append(*t, fmt.Sprintf("%s;dur=%.3f", name, float64(d.Nanoseconds())/1e6))
}

// ServeHTTP handles every request.
func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Start the clock.
	t0 := time.Now()
	// Strip the language prefix: "/go/orders" -> "/orders".
	p := strings.TrimPrefix(r.URL.Path, pathPrefix)
	if p == "" {
		p = "/"
	}
	// All responses are JSON.
	w.Header().Set("Content-Type", "application/json")

	// Health check: no rate limiting, no backend.
	if p == "/healthz" {
		w.Write([]byte(`{"ok":true}`))
		return
	}

	// 1. Rules + identity (memory only).
	keys, args, names := keysFor(s.rules.get(), p, callerIDs(r))
	t1 := time.Now()

	// 2. One Redis round trip for every key.
	// Defaults if no rule matched: allowed, unknown remaining, no wait.
	allowed, remaining, retryMs, deniedBy := int64(1), int64(-1), int64(0), int64(0)
	if len(keys) > 0 {
		// Runs fixed_window_counter.lua on all keys.
		res, err := s.script.Run(r.Context(), s.rdb, keys, args...).Int64Slice()
		if err != nil {
			// Redis down or script error: fail with 503 (fail-open/closed is parked).
			log.Printf("redis error: %v", err)
			http.Error(w, `{"error":"rate limiter unavailable"}`, http.StatusServiceUnavailable)
			return
		}
		// The 4 integers the script returns.
		allowed, remaining, retryMs, deniedBy = res[0], res[1], res[2], res[3]
	}
	t2 := time.Now()

	// Durations so far.
	var tm timing
	tm.add("rules", t1.Sub(t0))
	tm.add("redis", t2.Sub(t1))
	// Requests left in the fullest window.
	w.Header().Set("X-RateLimit-Remaining", strconv.FormatInt(remaining, 10))

	// Denied: answer 429 without calling the backend.
	if allowed == 0 {
		// Which rule said no (deniedBy is 1-based; 0 = none).
		rule := ""
		if deniedBy > 0 {
			rule = names[deniedBy-1]
		}
		w.Header().Set("X-RateLimit-Rule", rule)
		// Retry-After in whole seconds, rounded up, at least 1.
		w.Header().Set("Retry-After", strconv.Itoa(int(math.Max(1, math.Ceil(float64(retryMs)/1000)))))
		// Total time and the header.
		tm.add("total", time.Since(t0))
		w.Header().Set("Server-Timing", strings.Join(tm, ", "))
		// Send the 429 with a JSON explanation.
		w.WriteHeader(http.StatusTooManyRequests)
		json.NewEncoder(w).Encode(map[string]interface{}{"error": "rate limited", "rule": rule, "retry_after_ms": retryMs})
		return
	}

	// 3. Allowed: call the backend.
	status, body := s.callBackend(r.Context(), p, r.Method)
	t3 := time.Now()
	tm.add("backend", t3.Sub(t2))
	tm.add("total", t3.Sub(t0))
	w.Header().Set("Server-Timing", strings.Join(tm, ", "))
	// Send the backend's answer.
	w.WriteHeader(status)
	w.Write(body)
}

// callBackend invokes the Lambda, or echoes locally when none is configured.
func (s *server) callBackend(ctx context.Context, p, method string) (int, []byte) {
	// Request details sent to the backend.
	payload, _ := json.Marshal(map[string]string{"path": p, "method": method})
	// No Lambda: echo (local testing).
	if s.lambda == nil {
		return http.StatusOK, []byte(`{"echo":` + string(payload) + `}`)
	}
	// Call the Lambda and wait for its answer.
	out, err := s.lambda.Invoke(ctx, &lambda.InvokeInput{FunctionName: aws.String(backendFunction), Payload: payload})
	if err != nil {
		// Couldn't reach the backend.
		log.Printf("backend error: %v", err)
		return http.StatusBadGateway, []byte(`{"error":"backend unavailable"}`)
	}
	// FunctionError is set if the Lambda crashed.
	if out.FunctionError != nil {
		return http.StatusBadGateway, out.Payload
	}
	// Normal answer.
	return http.StatusOK, out.Payload
}

func main() {
	// Context for startup calls.
	ctx := context.Background()

	// Load the shared Lua algorithm.
	lua, err := os.ReadFile(luaPath)
	if err != nil {
		log.Fatalf("read lua: %v", err)
	}

	// Long-lived clients.
	s := &server{
		// Connection pool, reused by all requests. Password from REDIS_PASSWORD (empty = none).
		rdb:    redis.NewClient(&redis.Options{Addr: redisAddr, Password: os.Getenv("REDIS_PASSWORD")}),
		script: redis.NewScript(string(lua)), // runs via EVALSHA
		rules:  &rulesHolder{},
	}

	// AWS clients only when something on AWS is configured.
	if rulesBucket != "" || backendFunction != "" {
		// Loads credentials: the EC2 instance role when deployed.
		cfg, err := config.LoadDefaultConfig(ctx)
		if err != nil {
			log.Fatalf("aws config: %v", err)
		}
		if rulesBucket != "" {
			s.rules.s3 = s3.NewFromConfig(cfg)
		}
		if backendFunction != "" {
			s.lambda = lambda.NewFromConfig(cfg)
		}
	}

	// Load rules before taking traffic.
	if s.rules.s3 != nil {
		// From S3.
		if _, err := s.rules.fetchS3(ctx); err != nil {
			log.Fatalf("load rules from s3: %v", err)
		}
		// Keep checking for changes in the background.
		every, _ := strconv.ParseFloat(env("RULES_REFRESH_S", "30"), 64)
		go s.rules.refreshForever(time.Duration(every * float64(time.Second)))
	} else {
		// From the local file.
		data, err := os.ReadFile(rulesFile)
		if err != nil {
			log.Fatalf("read rules: %v", err)
		}
		rules, err := parseRules(data)
		if err != nil {
			log.Fatalf("parse rules: %v", err)
		}
		s.rules.current.Store(&rules)
	}
	log.Printf("loaded %d rules", len(s.rules.get()))

	// Default is loopback. The Dockerfile sets HOST=0.0.0.0 because inside the
	// container only the Docker network can reach it (no port is published).
	addr := net.JoinHostPort(env("HOST", "127.0.0.1"), env("PORT", "8000"))
	log.Printf("listening on %s", addr)
	// Timeouts stop slow clients from holding connections forever.
	srv := &http.Server{Addr: addr, Handler: s, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
