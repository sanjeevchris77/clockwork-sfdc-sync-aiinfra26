# AI Infra Summit 2026 — Salesforce sync setup

Files here mirror `clockwork-sfdc-sync`'s template, pointed at AI Infra Summit 2026.

## What to do with these files

1. Copy this whole folder's contents into `clockwork-sfdc-sync-aiinfra26` (the repo you already created), preserving the paths:
   - `.github/workflows/sync.yml`
   - `scripts/sync_salesforce.py` (unmodified copy of the RAISE template's script)
   - `events/aiinfra26/config.json` (new — built from your answers)
2. Commit and push.
3. Add repo secrets (Settings → Secrets and variables → Actions → New repository secret):
   - `SF_LOGIN_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` — same values as the RAISE repo if it's the same Salesforce org/External Client App, otherwise a fresh App. I never see or handle these — add them directly in GitHub.
4. Go to Actions tab → "Sync Salesforce data" → "Run workflow" to trigger the first sync manually. After that it runs automatically every 6 hours.
5. Confirm `data/leads.json`, `data/opportunities.json`, `data/account_summary.json`, `data/meta.json` show up with real (non-zero) counts.

## Known quirk carried over from the RAISE template

`scripts/sync_salesforce.py` unconditionally runs a "find all Campaigns named %Raise%" discovery query and writes `data/campaigns_raise.json` + `data/year_comparison.json`, regardless of event. For AI Infra Summit this is harmless dead weight — it'll just find RAISE's own campaigns (unrelated to this event) and produce a near-empty year_comparison.json since `comparison_campaign_id` is blank in the new config. The AI Infra dashboard won't reference either file. Not worth a code change right now, but flagging so it doesn't look like a bug if you notice it in the output.

## Once real data lands

Ping me — I'll pull `data/*.json` from this repo via the GitHub connector (read-only access works fine; I just can't push) and build the funnel/segment/persona/tier charts into the AI Infra dashboard using the same layout as `raise-2026.html`.
