#!/bin/sh
# Step 12: the deployed environment has no real production identity
# provider (see docs/DEPLOYMENT.md's disclosed JWT follow-up) - this
# writes an SSM-sourced public key (JWT_PUBLIC_KEY_PEM, injected as a
# plain env var via ECS `secrets`) out to the file path JWT_PUBLIC_KEY_PATH
# expects, before the real app starts. A no-op locally/in CI, where this
# env var is never set.
set -e

if [ -n "$JWT_PUBLIC_KEY_PEM" ] && [ -n "$JWT_PUBLIC_KEY_PATH" ]; then
    mkdir -p "$(dirname "$JWT_PUBLIC_KEY_PATH")"
    printf '%s' "$JWT_PUBLIC_KEY_PEM" > "$JWT_PUBLIC_KEY_PATH"
fi

exec "$@"
