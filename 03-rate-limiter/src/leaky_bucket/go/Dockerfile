# Go leaky bucket LIMITER (the door: POST /chat, GET /result).
# One Go program with two modes (limiter and worker); this image starts the limiter.
# Build context is src/ (one level above leaky_bucket/) so the token bucket script
# can be reused without copying it into this folder:
#   docker build -f leaky_bucket/go/Dockerfile -t limiter-go .
# --- Stage 1: build ---------------------------------------------------------
# Official Go image with the compiler.
FROM golang:1.27 AS build
# Work in /src.
WORKDIR /src
# Copy the module file first, so the dependency step is cached.
COPY leaky_bucket/go/go.mod ./
# Copy the code (tidy needs it to see which packages are imported).
COPY leaky_bucket/go/*.go ./
# Download pinned deps and write go.sum; each download is checked against
# Go's checksum database (sum.golang.org).
RUN go mod tidy
# Build one static binary (no C libraries), stripped of debug info (-s -w).
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /leaky .
# --- Stage 2: run -----------------------------------------------------------
# Minimal image: just the binary, CA certificates for AWS HTTPS, and a non-root user.
FROM gcr.io/distroless/static-debian12:nonroot
# The compiled program from stage 1.
COPY --from=build /leaky /leaky
# The EXISTING token bucket script (reused for the polling limit), our two scripts and the default rules.
COPY token_bucket/token_bucket.lua leaky_bucket/accept.lua leaky_bucket/finish.lua leaky_bucket/rules.yaml /app/
# Bind all interfaces INSIDE the container only. Compose publishes no port for
# this service; nginx reaches it over the private Docker network.
# The *_LUA / RULES_FILE variables point at the files copied above.
ENV HOST=0.0.0.0 PORT=8000 LANGUAGE=go ACCEPT_LUA=/app/accept.lua FINISH_LUA=/app/finish.lua TOKEN_BUCKET_LUA=/app/token_bucket.lua RULES_FILE=/app/rules.yaml
# Start the limiter mode (image already runs as the "nonroot" user).
ENTRYPOINT ["/leaky", "limiter"]
