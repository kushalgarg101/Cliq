# Deploying the assessment demo on Render

This repository includes the supplied assessment ZIP at `data/AI Agent Assessment - Candidate Pack.zip`. It is necessary for the hosted demo: the application ingests it into a local SQLite database at first start. Do not make the repository public or reuse this packaging approach for real customer data.

## What is ready in the repository

- `Dockerfile` builds and starts the FastAPI application on Render's `PORT`.
- `render.yaml` declares a Docker web service and a `/healthz` health check.
- The health check fails if the assessment evidence pack is unavailable.
- Provider secrets are marked `sync: false`, so the Blueprint never commits or copies them.
- The web app requires secure cookies when deployed through HTTPS.

## Your Render steps

1. Sign in to [Render](https://dashboard.render.com/) with your personal account and connect the private GitHub repository `kushalgarg101/Cliq`.
2. Select **New** → **Blueprint**, choose the `main` branch, and create the `parcelpilot-support-agent` service from `render.yaml`.
3. In the service's **Environment** settings, set all three values below. Do not add provider secrets to GitHub.

   ```text
   LLM_API_KEY=<your provider key>
   LLM_BASE_URL=https://openrouter.ai/api/v1
   LLM_MODEL=<a currently available tool-capable model>
   ```

   Leave `LLM_MODE=auto` for a resilient demo: the system transparently uses its limited deterministic workflow if a provider is unavailable. Set `LLM_MODE=provider` only when you want chat disabled rather than falling back if the provider fails.
4. Deploy and wait for Render to report `/healthz` as healthy. Open the generated `https://…onrender.com` URL.
5. Sign in with `maya` / `parcelpilot-demo`. Verify the order, action-confirmation, and Operations Lead demo flows described below.

## Acceptance checks after deploy

1. `GET /healthz` returns `{"status":"ok","data_ready":true}`.
2. As `northstar`, ask: `Can Northstar cancel ORD-1001 without a cancellation fee?` It must cite the Northstar agreement and report a 0 INR fee.
3. As `maya`, ask: `Please escalate TKT-501 immediately.` Review the pending action then select **Confirm action**.
4. As `opslead`, load the proactive insights panel. It must show prioritised cross-account signals.
5. As `northstar`, confirm that an Axis or LumenWorks order is denied rather than disclosed.

## Assessment-demo operational limits

Render's free web-service filesystem is ephemeral. The data ZIP is bundled into the image, so every fresh instance can ingest it; however, mock action history and sessions reset after a restart or redeploy. This is acceptable for the assessment demo. A production deployment should move runtime state to a managed database and keep documents in controlled object storage. Render persistent disks require a paid web-service plan and are not a substitute for multi-instance database storage.

If Render asks for repository access, grant access only to this private repository. Rotate any provider credential that was pasted into chat before recording or sharing the demo.