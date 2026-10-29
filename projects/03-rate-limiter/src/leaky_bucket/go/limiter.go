// Leaky bucket (queue) limiter server, Go version. Same behaviour as python/app.py.
//
// This server is the "door": it decides who may join the line and answers polls.
// It does NOT control the speed; the worker (worker.go) starts jobs at a fixed
// rate. Example domain: a chat agent where every message waits for its turn.
//
// Routes (nginx sends /go/<path> here; the prefix is stripped):
//
//	POST /chat              header X-User-Id, optional JSON {"message": "..."}
//	                        202 {job_id, position, tier}  or  429 + Retry-After
//	GET  /result/<job_id>   header X-User-Id (must be the job's owner, else 404)
//	                        200 {status: queued|processing|done|failed|expired,
//	                             position, answer?, queue_wait_ms?, processing_ms?}
//	                        429 + Retry-After if the client polls too fast
//	GET  /healthz           200, no checks
//
// Request flow of POST /chat:
//  1. Validate the user id and the body.
//  2. One Redis call runs accept.lua: tier, room in the user's line?, ticket.
//  3. Full -> 429. Room -> send the job to the SQS FIFO queue of the tier -> 202.
//
// Every response (except /healthz) carries a Server-Timing header, so the laptop
// can see where the time went:  Server-Timing: redis;dur=0.42, sqs;dur=14.2, total;dur=15.0
//
// Extra config: PATH_PREFIX (default /go), HOST / PORT (default 127.0.0.1:8000).
package main

import (
	"context"       // deadlines/cancellation for Redis and AWS calls
	"crypto/rand"   // random bytes for job ids
	"encoding/json" // builds and reads JSON bodies
	"errors"        // inspects errors (body too large, missing key)
	"fmt"           // string formatting
	"io"            // reads the request body
	"log"           // startup and error logs (not per request)
	"math"          // Ceil and Round
	"net"           // joins host and port
	"net/http"      // the HTTP server
	"os"            // environment variables
	"os/signal"     // stop cleanly on SIGTERM (docker stop)
	"strconv"       // number <-> text
	"strings"       // prefix checks, joins
	"syscall"       // the SIGTERM signal number
	"time"          // timings and timeouts
	"unicode/utf8"  // counts characters, checks UTF-8

	"github.com/aws/aws-sdk-go-v2/aws"         // AWS helper types (aws.String)
	"github.com/aws/aws-sdk-go-v2/config"      // loads credentials (instance role on EC2)
	"github.com/aws/aws-sdk-go-v2/service/s3"  // reads rules.yaml from S3
	"github.com/aws/aws-sdk-go-v2/service/sqs" // sends jobs to the line
	"github.com/redis/go-redis/v9"             // Redis client
)

// --- Config ------------------------------------------------------------------

var (
	// URL prefix nginx uses for this language; stripped before routing.
	pathPrefix = strings.TrimRight(env("PATH_PREFIX", "/go"), "/")
)

const (
	// Longest chat message we accept, in characters.
	maxMessageChars = 500
	// Largest request body we read, in bytes (a 500 character message fits many times over).
	maxBodyBytes = 4096
	// Every job of a queue uses the same group id, so SQS keeps strict first-in, first-out order.
	groupID = "q"
)

// --- JSON bodies (struct field order = key order in the JSON) -------------------

// errorBody is {"error": "..."}.
type errorBody struct {
	Error string `json:"error"` // what went wrong
}

// acceptedBody is the 202 answer of POST /chat.
type acceptedBody struct {
	JobID    string `json:"job_id"`   // poll GET /result/<job_id> with this
	Position int64  `json:"position"` // place in line, 1 = next up
	Tier     int64  `json:"tier"`     // the tier that was applied
}

// fullBody is the 429 answer of POST /chat.
type fullBody struct {
	Error        string `json:"error"`          // always "line full"
	Tier         int64  `json:"tier"`           // the tier that was applied
	RetryAfterMs int64  `json:"retry_after_ms"` // soonest a place can free up
}

// pollBody is the 429 answer of GET /result.
type pollBody struct {
	Error        string `json:"error"`          // always "polling too fast"
	RetryAfterMs int64  `json:"retry_after_ms"` // how long to wait
}

