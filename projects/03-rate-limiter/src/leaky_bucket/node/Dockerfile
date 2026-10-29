# Node leaky bucket LIMITER (the door: POST /chat, GET /result).
# Build context is src/ (one level above leaky_bucket/) so the token bucket script
# can be reused without copying it into this folder:
#   docker build -f leaky_bucket/node/Dockerfile -t limiter-node .
# Small official Node LTS image.
FROM node:22-slim
# All app files live in /app inside the container.
WORKDIR /app
# Copy the dependency files first, so Docker can cache the install step.
COPY leaky_bucket/node/package.json leaky_bucket/node/package-lock.json ./
# Install exactly what the lock file says (integrity-checked), no dev deps.
RUN npm ci --omit=dev
# The EXISTING token bucket script (reused for the polling limit), our two scripts and the default rules.
COPY token_bucket/token_bucket.lua leaky_bucket/accept.lua leaky_bucket/finish.lua leaky_bucket/rules.yaml ./
# The server code and the helpers it shares with the worker.
COPY leaky_bucket/node/common.js leaky_bucket/node/server.js ./
# Bind all interfaces INSIDE the container only. Compose publishes no port for
# this service; nginx reaches it over the private Docker network.
# The *_LUA / RULES_FILE variables point at the files copied above.
ENV HOST=0.0.0.0 PORT=8000 LANGUAGE=node ACCEPT_LUA=/app/accept.lua FINISH_LUA=/app/finish.lua TOKEN_BUCKET_LUA=/app/token_bucket.lua RULES_FILE=/app/rules.yaml NODE_ENV=production
# Run as the image's unprivileged "node" user, not root.
USER node
# Start the server.
CMD ["node", "server.js"]
