#!/usr/bin/env python3
"""
Roll up HubSpot Contact lifecycle stages to the Company (account) level into a
"Marketing Qualified Account" (MQA) view, then join each Company to its real
Salesforce Account by domain -- so the dashboard can show, per account: is it
an MQA, how many contacts drove that, and what its authoritative Salesforce
ABM tier / owner are.

This script is STANDALONE from sync_salesforce.py on purpose (separate auth
scopes, separate schedule, separate failure domain) even though it duplicates
a couple of small helpers (get_access_token, soql, domain_of). If HubSpot
sync breaks, the core Salesforce-fed dashboard keeps working untouched.

Why "MQA" is computed this way (per direction from the dashboard owner):
  - There is no native numeric lead-scoring field in this HubSpot portal
    (confirmed: `hubspotscore` does not exist here). The de facto scoring
    methodology IS the lifecycle-stage funnel itself.
  - Rule: a Company becomes an MQA the moment ANY associated Contact reaches
    lifecyclestage Marketing Qualified Lead (MQL) or later -- Sales Accepted
    Lead, SQL, Opportunity, Customer. This is intentionally a ratchet: once a
    company crosses the MQL line via any one contact, it stays an MQA even if
    that contact later reverts, because the account-level signal ("someone
    here engaged enough to qualify") doesn't un-happen.
  - There is no explicit Salesforce Account Id field exposed on HubSpot
    Company records in this portal. The reliable join key confirmed live is
    Company.domain (HubSpot) <-> Account.Website domain (Salesforce) -- the
    exact same domain-matching technique sync_salesforce.py already uses
    throughout for Lead/Opportunity/Account matching.

Why there's no per-contact "activity trail" here: a live check of this
portal found that ~98% of MQL+ contacts have hs_analytics_source = OFFLINE
and zero form conversions -- meaning qualification here comes from bulk
CRM sync/import criteria, not tracked marketing engagement (page views,
form fills, email clicks). Building a rich activity feed from that data
would mostly be empty and misleading. Instead, this script surfaces the two
signals that ARE reliably populated for the account's best (highest-stage)
contact: the date it entered its current qualifying stage
(hs_v2_date_entered_marketingqualifiedlead, "qualified_since") and the most
recent logged activity of any kind (notes_last_updated, "last_activity_date")
-- an honest proxy for "is anyone actually engaging with this account,"
not a marketing-attribution story.

Required env vars:
  HUBSPOT_ACCESS_TOKEN   Private App access token, scoped to read
                          crm.objects.contacts and crm.objects.companies.
                          If unset, this script prints a notice and exits 0
                          (non-fatal) so it can be added to the same
                          workflow run as the Salesforce sync without
                          breaking anything before the token exists.
  SF_LOGIN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET
                          Same Salesforce External Client App credentials
                          sync_salesforce.py already uses, reused here only
                          to pull ABM-tiered Accounts for the domain join.

Optional env var:
  EVENT_CONFIG           Same event config JSON used by sync_salesforce.py,
                          reused here only for `abm_tier_field`,
                          `sfdc_lightning_domain`, and `output_dir` so the
                          two scripts stay pointed at the same org/output
                          location. Defaults mirror sync_salesforce.py's own
                          defaults if unset or a key is missing.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, parse, error

REPO_ROOT = Path(__file__).resolve().parent.parent

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN")
if not HUBSPOT_TOKEN:
    print("HUBSPOT_ACCESS_TOKEN not set -- skipping HubSpot MQA sync (non-fatal). "
          "Create a HubSpot Private App with read access to Contacts and "
          "Companies, then add its token as this repo's HUBSPOT_ACCESS_TOKEN "
          "secret to enable this.", file=sys.stderr)
    sys.exit(0)

# --- Reuse the same event config the Salesforce sync uses, just for the
# handful of values relevant here. Falls back to sensible defaults so this
# script still runs even if EVENT_CONFIG isn't set.
CONFIG_PATH = REPO_ROOT / os.environ.get("EVENT_CONFIG", "events/raise2026/config.json")
try:
    CONFIG = json.loads(CONFIG_PATH.read_text())
except FileNotFoundError:
    CONFIG = {}

ABM_TIER_FIELD = CONFIG.get("abm_tier_field", "ABM_Tier__c")
SFDC_LIGHTNING_DOMAIN = CONFIG.get("sfdc_lightning_domain", "https://clockworksystems.lightning.force.com")
OUT_DIR = REPO_ROOT / CONFIG.get("output_dir", "data")

HUBSPOT_API_BASE = "https://api.hubapi.com"

# --- Lifecycle stage ranking. HubSpot returns either the human-readable
# internal name (e.g. "marketingqualifiedlead") or, for stages that were
# renamed/customized in this portal, a numeric string id (confirmed live:
# 1426326230 = Marketing Engaged Lead, 1426129651 = Sales Accepted Lead).
# Extend this map if the portal adds more custom lifecycle stages later --
# unrecognized values rank as -1 (treated as "no signal", never an MQA on
# their own).
LIFECYCLE_RANK = {
    "subscriber": 0,
    "lead": 0,
    "1426326230": 1,        # Marketing Engaged Lead (custom)
    "marketingqualifiedlead": 2,   # MQL
    "1426129651": 3,        # Sales Accepted Lead (custom)
    "salesqualifiedlead": 4,       # SQL
    "opportunity": 5,
    "customer": 6,
    "evangelist": 6,
    "other": 0,
}
LIFECYCLE_LABEL = {
    "subscriber": "Subscriber",
    "lead": "Lead",
    "1426326230": "Marketing Engaged Lead",
    "marketingqualifiedlead": "Marketing Qualified Lead (MQL)",
    "1426129651": "Sales Accepted Lead",
    "salesqualifiedlead": "Sales Qualified Lead (SQL)",
    "opportunity": "Opportunity",
    "customer": "Customer",
    "evangelist": "Evangelist",
    "other": "Other",
}
# A Company is an MQA once its BEST contact's rank is >= this threshold.
MQA_RANK_THRESHOLD = LIFECYCLE_RANK["marketingqualifiedlead"]

COMPANY_PROPERTIES = ["name", "domain", "industry", "abm_tier", "icp_account", "hs_is_target_account"]


def lifecycle_rank(stage):
    return LIFECYCLE_RANK.get((stage or "").lower(), -1)


def lifecycle_label(stage):
    return LIFECYCLE_LABEL.get((stage or "").lower(), stage or "(none)")


def hs_ms_to_iso(ms_str):
    """HubSpot timestamp properties come back as a string of milliseconds
    since epoch. Returns an ISO 8601 string, or None if unset/unparseable."""
    if not ms_str:
        return None
    try:
        return datetime.fromtimestamp(int(ms_str) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Salesforce helpers (duplicated from sync_salesforce.py -- see module
# docstring for why this script stays standalone).
# ---------------------------------------------------------------------------

def get_sf_access_token():
    login_url = os.environ["SF_LOGIN_URL"]
    client_id = os.environ["SF_CLIENT_ID"]
    client_secret = os.environ["SF_CLIENT_SECRET"]
    url = f"{login_url}/services/oauth2/token"
    payload = parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = request.Request(url, data=payload, method="POST")
    with request.urlopen(req) as resp:
        data = json.loads(resp.read())
        return data["access_token"], data["instance_url"]


def soql(instance_url, token, query):
    records = []
    url = f"{instance_url}/services/data/v60.0/query/?q={parse.quote(query)}"
    while url:
        req = request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with request.urlopen(req) as resp:
            data = json.loads(resp.read())
        records.extend(data["records"])
        next_url = data.get("nextRecordsUrl")
        url = f"{instance_url}{next_url}" if next_url else None
    return records


def domain_of(url):
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^[a-z]+://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("/")[0]
    u = u.split("?")[0].split("#")[0]
    u = u.split(":")[0]
    return u or None


def pull_all_tiered_sf_accounts(instance_url, token, tier_field):
    query = f"""
        SELECT Id, Name, Website, OwnerId, Owner.Name, {tier_field}
        FROM Account
        WHERE {tier_field} != null AND Website != null
    """
    return soql(instance_url, token, query)


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def hubspot_request(path, method="GET", body=None, max_retries=4):
    """Minimal HubSpot REST client: bearer auth, JSON in/out, basic retry on
    429 (rate limit) and 5xx with linear backoff. Raises on other errors."""
    url = f"{HUBSPOT_API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    for attempt in range(max_retries):
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req) as resp:
                return json.loads(resp.read())
        except error.HTTPError as e:
            body_text = e.read().decode()
            if e.code == 429 or e.code >= 500:
                wait = 2 * (attempt + 1)
                print(f"HubSpot {method} {path} -> {e.code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"HubSpot request failed: {method} {path} -> {e.code}: {body_text}", file=sys.stderr)
            raise
    raise RuntimeError(f"HubSpot request exhausted retries: {method} {path}")


CONTACT_PROPERTIES = ["lifecyclestage", "firstname", "lastname", "jobtitle", "email",
                       "hs_v2_date_entered_marketingqualifiedlead", "notes_last_updated"]


def pull_contacts_with_company_associations():
    """Paginates ALL Contacts, pulling lifecyclestage plus a handful of
    identity/activity properties, and their associated Company ids. This is
    the expensive direction (potentially many pages for a large portal) but
    it's the only reliable way to compute a per-company MAX lifecycle stage
    without a native rollup field -- runs on a schedule, not in the request
    path of anything user-facing, so pagination cost is acceptable.
    Returns a dict: {company_id: [contact_dict, ...]}, where each
    contact_dict has stage/name/title/email/mql_date_ms/last_activity_ms."""
    company_contacts = {}
    after = None
    page = 0
    while True:
        page += 1
        qs = {
            "limit": "100",
            "properties": ",".join(CONTACT_PROPERTIES),
            "associations": "companies",
        }
        if after:
            qs["after"] = after
        path = f"/crm/v3/objects/contacts?{parse.urlencode(qs)}"
        data = hubspot_request(path)
        for contact in data.get("results", []):
            props = contact.get("properties") or {}
            info = {
                "stage": props.get("lifecyclestage"),
                "name": f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip() or None,
                "title": props.get("jobtitle"),
                "email": props.get("email"),
                "mql_date_ms": props.get("hs_v2_date_entered_marketingqualifiedlead"),
                "last_activity_ms": props.get("notes_last_updated"),
            }
            company_ids = [
                r["id"] for r in
                ((contact.get("associations") or {}).get("companies") or {}).get("results", [])
            ]
            for cid in company_ids:
                company_contacts.setdefault(cid, []).append(info)
        print(f"  contacts page {page}: {len(data.get('results', []))} contacts, "
              f"{len(company_contacts)} distinct companies seen so far", file=sys.stderr)
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return company_contacts


def pull_companies_by_id(company_ids):
    """Batch-reads Company properties for a list of HubSpot Company ids, 100
    at a time (HubSpot batch API limit)."""
    results = {}
    ids = list(company_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        body = {
            "properties": COMPANY_PROPERTIES,
            "inputs": [{"id": cid} for cid in chunk],
        }
        data = hubspot_request("/crm/v3/objects/companies/batch/read", method="POST", body=body)
        for c in data.get("results", []):
            results[c["id"]] = c.get("properties") or {}
    return results


def build_hubspot_mqa(sf_instance_url, sf_token):
    company_contacts = pull_contacts_with_company_associations()
    if not company_contacts:
        print("No Contact-to-Company associations found -- nothing to roll up.", file=sys.stderr)
        return None

    companies_props = pull_companies_by_id(company_contacts.keys())

    # --- Salesforce side of the join: all ABM-tiered Accounts with a
    # Website, keyed by domain (same technique as sync_salesforce.py).
    sf_accounts = pull_all_tiered_sf_accounts(sf_instance_url, sf_token, ABM_TIER_FIELD)
    sf_by_domain = {}
    for a in sf_accounts:
        d = domain_of(a.get("Website"))
        if d and d not in sf_by_domain:  # first tiered account wins on a domain collision
            sf_by_domain[d] = a

    rows = []
    for company_id, contacts in company_contacts.items():
        props = companies_props.get(company_id, {})
        ranks = [lifecycle_rank(c["stage"]) for c in contacts]
        best_rank = max(ranks) if ranks else -1
        # Best contact = highest-ranked; tie-break on most recently entering
        # that stage (a later mql_date_ms wins), so "best_contact" points at
        # whichever real person most recently drove the qualification.
        best_idx = max(
            range(len(contacts)),
            key=lambda i: (ranks[i], contacts[i].get("mql_date_ms") or "0"),
        ) if contacts else None
        best = contacts[best_idx] if best_idx is not None else {}
        mql_plus_count = sum(1 for r in ranks if r >= MQA_RANK_THRESHOLD)

        domain = domain_of(props.get("domain"))
        sf_acct = sf_by_domain.get(domain) if domain else None

        rows.append({
            "hubspot_company_id": company_id,
            "name": props.get("name"),
            "domain": props.get("domain"),
            "industry": props.get("industry"),
            "hubspot_abm_tier": props.get("abm_tier"),
            "hubspot_icp_account": props.get("icp_account"),
            "hubspot_target_account": props.get("hs_is_target_account"),
            "contact_count": len(contacts),
            "highest_lifecyclestage": lifecycle_label(best.get("stage")),
            "mql_plus_contact_count": mql_plus_count,
            "is_mqa": best_rank >= MQA_RANK_THRESHOLD,
            "best_contact_name": best.get("name"),
            "best_contact_title": best.get("title"),
            "best_contact_email": best.get("email"),
            "qualified_since": hs_ms_to_iso(best.get("mql_date_ms")),
            "last_activity_date": hs_ms_to_iso(best.get("last_activity_ms")),
            "sfdc_matched": sf_acct is not None,
            "sfdc_account_id": sf_acct["Id"] if sf_acct else None,
            "sfdc_tier": (sf_acct.get(ABM_TIER_FIELD) if sf_acct else None) or ("Untiered" if sf_acct else None),
            "sfdc_owner": ((sf_acct.get("Owner") or {}).get("Name") if sf_acct else None),
            "account_sfdc_link": (
                f"{SFDC_LIGHTNING_DOMAIN}/lightning/r/Account/{sf_acct['Id']}/view" if sf_acct else None
            ),
        })

    rows.sort(key=lambda r: (not r["is_mqa"], -r["mql_plus_contact_count"]))

    mqa_rows = [r for r in rows if r["is_mqa"]]
    tier_breakdown = {}
    for r in mqa_rows:
        tier = r["sfdc_tier"] or "Not Matched to Salesforce"
        tier_breakdown[tier] = tier_breakdown.get(tier, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": (
            "A Company is a Marketing Qualified Account (MQA) once at least one "
            "associated Contact has reached HubSpot lifecyclestage 'Marketing "
            "Qualified Lead' or later (Sales Accepted Lead, SQL, Opportunity, "
            "Customer) -- this portal has no native numeric lead-scoring field, "
            "so the lifecycle-stage funnel IS the scoring methodology. Matched "
            "to Salesforce Accounts by domain (Company.domain <-> "
            "Account.Website), the same technique used elsewhere in this "
            "pipeline -- accounts with no Website or no domain match show as "
            "'Not Matched to Salesforce'. 'best_contact' fields identify the "
            "single contact whose stage drove the MQA status (highest stage, "
            "ties broken by most recently qualified). 'qualified_since' is "
            "when that contact entered its current stage; 'last_activity_date' "
            "is the most recent logged activity of any kind on that contact. "
            "Note: a live check of this portal found ~98% of MQL+ contacts "
            "have no tracked marketing engagement (no form conversions, "
            "hs_analytics_source = OFFLINE) -- qualification here comes "
            "predominantly from bulk CRM sync/import criteria, not organic "
            "funnel activity, so these two fields are the closest reliable "
            "'is anyone engaging with this account' signal available, not a "
            "marketing-attribution trail."
        ),
        "summary": {
            "companies_with_contacts": len(rows),
            "mqa_count": len(mqa_rows),
            "sfdc_matched_count": sum(1 for r in rows if r["sfdc_matched"]),
            "sfdc_matched_mqa_count": sum(1 for r in mqa_rows if r["sfdc_matched"]),
            "tier_breakdown": tier_breakdown,
        },
        "companies": rows,
    }


def main():
    sf_token, sf_instance_url = get_sf_access_token()
    result = build_hubspot_mqa(sf_instance_url, sf_token)
    if result is None:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "hubspot_mqa.json").write_text(json.dumps(result, indent=2))
    print(f"Wrote {OUT_DIR / 'hubspot_mqa.json'}: "
          f"{result['summary']['mqa_count']} MQAs out of "
          f"{result['summary']['companies_with_contacts']} companies with contacts, "
          f"{result['summary']['sfdc_matched_mqa_count']} matched to Salesforce Accounts.")


if __name__ == "__main__":
    main()
