#!/usr/bin/env bash
# Deploy the triage agent to Cloud Run with traces going to Cloud Trace.
#
# Cost posture: Cloud Run scales to zero and a demo service sits inside the
# always-free allowance. Model calls still go to the AI Studio free tier via the
# key in Secret Manager — Vertex AI is deliberately NOT used, because it has no
# free tier and this project has billing enabled.
#
# TEARDOWN is deliberately not scripted here. Run it yourself when done:
#   gcloud run services delete issue-triage --region australia-southeast1 \
#     --project "$PROJECT"
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-australia-southeast1}"
SERVICE="${SERVICE:-issue-triage}"
SECRET="${SECRET:-gemini-api-key}"

case "${1:-deploy}" in

  enable)
    gcloud services enable \
      run.googleapis.com cloudtrace.googleapis.com secretmanager.googleapis.com \
      cloudbuild.googleapis.com artifactregistry.googleapis.com \
      --project "$PROJECT"
    ;;

  secret)
    # Push the local key into Secret Manager without it appearing in argv.
    key=$(grep '^GOOGLE_API_KEY=' issue_triage/.env | cut -d= -f2-)
    [ -n "$key" ] || { echo "no GOOGLE_API_KEY in issue_triage/.env" >&2; exit 1; }
    if gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
      printf '%s' "$key" | gcloud secrets versions add "$SECRET" --data-file=- --project "$PROJECT"
    else
      printf '%s' "$key" | gcloud secrets create "$SECRET" --data-file=- --project "$PROJECT"
    fi
    ;;

  deploy)
    # --adk_version defaults to an older release than the one developed against,
    # which would deploy an agent that behaves differently from local. Pin it.
    adk_version="$(uv run python -c 'import google.adk; print(google.adk.__version__)')"
    # --no-allow-unauthenticated: a public URL would let anyone spend the API key's quota.
    uv run adk deploy cloud_run issue_triage \
      --project "$PROJECT" \
      --region "$REGION" \
      --service_name "$SERVICE" \
      --adk_version "$adk_version" \
      --with_ui \
      --trace_to_cloud \
      -- \
      --min-instances=0 \
      --no-allow-unauthenticated \
      --set-secrets="GOOGLE_API_KEY=${SECRET}:latest" \
      --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=FALSE"
    ;;

  url)
    gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" \
      --format='value(status.url)'
    ;;

  traces)
    echo "https://console.cloud.google.com/traces/list?project=${PROJECT}"
    ;;

  *)
    echo "usage: $0 {enable|secret|deploy|url|traces}" >&2
    echo "teardown is manual — see the header of this file" >&2
    exit 1
    ;;
esac