// resultBody is the 200 answer of GET /result. Pointers + omitempty leave out what does not apply.
type resultBody struct {
	Status       string  `json:"status"`                  // queued | processing | done | failed | expired
	Tier         int64   `json:"tier"`                    // the job's tier
	Position     int64   `json:"position"`                // place in line (0 once ended)
	QueueWaitMs  *int64  `json:"queue_wait_ms,omitempty"` // only when ended
	ProcessingMs *int64  `json:"processing_ms,omitempty"` // only when ended
	Answer       *string `json:"answer,omitempty"`        // only when done
	Error        *string `json:"error,omitempty"`         // only when failed or expired
}

// queueMessage is what goes into the SQS message body (the worker reads it back).
type queueMessage struct {
	Job  string `json:"job"`  // job id
	Msg  string `json:"msg"`  // chat text
	Fail bool   `json:"fail"` // test hook: make the backend raise
}

// --- Server -------------------------------------------------------------------

// limiter holds the long-lived clients shared by all requests.
type limiter struct {
	rdb    *redis.Client // Redis connection pool
	accept *redis.Script // accept.lua (EVALSHA, reloads if Redis lost it)
	finish *redis.Script // finish.lua (gives a place back on errors / expiry)
	poll   *redis.Script // the existing token_bucket.lua, used for the polling limit
	sqs    *sqs.Client   // sends jobs to the line
	rules  *rulesHolder  // in-memory rules
}

// timing collects named durations for the Server-Timing header.
type timing []string

// add records one step's duration in ms.
func (t *timing) add(name string, d time.Duration) {
	*t = append(*t, fmt.Sprintf("%s;dur=%.3f", name, float64(d.Nanoseconds())/1e6))
}

// header renders the collected durations as one header value.
func (t timing) header() string {
	return strings.Join(t, ", ")
}

// respond writes a JSON answer with a status code and optional extra headers (name, value pairs).
func respond(w http.ResponseWriter, status int, body []byte, extra ...[2]string) {
	// All responses are JSON.
	w.Header().Set("Content-Type", "application/json")
	// Extra headers such as Retry-After and Server-Timing.
	for _, h := range extra {
		w.Header().Set(h[0], h[1])
	}
	// Status line, then the body.
	w.WriteHeader(status)
	w.Write(body)
}

// respondJSON marshals v and sends it.
func respondJSON(w http.ResponseWriter, status int, v interface{}, extra ...[2]string) {
	// Our structs always marshal; an error here would be a programming mistake.
	body, err := json.Marshal(v)
	if err != nil {
		log.Printf("marshal: %v", err)
		status, body = http.StatusInternalServerError, []byte(`{"error":"internal error"}`)
	}
	respond(w, status, body, extra...)
}

// totalOnly is the Server-Timing header with just the total time since t0.
func totalOnly(t0 time.Time) [2]string {
	var tm timing
	tm.add("total", time.Since(t0))
	return [2]string{"Server-Timing", tm.header()}
}

// retryAfter is the Retry-After value: whole seconds, rounded up, at least 1.
func retryAfter(retryMs int64) [2]string {
	return [2]string{"Retry-After", strconv.Itoa(int(math.Max(1, math.Ceil(float64(retryMs)/1000))))}
}

// newJobID makes a random version 4 UUID in lowercase hex, e.g. 6f1c2a3b-0000-4111-8222-123456789abc.
func newJobID() (string, error) {
	// 16 random bytes from the system.
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	// Mark it as version 4, variant 10 (RFC 4122).
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	// 8-4-4-4-12 hex groups.
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}

