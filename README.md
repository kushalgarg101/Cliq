# ParcelPilot Support Agent

Run with the supplied pack in place:

```powershell
Copy-Item 'C:\Users\Kusha\Downloads\AI Agent Assessment - Candidate Pack-20260823T120019Z-1-001.zip' 'data\AI Agent Assessment - Candidate Pack.zip'
Copy-Item .env.example .env
C:\Users\Kusha\.local\bin\uv.exe sync --all-groups
$env:PYTHONPATH = 'src'
C:\Users\Kusha\.local\bin\uv.exe run uvicorn parcelpilot.main:app --reload
```

Open `http://127.0.0.1:8000`. Assessment users are listed on the sign-in page; their shared demo password is `parcelpilot-demo`.

For a hosted deployment, set `APP_COOKIE_SECURE=true`, use HTTPS, and configure a tool-capable OpenAI-compatible provider with that provider's key in `LLM_API_KEY`, its endpoint in `LLM_BASE_URL`, and its model in `LLM_MODEL`. `LLM_MODE=provider` is recommended for full conversational chat; `auto` is an explicitly labelled limited deterministic fallback when no key is available.

Source data is ingested into `var/source.db` on first launch and fingerprinted against the ZIP. If the ZIP changes, the app fails closed rather than serving stale policy. To intentionally refresh it, stop the server, delete only `var/source.db`, then restart the app. This avoids replacing a live SQLite file on Windows.

Run checks with `uv run pytest`, `uv run ruff check src tests`, and `uv lock --check`.
