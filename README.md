# clockwork-sfdc-sync

Scheduled GitHub Action that pulls an event's booth-scan Leads and rep-owned
Opportunities out of Salesforce and commits them as JSON into `data/`, so
they can be read by other tools (e.g. Claude via the GitHub connector)
without giving anyone direct Salesforce credentials.

The sync logic (`scripts/sync_salesforce.py`) is event-generic: everything
specific to this event (which Campaigns, which reps, the event's start date,
etc.) lives in `events/raise2026/config.json`, not in the script itself.

**Each event gets its own pair of repos and its own link, not a shared one.**
This repo (`clockwork-sfdc-sync`) and its paired dashboard repo
(`ClockWorkRAISESUMMIT`) are both marked as GitHub template repositories, so
a new event is spun up by cloning both as fresh, independent repos -- own
git history, own secrets, own Netlify site, own URL. See "Adding a new
event" below.

## One-time setup

1. **Add repo secrets** (Settings -> Secrets and variables -> Actions -> New
   repository secret), using the "Clockwork SFDC Sync" External Client App
   and its Client Credentials Flow:
   - `SF_CLIENT_ID` -- External Client App Consumer Key
   - `SF_CLIENT_SECRET` -- External Client App Consumer Secret
   - `SF_LOGIN_URL` -- your org's My Domain URL, e.g.
     `https://yourorg.my.salesforce.com` (must be the specific My Domain,
     not the generic login.salesforce.com -- Client Credentials Flow
     requires it)

   None of these are ever seen by Claude or committed to the repo -- they
   live only in GitHub's encrypted secrets store and are injected as env
   vars at Action runtime.

   Before this works, the External Client App also needs a "Run As" user
   set under its Policies tab -> Client Credentials Flow section -- that
   user's permissions determine what Leads/Opportunities this script can
   see.

2. **Run the workflow once manually**: go to the Actions tab -> "Sync
   Salesforce data" -> "Run workflow". This creates the first `data/*.json`
   files.

3. After that it re-runs automatically every 6 hours (edit the cron
   schedule in `.github/workflows/sync.yml` to change frequency), and can
   always be re-triggered manually.

## What gets synced

- `data/leads.json` -- every Lead campaign-member on the event's booth-scan
  campaigns, with owner, company, title, email, mobile, LinkedIn (if the
  `LinkedIn__c` field exists on Lead -- rename in the script if your org
  uses a different API name), Lead Source, Notes, and a best-effort
  MQL/MEL lifecycle classification when Status isn't already set.
- `data/opportunities.json` -- Opportunities on those campaigns owned by
  this event's tracked reps, with Amount (defaulting to the config's
  `default_opp_amount` if blank), Stage, Created Date, a Net New/Existing
  `origin` classification, and associated contact roles.
- `data/by_rep.json` -- the same data pre-split per rep.
- `data/account_summary.json` -- one row per company seen at the booth:
  breadth (persona count), depth (lifecycle stages), prior-engagement flags,
  potential pipeline, and ABM tier (if the config's `abm_tier_field` is set
  on Account).
- `data/meta.json` -- campaign IDs, rep names, and roll-up counts for a
  quick sanity check.
- `data/campaigns_raise.json`, `data/year_comparison.json`,
  `data/raise2025_booth_leads.json` -- year-over-year comparison against
  the config's `comparison_campaign_id`.

## Adding a new event

Each event is a fully independent pair of repos -- its own sync repo, own
dashboard repo, own Netlify site, own link. Nothing about a new event can
accidentally affect RAISE 2026's live site or data.

1. **Mark both repos as GitHub templates** (one-time, already done for
   `clockwork-sfdc-sync` and `ClockWorkRAISESUMMIT`): repo Settings ->
   check "Template repository".
2. **Spin up the new sync repo**: on `clockwork-sfdc-sync`, click "Use this
   template" -> "Create a new repository", name it for the new event (e.g.
   `clockwork-sfdc-sync-<event>`). In the new repo:
   - Edit `events/raise2026/config.json` (rename the folder to
     `events/<new-name>/` if you like, or just edit the file in place --
     since this repo now serves only this one event, the exact path
     doesn't matter) with the new event's `campaign_ids`, `rep_names`,
     `default_opp_amount`, `qualified_stages`, `comparison_campaign_id`,
     `event_start_date`, `net_new_lead_source`, `abm_tier_field` (ask the
     user for the exact Account field API name -- it varies per org and
     isn't guessable), and `ad_hoc_report_id`.
   - Add that repo's own `SF_LOGIN_URL` / `SF_CLIENT_ID` / `SF_CLIENT_SECRET`
     secrets (same Salesforce org's External Client App, or a different
     one, as needed).
   - Run the "Sync Salesforce data" workflow once manually to produce the
     first `data/*.json` files.
3. **Spin up the new dashboard repo**: on `ClockWorkRAISESUMMIT`, click "Use
   this template" -> "Create a new repository", name it for the new event.
   Build that event's dashboard HTML from the new sync repo's `data/*.json`
   files, following the same chart/table conventions as the existing
   dashboard (KPI cards, engagement pie chart, persona funnel, Opportunity
   table, etc.) -- classification-heavy charts (like company segment or ABM
   tier) still need a fresh human decision on categories, same as they did
   for RAISE 2026. Push it into the new repo.
4. **Connect a new Netlify site**: Netlify -> Add new site -> Import from
   Git -> pick the new dashboard repo. Netlify assigns a fresh, unique link
   for it automatically, entirely separate from RAISE 2026's.

## Notes / caveats

- Auth uses the OAuth 2.0 Client Credentials flow via a Salesforce External
  Client App -- simpler than the old Username-Password flow since it only
  needs a Client ID + Secret, but it authenticates as a specific "Run As"
  user configured on the app, so that user needs read access to the
  relevant Leads/Opportunities. If the Action fails with
  `invalid_client_id` or similar, double check `SF_LOGIN_URL` is the org's
  exact My Domain URL, and that "Enable Client Credentials Flow" is checked
  and saved on the app's Settings tab.
- The lifecycle classification (MQL vs MEL) from the Notes field is a
  simple keyword heuristic -- review it before trusting it fully. If your
  org already sets Lead Status accurately, that value is used as-is instead.
- All event-specific values now live in `events/<name>/config.json`, loaded
  via the `EVENT_CONFIG` env var (defaults to `events/raise2026/config.json`
  for backward compatibility). Nothing event-specific should need to change
  in `scripts/sync_salesforce.py` itself.