// ServeHTTP handles every request.
func (l *limiter) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Start the clock for this request.
	t0 := time.Now()
	// Strip the language prefix: "/go/chat" -> "/chat".
	p := r.URL.Path
	if strings.HasPrefix(p, pathPrefix) {
		p = strings.TrimPrefix(p, pathPrefix)
		if p == "" {
			p = "/"
		}
	}

	// Health check: no user id, no Redis, no SQS.
	if p == "/healthz" {
		respond(w, http.StatusOK, []byte(`{"ok":true}`))
		return
	}

	// Which route is this? Anything else is a 404.
	isChat := p == "/chat"
	isResult := strings.HasPrefix(p, "/result/")
	if !isChat && !isResult {
		respondJSON(w, http.StatusNotFound, errorBody{"not found"}, totalOnly(t0))
		return
	}
	// Each route allows exactly one method.
	want := http.MethodGet
	if isChat {
		want = http.MethodPost
	}
	if r.Method != want {
		respondJSON(w, http.StatusMethodNotAllowed, errorBody{"method not allowed"}, totalOnly(t0))
		return
	}

	// The caller must say who they are (test only: callers can fake this header).
	user := r.Header.Get("X-User-Id")
	// Missing or unsafe characters -> 400.
	if !callerIDRe.MatchString(user) {
		respondJSON(w, http.StatusBadRequest, errorBody{"X-User-Id header is required (letters, digits, _ . :)"}, totalOnly(t0))
		return
	}

	// Hand over to the route.
	if isChat {
		l.chat(w, r, user, t0)
		return
	}
	l.result(w, r, user, strings.TrimPrefix(p, "/result/"), t0)
}

// --- POST /chat ---------------------------------------------------------------

// parseChatBody checks the optional JSON body and returns the message and the fail hook.
func parseChatBody(body []byte) (message string, fail bool, err error) {
	// No body is fine: an empty message.
	if len(body) == 0 {
		return "", false, nil
	}
	// Bad UTF-8 is an error (the decoder would silently replace it).
	if !utf8.Valid(body) {
		return "", false, errors.New("body must be UTF-8")
	}
	// Must be a JSON object, e.g. {"message": "hi"}.
	var data map[string]interface{}
	if err := json.Unmarshal(body, &data); err != nil {
		return "", false, err
	}
	// "null" decodes without error to a nil map: not an object.
	if data == nil {
		return "", false, errors.New("body must be a JSON object")
	}
	// Missing message = empty message; anything but text is an error.
	if v, present := data["message"]; present {
		s, ok := v.(string)
		// The message must be text and not too long (counted in characters, not bytes).
		if !ok || utf8.RuneCountInString(s) > maxMessageChars {
			return "", false, fmt.Errorf("message must be text of at most %d characters", maxMessageChars)
		}
		message = s
	}
	// Test hook: {"fail": true} makes the backend raise, to test retries.
	fail = data["fail"] == true
	return message, fail, nil
}

