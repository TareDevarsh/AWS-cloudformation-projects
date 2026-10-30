# Python sliding window log limiter.
# Build context is src/sliding_window_log/ so the shared Lua script and rules are included:
#   docker build -f python/Dockerfile -t limiter-python .

# Small official Python image (Debian slim, no extra tools).
FROM python:3.12-slim

# All app files live in /app inside the container.
WORKDIR /app
# Copy only the dependency list first, so Docker can cache the install step.
COPY python/requirements.txt .
# Install pinned dependencies; --no-cache-dir keeps the image smaller.
RUN pip install --no-cache-dir -r requirements.txt

# Shared algorithm and default rules (one level up from this Dockerfile).
COPY sliding_window_log.lua rules.yaml ./
# The server code.
COPY python/app.py .

# Bind all interfaces INSIDE the container only. Compose publishes no port for
# this service; nginx reaches it over the private Docker network.
# LUA_PATH / RULES_FILE point at the files copied above.
ENV HOST=0.0.0.0 PORT=8000 LUA_PATH=/app/sliding_window_log.lua RULES_FILE=/app/rules.yaml
# Run as an unprivileged user, not root.
USER nobody
# Start the server.
CMD ["python", "app.py"]
