// Leaky bucket (queue) rate limiter, Go version: ONE binary with two modes.
//
//	leaky limiter   the "door": POST /chat, GET /result/<job_id>, GET /healthz  (limiter.go)
//	leaky worker    the "leak": starts jobs from the SQS queues at a fixed rate (worker.go)
//
// Both modes share the config and rules code in common.go. The two Dockerfiles
// (Dockerfile and Dockerfile.worker) build the same code; they only differ in
// the mode they start and in the files they copy. With no argument the mode is
// "limiter".
package main

import (
	"log" // startup and error logs
	"os"  // command line arguments
)

func main() {
	// Mode from the first argument; the limiter is the default.
	mode := "limiter"
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	// A context that is cancelled by SIGINT / SIGTERM (docker stop), so both modes stop cleanly.
	ctx, stop := stopSignals()
	defer stop()
	// Run the chosen mode until it is told to stop.
	switch mode {
	case "limiter":
		runLimiter(ctx)
	case "worker":
		runWorker(ctx)
	default:
		log.Fatalf("unknown mode %q (use limiter or worker)", mode)
	}
}