func (l *limiter) chat(w http.ResponseWriter, r *http.Request, user string, t0 time.Time) {
	ctx := r.Context() // cancelled when the client disconnects, so Redis and SQS calls stop early
	// 1. Read and check the body (memory only, no network). MaxBytesReader stops at the limit.
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		// Too big: stop before parsing.
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			respondJSON(w, http.StatusRequestEntityTooLarge, errorBody{"body too large"}, totalOnly(t0))
		} else {
			respondJSON(w, http.StatusBadRequest, errorBody{"bad body: " + err.Error()}, totalOnly(t0))
		}
		return
	}
	// Not JSON, not an object, or a bad message: 400.
	message, fail, err := parseChatBody(body)
	if err != nil {
		respondJSON(w, http.StatusBadRequest, errorBody{"bad body: " + err.Error()}, totalOnly(t0))
		return
	}

	// Current settings (a plain struct, replaced as a whole when rules change).
	cfg := l.rules.get()
	// A random, hard to guess id for this job.
	jobID, err := newJobID()
	if err != nil {
		log.Printf("job id: %v", err)
		respondJSON(w, http.StatusInternalServerError, errorBody{"internal error"}, totalOnly(t0))
		return
	}
	// Lua args: language, user, job id, line count lifetime, job lifetime,
	// then 3 numbers per tier (tier, max_waiting, interval_ms).
	args := []interface{}{language, user, jobID, cfg.Queue.RetentionS + lineTTLCushionS, jobTTLS}
	for _, t := range cfg.Tiers {
		args = append(args, t.ID, t.MaxWaiting, int64(math.Round(t.IntervalMs)))
	}
	// Time after step 1.
	t1 := time.Now()

	// 2. One Redis round trip: tier, room in line?, ticket, job record.
	res, err := l.accept.Run(ctx, l.rdb, []string{"tiers"}, args...).Int64Slice()
	if err != nil || len(res) != 5 {
		// Redis down or script error: nothing was written, tell the client.
		log.Printf("redis error: %v (reply %v)", err, res)
		respondJSON(w, http.StatusServiceUnavailable, errorBody{"rate limiter unavailable"}, totalOnly(t0))
		return
	}
	// The 5 integers the script returns (the ticket, res[2], is not needed here).
	accepted, tier, position, retryMs := res[0], res[1], res[3], res[4]
	// Time after step 2.
	t2 := time.Now()

	// Line full: answer 429 without touching SQS.
	if accepted == 0 {
		var tm timing
		tm.add("redis", t2.Sub(t1))
		tm.add("total", t2.Sub(t0))
		respondJSON(w, http.StatusTooManyRequests, fullBody{"line full", tier, retryMs},
			retryAfter(retryMs), [2]string{"Server-Timing", tm.header()})
		return
	}

	// 3. Joined the line: put the job in this tier's SQS FIFO queue.
	payload, _ := json.Marshal(queueMessage{Job: jobID, Msg: message, Fail: fail})
	// Short timeout so a slow SQS cannot hold requests for long.
	sctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	_, err = l.sqs.SendMessage(sctx, &sqs.SendMessageInput{
		QueueUrl:               aws.String(queueURL(int(tier))), // this language and tier
		MessageBody:            aws.String(string(payload)),     // what the worker needs
		MessageGroupId:         aws.String(groupID),             // one group = strict first-in, first-out
		MessageDeduplicationId: aws.String(jobID),               // a repeat send of the same job is ignored
	})
	if err != nil {
		// Could not queue it: give the place in line back, then tell the client.
		log.Printf("SQS send failed for job %s: %v", jobID, err)
		if err := l.finish.Run(ctx, l.rdb, []string{jobKey(jobID)}, "failed", "could not queue the job", jobTTLS).Err(); err != nil {
			// The line count then expires on its own (its TTL).
			log.Printf("could not give the place back: %v", err)
		}
		var tm timing
		tm.add("redis", t2.Sub(t1))
		tm.add("total", time.Since(t0))
		respondJSON(w, http.StatusBadGateway, errorBody{"queue unavailable"}, [2]string{"Server-Timing", tm.header()})
		return
	}
	// Time after step 3.
	t3 := time.Now()

	// Tell the client where it stands; it polls GET /result/<job_id> from here.
	var tm timing
	tm.add("redis", t2.Sub(t1))
	tm.add("sqs", t3.Sub(t2))
	tm.add("total", t3.Sub(t0))
	respondJSON(w, http.StatusAccepted, acceptedBody{jobID, position, tier}, [2]string{"Server-Timing", tm.header()})
}

// --- GET /result/<job_id> -----------------------------------------------------

// readJob returns the job record (nil if missing), the Redis time (ms) and each
// tier's "now serving", all in one round trip.
func (l *limiter) readJob(ctx context.Context, jobID string, cfg *Config) (map[string]string, int64, map[int]int64, error) {
	// Pipeline = send all commands together, get all answers together.
	pipe := l.rdb.Pipeline()
	// The job record (empty map if missing).
	jobCmd := pipe.HGetAll(ctx, jobKey(jobID))
	// Redis server clock.
	timeCmd := pipe.Time(ctx)
	// "Now serving" of every configured tier (we don't know the job's tier yet).
	svCmds := make([]*redis.StringCmd, len(cfg.Tiers))
	for i, t := range cfg.Tiers {
		svCmds[i] = pipe.Get(ctx, fmt.Sprintf("sv-%s-%d", language, t.ID))
	}
	// Send. Exec's own error is just the first failed command (a missing "sv" key counts
	// as one), so every command is checked on its own below.
	_, _ = pipe.Exec(ctx)
	job, err := jobCmd.Result()
	if err != nil {
		return nil, 0, nil, err
	}
	now, err := timeCmd.Result()
	if err != nil {
		return nil, 0, nil, err
	}
	// tier id -> last finished ticket (0 if never set).
	serving := map[int]int64{}
	for i, t := range cfg.Tiers {
		v, err := svCmds[i].Int64()
		if err != nil && !errors.Is(err, redis.Nil) {
			return nil, 0, nil, err
		}
		serving[t.ID] = v
	}
	// No fields at all = no such job.
	if len(job) == 0 {
		job = nil
	}
	// Milliseconds since 1970 from the Redis clock.
	return job, now.UnixMilli(), serving, nil
}

