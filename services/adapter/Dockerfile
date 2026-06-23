# Axonate CLI adapter (POC, DISPOSABLE). Bundles node + the Claude/Codex CLIs.
# Deleted at the production swap.
FROM python:3.11-slim

# node 20 for the CLIs
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# the two agent CLIs the adapter shells out to
RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cli_adapter.py .

# config-dir volumes (mounted in compose) hold the CLI logins; /work is the write sandbox
RUN mkdir -p /cfg/acctA /cfg/acctB /cfg/codex /work

EXPOSE 8080
# bind 0.0.0.0 inside the container; compose only exposes it on the internal network
CMD ["uvicorn", "cli_adapter:app", "--host", "0.0.0.0", "--port", "8080"]
