import os
import json
import requests
from datetime import datetime
from anthropic import Anthropic

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

hs_headers = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type":  "application/json"
}
claude = Anthropic(api_key=ANTHROPIC_KEY)

# ── Config ────────────────────────────────────────────────────────────

DEAL_STAGES = [
    "Consultation Requested",
    "Consultation Booked",
    "Consultation Completed",
    "Pitch Booked",
    "Pitch Completed",
]

CREATE_DATE_AFTER_MS = int(
    datetime(2026, 1, 1).timestamp() * 1000
)

DETECTION_PROMPT = """
You are analysing a CRM activity (call, note, or email) from a sales team.

Determine if there is ANY discussion, mention, or reference to:
- Pricing, cost, fees, rates, or budget
- Quotes, proposals, or investment figures
- Discounts, contracts, or billing terms
- Indirect references like "let's talk numbers", "what would it take 
  commercially", "sent the proposal over", "what's the damage", 
  "monthly investment", "budget approval", or "flat fee"

Be context-aware — a call titled "Proposal Call" or a note mentioning 
a specific dollar amount ($X,XXX/month or $XX,XXX project) should 
always be flagged.

Respond ONLY with valid JSON, no preamble, no markdown:
{"pricing_mentioned": true/false, "reason": "one concise sentence — what was discussed, which activity type, approximate date if available"}
"""

# ── Fetch eligible deals ──────────────────────────────────────────────

def get_eligible_deals():
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    payload = {
        "filterGroups": [{
            "filters": [
                {
                    "propertyName": "dealstage",
                    "operator":     "IN",
                    "values":       DEAL_STAGES
                },
                {
                    "propertyName": "createdate",
                    "operator":     "GTE",
                    "value":        str(CREATE_DATE_AFTER_MS)
                },
                {
                    "propertyName": "pricing_discussion_initiated",
                    "operator":     "NEQ",
                    "value":        "true"
                }
            ]
        }],
        "properties": ["dealname", "dealstage", "createdate"],
        "limit": 100
    }
    r = requests.post(url, headers=hs_headers, json=payload)
    r.raise_for_status()
    deals = r.json().get("results", [])
    print(f"✅ {len(deals)} eligible deals found")
    return deals

# ── Fetch engagements via legacy Engagements API (uses timeline scope) ─

def get_engagements(deal_id):
    """
    Uses the legacy engagements API — works with 'timeline' scope.
    Returns calls, notes, emails, meetings all in one endpoint.
    """
    activities = []
    offset     = 0

    while True:
        url = (
            f"https://api.hubapi.com/engagements/v1/engagements"
            f"/associated/deal/{deal_id}/paged"
            f"?limit=100&offset={offset}"
        )
        r = requests.get(url, headers=hs_headers)
        if r.status_code != 200:
            print(f"   ⚠️  Engagements API error {r.status_code} for deal {deal_id}")
            break

        data    = r.json()
        results = data.get("results", [])

        for item in results:
            eng      = item.get("engagement", {})
            metadata = item.get("metadata",   {})
            eng_type = eng.get("type", "")

            # Extract text based on engagement type
            text = ""
            if eng_type == "NOTE":
                text = metadata.get("body", "")
            elif eng_type == "CALL":
                text = " ".join(filter(None, [
                    metadata.get("body",       ""),
                    metadata.get("transcript", ""),
                    metadata.get("title",      ""),
                ]))
            elif eng_type in ("EMAIL", "INCOMING_EMAIL"):
                text = " ".join(filter(None, [
                    metadata.get("text",    ""),
                    metadata.get("subject", ""),
                ]))
            elif eng_type == "MEETING":
                text = " ".join(filter(None, [
                    metadata.get("body",  ""),
                    metadata.get("title", ""),
                ]))

            # Strip HTML tags simply
            import re
            text = re.sub(r"<[^>]+>", " ", text).strip()

            if text and len(text) > 20:
                activities.append({
                    "type": eng_type,
                    "id":   eng.get("id"),
                    "text": text
                })

        if not data.get("hasMore", False):
            break
        offset = data.get("offset", 0)

    return activities

# ── Claude analysis ───────────────────────────────────────────────────

def analyse(text, activity_type):
    try:
        msg = claude.messages.create(
            model      = "claude-haiku-4-5",
            max_tokens = 150,
            messages   = [{
                "role":    "user",
                "content": (
                    f"{DETECTION_PROMPT}\n\n"
                    f"ACTIVITY TYPE: {activity_type}\n\n"
                    f"CONTENT:\n{text[:4000]}"
                )
            }]
        )
        raw    = msg.content[0].text.strip()
        result = json.loads(raw)
        return result["pricing_mentioned"], result["reason"]
    except Exception as e:
        print(f"   ⚠️  Claude error: {e}")
        return False, ""

# ── Update deal ───────────────────────────────────────────────────────

def update_deal(deal_id, reason):
    url     = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
    payload = {
        "properties": {
            "pricing_discussion_initiated":  "true",
            "pricing_discussion_tag_reason": reason[:500]
        }
    }
    r = requests.patch(url, headers=hs_headers, json=payload)
    r.raise_for_status()

# ── Main ──────────────────────────────────────────────────────────────

def main():
    deals   = get_eligible_deals()
    updated = []

    for deal in deals:
        deal_id   = deal["id"]
        deal_name = deal["properties"].get("dealname", f"Deal {deal_id}")
        stage     = deal["properties"].get("dealstage", "")

        print(f"\n🔍 {deal_name} [{stage}]")

        activities = get_engagements(deal_id)
        print(f"   {len(activities)} engagements found "
              f"({', '.join(set(a['type'] for a in activities)) or 'none'})")

        if not activities:
            continue

        pricing_found = False
        best_reason   = ""

        for activity in activities:
            found, reason = analyse(activity["text"], activity["type"])
            if found:
                pricing_found = True
                best_reason   = reason
                print(f"   ✅ Match [{activity['type']}]: {reason}")
                break

        if not pricing_found:
            print(f"   — No pricing found")
            continue

        update_deal(deal_id, best_reason)
        print(f"   🎯 Both fields updated")
        updated.append({
            "deal":   deal_name,
            "stage":  stage,
            "reason": best_reason
        })

    print(f"\n{'='*50}")
    print(f"✅ Done — {len(updated)} deals updated this run")
    for u in updated:
        print(f"  • {u['deal']}: {u['reason']}")

if __name__ == "__main__":
    main()