// atoi reads a whole number from a job field (0 if missing or bad).
func atoi(s string) int64 {
	n, _ := strconv.ParseInt(s, 10, 64)
	return n
}

func (l *limiter) result(w http.ResponseWriter, r *http.Request, user, jobID string, t0 time.Time) {
	ctx := r.Context() // cancelled when the client disconnects, so Redis and SQS calls stop early
	// Not even shaped like one of our ids: 404, and Redis is not touched.
	if !jobIDRe.MatchString(jobID) {
		respondJSON(w, http.StatusNotFound, errorBody{"no such job"}, totalOnly(t0))
		return
	}

	// Current settings.
	cfg := l.rules.get()
	// 1. Polling limit: reuse the token bucket script on two buckets (all-or-nothing):
	//    one per job and one per user. The job bucket also covers unknown ids.
	pr, err := l.poll.Run(ctx, l.rdb, []string{"poll-u" + jobID, "pollu-u" + user},
		cfg.Poll.JobSize, cfg.Poll.JobRate, 1, cfg.Poll.UserSize, cfg.Poll.UserRate, 1).Int64Slice()
	if err != nil || len(pr) < 3 {
		// Redis down or script error.
		log.Printf("redis error: %v (reply %v)", err, pr)
		respondJSON(w, http.StatusServiceUnavailable, errorBody{"rate limiter unavailable"}, totalOnly(t0))
		return
	}
	// Time after step 1.
	t1 := time.Now()
	// Too many polls: 429 with a wait time.
	if pr[0] == 0 {
		var tm timing
		tm.add("redis", t1.Sub(t0))
		tm.add("total", t1.Sub(t0))
		respondJSON(w, http.StatusTooManyRequests, pollBody{"polling too fast", pr[2]},
			retryAfter(pr[2]), [2]string{"Server-Timing", tm.header()})
		return
	}

	// 2. Read the job, the Redis clock, and every tier's "now serving" in one round trip.
	job, nowMs, serving, err := l.readJob(ctx, jobID, cfg)
	if err != nil {
		log.Printf("redis error: %v", err)
		respondJSON(w, http.StatusServiceUnavailable, errorBody{"rate limiter unavailable"}, totalOnly(t0))
		return
	}
	// Unknown job (never existed or its record expired) or someone else's job: 404.
	if job == nil || job["u"] != user {
		var tm timing
		tm.add("redis", time.Since(t1))
		tm.add("total", time.Since(t0))
		respondJSON(w, http.StatusNotFound, errorBody{"no such job"}, [2]string{"Server-Timing", tm.header()})
		return
	}

	// 3. A job that is still waiting but older than the SQS retention has lost its
	//    message (SQS dropped it): end it as "expired" and give the place back.
	if (job["st"] == "queued" || job["st"] == "processing") && nowMs-atoi(job["t0"]) > int64(cfg.Queue.RetentionS)*1000 {
		if err := l.finish.Run(ctx, l.rdb, []string{jobKey(jobID)}, "expired", "expired in the queue before it was processed", jobTTLS).Err(); err != nil {
			log.Printf("redis error: %v", err)
			respondJSON(w, http.StatusServiceUnavailable, errorBody{"rate limiter unavailable"}, totalOnly(t0))
			return
		}
		// Read again: if a worker finished it first, we report what really happened.
		job, nowMs, serving, err = l.readJob(ctx, jobID, cfg)
		// The record can only vanish in a rare race (its TTL ran out right now).
		if err != nil || job == nil {
			respondJSON(w, http.StatusNotFound, errorBody{"no such job"}, totalOnly(t0))
			return
		}
	}
	// Time after step 2/3.
	t2 := time.Now()

	// Build the answer. Status and place in line (0 once the job has ended).
	status := job["st"]
	tier := atoi(job["tr"])
	out := resultBody{Status: status, Tier: tier}
	if status == "queued" || status == "processing" {
		// Place in line = ticket - now serving of its tier (at least 1).
		out.Position = max(1, atoi(job["tk"])-serving[int(tier)])
	} else {
		// Ended: how long it waited and how long the agent took.
		qw, pm := atoi(job["qw"]), atoi(job["pm"])
		out.QueueWaitMs, out.ProcessingMs = &qw, &pm
		// The agent's answer, or why it failed.
		text := job["a"]
		if status == "done" {
			out.Answer = &text
		} else {
			out.Error = &text
		}
	}
	var tm timing
	tm.add("redis", t2.Sub(t1))
	tm.add("total", t2.Sub(t0))
	respondJSON(w, http.StatusOK, out, [2]string{"Server-Timing", tm.header()})
}

