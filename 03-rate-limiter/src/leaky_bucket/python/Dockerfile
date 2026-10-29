# Python leaky bucket LIMITER (the door: POST /chat, GET /result).
# Build context is src/ (one level above leaky_bucket/) so the token bucket script
# can be reused without copying it into this folder:
#   docker build -f leaky_bucket/python/Dockerfile -t limiter-python .

# Small official Python image (Debian slim, no extra tools).
FROM python:3.12-slim

# All app files live in /app inside the container.
WORKDIR /app
# Copy only the dependency list first, so Docker can cache the install step.
COPY leaky_bucket/python/requirements.txt .
# Install pinned dependencies; --no-cache-dir keeps the image smaller.
RUN pip install --no-cache-dir -r requirements.txt

# The EXISTING token bucket script (reused for the polling limit), our two scripts and the default rules.
COPY token_bucket/token_bucket.lua leaky_bucket/accept.lua leaky_bucket/finish.lua leaky_bucket/rules.yaml ./
# The server code and the helpers it shares with the worker.
COPY leaky_bucket/python/common.py leaky_bucket/python/app.py ./

# Bind all interfaces INSIDE the container only. Compose publishes no port for
# this service; nginx reaches it over the private Docker network.
# The *_LUA / RULES_FILE variables point at the files copied above.
ENV HOST=0.0.0.0 PORT=8000 LANGUAGE=python ACCEPT_LUA=/app/accept.lua FINISH_LUA=/app/finish.lua TOKEN_BUCKET_LUA=/app/token_bucket.lua RULES_FILE=/app/rules.yaml
# Run as an unprivileged user, not root.
USER nobody
# Start the server.
CMD ["python", "app.py"]
