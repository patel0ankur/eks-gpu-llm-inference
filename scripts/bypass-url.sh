#!/usr/bin/env bash
# Local bypass URL for testing without the NLB.
#   http://127.0.0.1:${PORT}  ->  public-gw Envoy  ->  chat-api  ->  llm-gw Envoy  ->  vLLM
# Same HTTPRoutes and BackendTrafficPolicies as the NLB path; only the AWS load balancer is skipped.
# Bound to 127.0.0.1 only: reachable from this machine, nowhere else.
# Note: port-forward pins one Envoy Pod, so the rate limit is 20 req/s total (not per replica).
set -euo pipefail
PORT="${PORT:-8080}"
echo "Bypass URL: http://127.0.0.1:${PORT}   (Ctrl-C to stop)"
echo "Try:  curl -s -X POST http://127.0.0.1:${PORT}/v1/ask -H 'Content-Type: application/json' -d '{\"question\":\"hi\"}'"
exec kubectl -n envoy-gateway-system port-forward --address 127.0.0.1 svc/public-gw "${PORT}:80"
