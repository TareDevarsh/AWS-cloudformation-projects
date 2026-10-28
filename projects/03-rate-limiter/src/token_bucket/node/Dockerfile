# Node token bucket limiter.
# Build context is src/token_bucket/ so the shared Lua script and rules are included:
#   docker build -f node/Dockerfile -t limiter-node .

# Small official Node LTS image.
FROM node:22-slim

# All app files live in /app inside the container.
WORKDIR /app
# Copy the dependency files first, so Docker can cache the install step.
COPY node/package.json node/package-lock.json ./
# Install exactly what the lock file says (integrity-checked), no dev deps.
RUN npm ci --omit=dev

# Shared algorithm and default rules.
COPY token_bucket.lua rules.yaml ./
# The server code.
COPY node/server.js .

# Bind all interfaces INSIDE the container only. Compose publishes no port for
# this service; nginx reaches it over the private Docker network.
ENV HOST=0.0.0.0 PORT=8000 LUA_PATH=/app/token_bucket.lua RULES_FILE=/app/rules.yaml NODE_ENV=production
# Run as the image's unprivileged "node" user, not root.
USER node
# Start the server.
CMD ["node", "server.js"]