// --- Start --------------------------------------------------------------------

// seedTiers writes the test-only seed users into the tiers hash (HSET is safe to repeat).
func seedTiers(ctx context.Context, rdb *redis.Client, rules *rulesHolder) {
	// user id -> tier number.
	seed := rules.get().Seed
	if len(seed) == 0 {
		return
	}
	// One call writes all users: field = user id, value = tier.
	values := make(map[string]interface{}, len(seed))
	for user, tid := range seed {
		values[user] = tid
	}
	if err := rdb.HSet(ctx, "tiers", values).Err(); err != nil {
		log.Printf("seeding the tiers hash failed: %v", err)
		return
	}
	log.Printf("seeded %d test users into the tiers hash", len(seed))
}

// runLimiter starts the HTTP server and runs until ctx is cancelled.
func runLimiter(ctx context.Context) {
	// Load the Lua scripts (the algorithm itself).
	acceptSrc, err := readFile(acceptLua)
	if err != nil {
		log.Fatalf("read accept.lua: %v", err)
	}
	finishSrc, err := readFile(finishLua)
	if err != nil {
		log.Fatalf("read finish.lua: %v", err)
	}
	pollSrc, err := readFile(tokenBucketLua)
	if err != nil {
		log.Fatalf("read token_bucket.lua: %v", err)
	}

	// Loads credentials: the EC2 instance role when deployed.
	awsCfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		log.Fatalf("aws config: %v", err)
	}

	// Long-lived clients.
	rdb := redis.NewClient(&redis.Options{Addr: redisAddr, Password: os.Getenv("REDIS_PASSWORD")}) // pool, reused by all requests
	l := &limiter{
		rdb:    rdb,
		accept: redis.NewScript(acceptSrc), // runs via EVALSHA
		finish: redis.NewScript(finishSrc), // runs via EVALSHA
		poll:   redis.NewScript(pollSrc),   // runs via EVALSHA
		sqs:    sqs.NewFromConfig(awsCfg),
		rules:  &rulesHolder{},
	}
	// S3 client only when a bucket is configured.
	if rulesBucket != "" {
		l.rules.s3 = s3.NewFromConfig(awsCfg)
	}

	// Load rules before taking traffic.
	if err := l.rules.loadInitial(ctx); err != nil {
		log.Fatal(err)
	}
	// TEST ONLY: write the seed users into the tiers hash.
	seedTiers(ctx, rdb, l.rules)
	// With S3, keep checking for rule changes (and re-apply the seed users after one).
	if l.rules.s3 != nil {
		every, _ := strconv.ParseFloat(env("RULES_REFRESH_S", "30"), 64)
		go l.rules.refreshForever(time.Duration(every*float64(time.Second)), func() { seedTiers(context.Background(), rdb, l.rules) })
	}

	// Default is loopback. The Dockerfile sets HOST=0.0.0.0 because inside the
	// container only the Docker network can reach it (no port is published).
	addr := net.JoinHostPort(env("HOST", "127.0.0.1"), env("PORT", "8000"))
	log.Printf("limiter listening on %s", addr)
	// Timeouts stop slow clients from holding connections forever.
	srv := &http.Server{Addr: addr, Handler: l, ReadHeaderTimeout: 5 * time.Second}
	// Stop the server when ctx is cancelled (docker stop sends SIGTERM).
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdown)
	}()
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
	log.Print("limiter stopped")
}

// stopSignals returns a context that is cancelled on SIGINT / SIGTERM.
func stopSignals() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}
