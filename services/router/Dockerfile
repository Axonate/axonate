# Axonate router (permanent, production-grade). Sits in front of LiteLLM.
FROM python:3.14-slim

WORKDIR /app
COPY services/router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/router/router.py services/router/routing.yaml ./
# auth shim is shared logic, copied in from services/auth
COPY services/auth/auth_shim.py ./
# per-user email -> virtual key map (optional; dev mode falls back to master key)
COPY config/users.yaml ./users.yaml

EXPOSE 4100
CMD ["uvicorn", "router:app", "--host", "0.0.0.0", "--port", "4100"]
