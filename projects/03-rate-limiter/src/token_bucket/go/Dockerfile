# Go token bucket limiter.
# Build context is src/token_bucket/ so the shared Lua script and rules are included:
#   docker build -f go/Dockerfile -t limiter-go .

# --- Stage 1: build ---------------------------------------------------------
# Official Go image with the compiler.
FROM golang:1.27 AS build
# Work in /src.
WORKDIR /src
# Copy the module file first, so the dependency step is cached.
COPY go/go.mod ./
# Copy the code (tidy needs it to see which packages are imported).
COPY go/main.go ./
# Download pinned deps and write go.sum; each download is checked against
# Go's checksum database (sum.golang.org).
RUN go mod tidy
# Build one static binary (no C libraries), stripped of debug info (-s -w).
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /limiter .

# --- Stage 2: run -----------------------------------------------------------
# Minimal image: just the binary, CA certificates for AWS HTTPS, and a non-root user.
FROM gcr.io/distroless/static-debian12:nonroot
# The compiled server from stage 1.
COPY --from=build /limiter /limiter
# Shared algorithm and default rules.
COPY token_bucket.lua rules.yaml /app/

# Bind all interfaces INSIDE the container only. Compose publishes no port for
# this service; nginx reaches it over the private Docker network.
ENV HOST=0.0.0.0 PORT=8000 LUA_PATH=/app/token_bucket.lua RULES_FILE=/app/rules.yaml
# Start the server (image already runs as the "nonroot" user).
ENTRYPOINT ["/limiter"]
