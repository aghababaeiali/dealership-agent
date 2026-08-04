#!/bin/bash
# Step 12, Part C: a screen-recording script for the LIVE AWS deployment.
# Hits the real ALB endpoint over the network - nothing here runs the
# graph in-process (that's scripts/demo.py, for local dev). Prints
# clearly, with pauses between sections, for someone watching a
# recording at normal speed who has never seen this project before.
#
# Requires:
#   - infra/terraform apply already run (see docs/DEPLOYMENT.md)
#   - Two demo customers already created in the live RDS instance, with
#     known order refs - customers can't be created from this machine
#     directly (RDS has no public access, by design), so this script
#     takes their ids/refs as configuration, not something it sets up
#     itself. See docs/DEPLOYMENT.md's verification section for how they
#     were seeded (a one-off ECS task, same pattern as running migrations).
#   - The local dev JWT keypair (dev_keys/) matching whatever public key
#     was pushed to this deployment's SSM JWT_PUBLIC_KEY_PEM parameter.
#
# Usage: scripts/aws_demo.sh [ALB_URL]
#   ALB_URL defaults to `terraform output -raw alb_dns_name` if omitted.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- Configuration: the two demo customers, seeded ahead of time ---
CUSTOMER_A_ID="${CUSTOMER_A_ID:-501}"
CUSTOMER_B_ID="${CUSTOMER_B_ID:-502}"
CUSTOMER_A_ORDER_REF="${CUSTOMER_A_ORDER_REF:-aws-demo-order-a}"
CUSTOMER_B_ORDER_REF="${CUSTOMER_B_ORDER_REF:-aws-demo-order-b}"

ALB_URL="${1:-}"
if [ -z "$ALB_URL" ]; then
    ALB_URL="http://$(terraform -chdir=infra/terraform output -raw alb_dns_name)"
fi

PAUSE="${DEMO_PAUSE:-3}"

pretty() {
    if command -v jq >/dev/null 2>&1; then
        jq .
    else
        python3 -m json.tool
    fi
}

section() {
    echo
    echo "================================================================================"
    echo "  $1"
    echo "================================================================================"
    sleep "$PAUSE"
}

pause() {
    sleep "$PAUSE"
}

section "dealership-agent — live AWS deployment demo"
echo "Target: $ALB_URL"
echo
echo "This is a real ECS Fargate service behind a real ALB, backed by a"
echo "real RDS Postgres+pgvector instance in a private subnet, calling a"
echo "real AWS Bedrock model - not a local demo."
pause

section "1. Liveness and readiness"
echo "GET /healthz  (process liveness only - no dependency checks)"
curl -sS "$ALB_URL/healthz" | pretty
pause
echo "GET /readyz   (checks the database AND the MCP tool-server subprocess)"
curl -sS "$ALB_URL/readyz" | pretty
pause

section "2. Public catalog search — no login required"
echo "GET /listings?query=reliable+family+SUV"
curl -sS "$ALB_URL/listings?query=reliable+family+SUV&limit=3" | pretty
pause

echo
echo "Minting a JWT for the demo customer (locally, using the same"
echo "keypair whose public half was pushed to this deployment's SSM"
echo "parameter store)..."
TOKEN_A=$(uv run python scripts/mint_dev_token.py --customer-id "$CUSTOMER_A_ID" --skip-db-check 2>/dev/null | tail -1)
pause

section "3. Authenticated order lookup"
echo "POST /chat as customer $CUSTOMER_A_ID, asking about their own order"
curl -sS -X POST "$ALB_URL/chat" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN_A" \
    -d '{"message": "Where is my order?"}' | pretty
pause

section "4. THE SECURITY BOUNDARY — the single most important thing here"
echo "Same customer ($CUSTOMER_A_ID), asking for a DIFFERENT customer's"
echo "order ref ($CUSTOMER_B_ORDER_REF, belongs to customer $CUSTOMER_B_ID)."
echo
echo "The tool schema the model sees has no customer_id field at all -"
echo "there is no field for the model to fill in with someone else's ID."
echo "Row-Level Security enforces the same boundary again, independently,"
echo "at the database layer."
pause
curl -sS -X POST "$ALB_URL/chat" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN_A" \
    -d "{\"message\": \"Can you show me the details for order $CUSTOMER_B_ORDER_REF?\"}" | pretty
pause

section "5. The action-claim verifier"
echo "Asking for something no tool in this system can actually do -"
echo "there is no cancel_order tool anywhere. Watch for a reply that"
echo "does NOT falsely claim the cancellation happened."
pause
curl -sS -X POST "$ALB_URL/chat" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN_A" \
    -d '{"message": "Please cancel my order"}' | pretty
pause

section "Demo complete"
echo "Every response above came from the live deployment at $ALB_URL —"
echo "real Bedrock calls, real Postgres, real MCP subprocess, real RLS."
