#!/usr/bin/env bash
# Deploy the triage agent to Cloud Run with traces going to Cloud Trace.
#
# Cloud Run scales to zero, but model calls use billed Vertex AI. This keeps a
# deployed service separate from the AI Studio quota used by local demo runs.
#
# TEARDOWN is deliberately not scripted here. Run it yourself when done:
#   gcloud run services delete issue-triage --region australia-southeast1 \
#     --project "$PROJECT"
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-australia-southeast1}"
SERVICE="${SERVICE:-issue-triage}"

case "${1:-deploy}" in

  enable)
    gcloud services enable \
      run.googleapis.com cloudtrace.googleapis.com aiplatform.googleapis.com \
      cloudbuild.googleapis.com artifactregistry.googleapis.com compute.googleapis.com \
      --project "$PROJECT"
    number=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
    runtime="serviceAccount:${number}-compute@developer.gserviceaccount.com"
    gcloud projects add-iam-policy-binding "$PROJECT" \
      --member="$runtime" --role=roles/aiplatform.user >/dev/null
    gcloud projects add-iam-policy-binding "$PROJECT" \
      --member="$runtime" --role=roles/cloudtrace.agent >/dev/null
    ;;

  deploy)
    # --adk_version defaults to an older release than the one developed against,
    # which would deploy an agent that behaves differently from local. Pin it.
    adk_version="$(uv run python -c 'import google.adk; print(google.adk.__version__)')"
    # --no-allow-unauthenticated: a public URL would let anyone create billed
    # Vertex AI traffic.
    # GOOGLE_CLOUD_PROJECT: --trace_to_cloud only registers the exporter when this
    # is set. Without it the service starts, logs one warning, and traces nothing.
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
      --set-env-vars="GOOGLE_GENAI_USE_ENTERPRISE=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global"
    ;;

  url)
    gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" \
      --format='value(status.url)'
    ;;

  traces)
    echo "https://console.cloud.google.com/traces/list?project=${PROJECT}"
    ;;

  *)
    echo "usage: $0 {enable|deploy|url|traces}" >&2
    echo "teardown is manual — see the header of this file" >&2
    exit 1
    ;;
esac
