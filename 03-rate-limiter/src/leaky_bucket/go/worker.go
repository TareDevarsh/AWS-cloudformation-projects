// Leaky bucket (queue) worker, Go version. Same behaviour as python/worker.py.
//
// The worker is the LEAK: it decides the speed. For every tier it runs one
// pacing loop that starts one job every 1 / leak_rate seconds, no faster and
// no slower (when there is work). Tier 0 (regular) starts 1 job per second,
// tier 1 (business) starts 5 per second (see rules.yaml).
//
// Job flow, per tier:
//  1. Take jobs from the tier's SQS FIFO queue (a small batch at a time).
//  2. At each tick START the next job. The worker does not wait for the
//     previous reply, so a 200 ms agent still keeps up with 5 starts a second.
//  3. For each started job (in the background):
//     a) finish.lua "processing"        (status for the polling client)
//     b) invoke the backend Lambda      (sleep_ms 200 = the fake chat agent)
//     c) finish.lua "done"              (answer, times, line count -1, now serving +1)
//     d) delete the SQS message
//     Backend error: before the 5th attempt, hide the message for 1 s so SQS
//     hands it back quickly. On the 5th failed attempt: finish.lua "failed" and
//     delete the message, so nobody waits for a job that cannot succeed.
//
// No bursts after idle time: a tick that was missed is not made up later.
// After a start at time T the next start is no earlier than T + interval.
//
// Extra config: BACKEND_FUNCTION (Lambda name; empty = built-in local echo agent),
// RECEIVE_WAIT_S (long poll seconds when nothing is in flight, default 5).
package main

import (
	"context"       // deadlines/cancellation for Redis and AWS calls
	"encoding/json" // reads SQS message bodies and Lambda answers
	"errors"        // builds errors
	"fmt"           // string formatting
	"log"           // startup and error logs
	"math"          // Ceil for the visibility timeout
	"os"            // environment variables
	"strconv"       // number <-> text
	"sync/atomic"   // counts jobs in flight per tier
	"time"          // pacing and timeouts

	"github.com/aws/aws-sdk-go-v2/aws"               // AWS helper types (aws.String)
	"github.com/aws/aws-sdk-go-v2/config"            // loads credentials (instance role on EC2)
	"github.com/aws/aws-sdk-go-v2/service/lambda"    // invokes the backend Lambda
	"github.com/aws/aws-sdk-go-v2/service/s3"        // reads rules.yaml from S3
	"github.com/aws/aws-sdk-go-v2/service/sqs"       // takes and deletes jobs
	"github.com/aws/aws-sdk-go-v2/service/sqs/types" // SQS message type
	"github.com/redis/go-redis/v9"                   // Redis client
)

// --- Config -------------------------------------------------------------------

var (
	// Backend Lambda name. Empty = local echo agent (testing).
	backendFunction = env("BACKEND_FUNCTION", "")
	// Seconds one receive call may wait for a message (long polling: returns early on arrival).
	receiveWaitS = atoiDefault(env("RECEIVE_WAIT_S", "5"), 5)
)

// maxAnswerChars is the longest answer text we store in Redis, in characters.
const maxAnswerChars = 2000

// atoiDefault reads a whole number, or returns def if the text is not one.
func atoiDefault(s string, def int) int {
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}

// truncate cuts text to at most n characters (not bytes).
func truncate(s string, n int) string {
	// Fast path: n bytes can hold at most n characters, so short text is fine as it is.
	if len(s) <= n {
		return s
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// sleepCtx waits for d, or less if ctx is cancelled. A zero or negative d returns at once.
func sleepCtx(ctx context.Context, d time.Duration) {
	if d <= 0 {
		return
	}
	// A timer that can be stopped, so nothing is left running.
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C: // waited long enough
	case <-ctx.Done(): // told to stop
	}
}

// --- The fake chat agent --------------------------------------------------------

// agentRequest is what is sent to the backend Lambda.
type agentRequest struct {
	Message string `json:"message"`  // chat text
	Fail    bool   `json:"fail"`     // test hook: make the agent raise
	SleepMs int    `json:"sleep_ms"` // how long the agent takes
}

// backend is the backend Lambda, or a local echo when BACKEND_FUNCTION is empty.
type backend struct {
	lambda *lambda.Client // nil = local echo
}

// call asks the agent for a reply; any error is a failed attempt.
func (b *backend) call(ctx context.Context, req agentRequest) (string, error) {
	// No Lambda configured: act like the agent locally (testing).
	if b.lambda == nil {
		// Take as long as the real agent would.
		sleepCtx(ctx, time.Duration(req.SleepMs)*time.Millisecond)
		// Same test hook as the real backend.
		if req.Fail {
			return "", errors.New("backend error: simulated failure")
		}
		return "agent reply to: " + req.Message, nil
	}
	// The request as JSON bytes.
	payload, err := json.Marshal(req)
	if err != nil {
		return "", err
	}
	// One try only (the queue does the retrying) and a read limit above the agent's 200 ms.
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	// Call the Lambda synchronously and wait for its answer.
	out, err := b.lambda.Invoke(cctx, &lambda.InvokeInput{FunctionName: aws.String(backendFunction), Payload: payload})
	if err != nil {
		return "", fmt.Errorf("backend error: %w", err)
	}
	// The Lambda's response body (empty = {}).
	var body struct {
		Reply        interface{} `json:"reply"`        // the agent's reply text
		ErrorMessage string      `json:"errorMessage"` // set by Lambda when it crashed
	}
	if len(out.Payload) > 0 {
		if err := json.Unmarshal(out.Payload, &body); err != nil {
			return "", fmt.Errorf("backend error: bad reply: %w", err)
		}
	}
	// FunctionError is set if the Lambda crashed: that is a failed attempt.
	if out.FunctionError != nil {
		msg := body.ErrorMessage
		if msg == "" {
			msg = "unknown"
		}
		return "", errors.New("backend error: " + msg)
	}
	// The agent's reply text (nothing = empty).
	switch v := body.Reply.(type) {
	case nil:
		return "", nil
	case string:
		return v, nil
	default:
		return fmt.Sprint(v), nil
	}
}

// --- The worker -----------------------------------------------------------------

// worker holds the clients and the state shared by the pacing loops.
type worker struct {
	rdb      *redis.Client         // Redis connection pool
	finish   *redis.Script         // finish.lua (EVALSHA, reloads if Redis lost it)
	sqs      *sqs.Client           // takes and deletes jobs
	backend  *backend              // the agent (Lambda or local echo)
	rules    *rulesHolder          // in-memory rules; the worker reads them once at start
	inflight map[int]*atomic.Int64 // jobs being processed, per tier id (fixed at start)
}

// finishJob runs finish.lua and returns its result: 1 applied, 0 already ended, -1 no such job.
func (w *worker) finishJob(ctx context.Context, key, status, text string) (int64, error) {
	return w.finish.Run(ctx, w.rdb, []string{key}, status, text, jobTTLS).Int64()
}

// run starts one pacing loop per tier and returns when ctx is cancelled.
func (w *worker) run(ctx context.Context) {
	cfg := w.rules.get()
	// One counter per tier. A tier added later needs a worker restart.
	w.inflight = make(map[int]*atomic.Int64, len(cfg.Tiers))
	for _, t := range cfg.Tiers {
		w.inflight[t.ID] = new(atomic.Int64)
	}
	// Say what we are going to do.
	for _, t := range cfg.Tiers {
		log.Printf("worker %s: tier %d %v/s", language, t.ID, t.LeakRate)
	}
	// One goroutine per tier; wait until all of them end.
	done := make(chan struct{}, len(cfg.Tiers))
	for _, t := range cfg.Tiers {
		go func(t Tier) {
			w.pace(ctx, t)
			done <- struct{}{}
		}(t)
	}
	for range cfg.Tiers {
		<-done
	}
}

// --- The pacing loop: this is the leak -------------------------------------------

func (w *worker) pace(ctx context.Context, tier Tier) {
	// Settings of this tier.
	interval := time.Duration(tier.IntervalMs * float64(time.Millisecond))
	// How many messages to take per receive: twice the leak rate, between 1 and 10.
	batch := min(10, max(1, int(2*tier.LeakRate)))
	// Received messages waiting for their tick, in queue order.
	var buffer []types.Message
	// Earliest time the next job may start (time.Now carries a clock that never jumps).
	nextTick := time.Now()
	// Jobs of this tier in flight.
	inflight := w.inflight[tier.ID]
	for ctx.Err() == nil {
		// Nothing buffered: ask the queue.
		if len(buffer) == 0 {
			// SQS FIFO hands out no further message of the (single) group while earlier ones
			// are still being processed. So while jobs of this tier are in flight, check
			// again every 50 ms (short poll) to pick up the next batch the moment the last
			// delete lands. With nothing in flight, long poll: it returns as soon as a job arrives.
			busy := inflight.Load() > 0
			wait := receiveWaitS
			if busy {
				wait = 0
			}
			buffer = w.receive(ctx, tier, batch, wait)
			// Empty answer: poll again (after a short pause if we were short polling).
			if len(buffer) == 0 {
				if busy {
					sleepCtx(ctx, 50*time.Millisecond)
				}
				continue
			}
		}
		// Wait for the tick. If we are already past it (idle time, slow receive)
		// there is no wait, and the missed ticks are NOT made up: no burst.
		sleepCtx(ctx, time.Until(nextTick))
		if ctx.Err() != nil {
			return
		}
		// The tick: start the next job in the background, without waiting for its reply.
		msg := buffer[0]
		buffer = buffer[1:]
		inflight.Add(1)
		go func() {
			// Forget the job when it ends.
			defer inflight.Add(-1)
			w.handle(ctx, tier, msg)
		}()
		// Next start is one interval after THIS start (not after the old tick).
		nextTick = time.Now().Add(interval)
	}
}

// receive takes up to batch messages from the tier's queue, waiting up to waitS seconds.
func (w *worker) receive(ctx context.Context, tier Tier, batch, waitS int) []types.Message {
	// Queue settings.
	q := w.rules.get().Queue
	// Messages wait in our buffer up to batch * interval; keep them hidden at least that long.
	hiddenS := q.VisibilityTimeoutS + int(math.Ceil(float64(batch)*tier.IntervalMs/1000))
	// The read limit must be longer than the long poll, or every empty receive would time out.
	rctx, cancel := context.WithTimeout(ctx, time.Duration(waitS+5)*time.Second)
	defer cancel()
	resp, err := w.sqs.ReceiveMessage(rctx, &sqs.ReceiveMessageInput{
		QueueUrl:            aws.String(queueURL(tier.ID)), // this language and tier
		MaxNumberOfMessages: int32(batch),                  // up to this many
		WaitTimeSeconds:     int32(waitS),                  // long poll (0 = answer at once)
		VisibilityTimeout:   int32(hiddenS),                // hidden from others while we hold them
		MessageSystemAttributeNames: []types.MessageSystemAttributeName{
			types.MessageSystemAttributeNameApproximateReceiveCount, // how many times handed out
		},
	})
	if err != nil {
		// Stopping: not an error.
		if ctx.Err() != nil {
			return nil
		}
		// SQS hiccup: log, pause briefly, and the loop tries again.
		log.Printf("receive failed (tier %d): %v", tier.ID, err)
		sleepCtx(ctx, time.Second)
		return nil
	}
	// Messages come back in queue order (single message group).
	return resp.Messages
}

// --- One started job --------------------------------------------------------------

// jobMessage is what the limiter put in the SQS message.
type jobMessage struct {
	Job  string      `json:"job"`  // job id
	Msg  string      `json:"msg"`  // chat text
	Fail interface{} `json:"fail"` // test hook; only the JSON value true counts
}

func (w *worker) handle(ctx context.Context, tier Tier, msg types.Message) {
	// The queue URL, needed to delete or hide this message.
	url := queueURL(tier.ID)
	// How many times SQS has handed out this message (1 on the first try).
	attempt := atoiDefault(msg.Attributes[string(types.MessageSystemAttributeNameApproximateReceiveCount)], 1)
	// What the limiter put in the message: job id, chat text, test hook.
	var body jobMessage
	if err := json.Unmarshal([]byte(aws.ToString(msg.Body)), &body); err != nil || body.Job == "" {
		// Not one of ours: delete it so it cannot block the line.
		log.Printf("dropping malformed message %s", aws.ToString(msg.MessageId))
		w.deleteMessage(ctx, url, msg)
		return
	}
	key := jobKey(body.Job)
	q := w.rules.get().Queue
	// a) Mark the start. 0 = already ended (a repeat delivery), -1 = record gone:
	//    in both cases there is nothing to do but remove the message.
	started, err := w.finishJob(ctx, key, "processing", "")
	if err != nil {
		// Redis trouble: leave the message; SQS hands it back after its visibility timeout.
		log.Printf("job %s: could not complete bookkeeping: %v", body.Job, err)
		return
	}
	if started != 1 {
		w.deleteMessage(ctx, url, msg)
		return
	}
	// b) Ask the agent. sleep_ms makes the Lambda take as long as a real model would.
	reply, err := w.backend.call(ctx, agentRequest{Message: body.Msg, Fail: body.Fail == true, SleepMs: w.rules.get().AgentSleepMs})
	if err != nil {
		// The agent failed. Last allowed attempt: end the job as failed and drop the message.
		if attempt >= q.MaxAttempts {
			log.Printf("job %s failed after %d attempts: %v", body.Job, attempt, err)
			text := truncate(fmt.Sprintf("failed after %d attempts: %v", attempt, err), maxAnswerChars)
			if _, ferr := w.finishJob(ctx, key, "failed", text); ferr != nil {
				log.Printf("job %s: could not complete bookkeeping: %v", body.Job, ferr)
				return
			}
			w.deleteMessage(ctx, url, msg)
			return
		}
		// Otherwise hide the message for just 1 s so SQS returns it for a quick retry.
		log.Printf("job %s attempt %d failed: %v", body.Job, attempt, err)
		if _, verr := w.sqs.ChangeMessageVisibility(ctx, &sqs.ChangeMessageVisibilityInput{
			QueueUrl: aws.String(url), ReceiptHandle: msg.ReceiptHandle, VisibilityTimeout: int32(q.RetryVisibilityS),
		}); verr != nil {
			log.Printf("job %s: could not complete bookkeeping: %v", body.Job, verr)
		}
		return
	}
	// c) Save the answer, give the user's place back, move "now serving" up.
	if _, err := w.finishJob(ctx, key, "done", truncate(reply, maxAnswerChars)); err != nil {
		log.Printf("job %s: could not complete bookkeeping: %v", body.Job, err)
		return
	}
	// d) Only now remove the message. If we crashed before this, SQS would redeliver it
	//    and step a) would see the job already ended.
	w.deleteMessage(ctx, url, msg)
}

// deleteMessage removes the message from the queue for good (errors are logged only).
func (w *worker) deleteMessage(ctx context.Context, url string, msg types.Message) {
	if _, err := w.sqs.DeleteMessage(ctx, &sqs.DeleteMessageInput{QueueUrl: aws.String(url), ReceiptHandle: msg.ReceiptHandle}); err != nil {
		log.Printf("delete failed: %v", err)
	}
}

// --- Start ------------------------------------------------------------------------

// runWorker loads rules and runs the pacing loops until ctx is cancelled.
func runWorker(ctx context.Context) {
	// Load the finish script (the worker never uses the other scripts).
	finishSrc, err := readFile(finishLua)
	if err != nil {
		log.Fatalf("read finish.lua: %v", err)
	}
	// Loads credentials: the EC2 instance role when deployed.
	awsCfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		log.Fatalf("aws config: %v", err)
	}
	// Long-lived clients.
	w := &worker{
		rdb:     redis.NewClient(&redis.Options{Addr: redisAddr, Password: os.Getenv("REDIS_PASSWORD")}), // pool, reused by all jobs
		finish:  redis.NewScript(finishSrc),                                                              // runs via EVALSHA
		sqs:     sqs.NewFromConfig(awsCfg),
		backend: &backend{},
		rules:   &rulesHolder{},
	}
	// Lambda client only if a function is configured (one try only: the queue does the retrying).
	if backendFunction != "" {
		w.backend.lambda = lambda.NewFromConfig(awsCfg, func(o *lambda.Options) { o.RetryMaxAttempts = 1 })
	}
	// S3 client only when a bucket is configured.
	if rulesBucket != "" {
		w.rules.s3 = s3.NewFromConfig(awsCfg)
	}
	// Load rules once. Restart the worker to apply changes or a new tier.
	if err := w.rules.loadInitial(ctx); err != nil {
		log.Fatal(err)
	}
	// Run until stopped, then close Redis.
	w.run(ctx)
	w.rdb.Close()
	log.Print("worker stopped")
}
