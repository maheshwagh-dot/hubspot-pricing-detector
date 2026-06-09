import os
import json
import requests
from datetime import datetime, timedelta
from anthropic import Anthropic

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))

hs_headers = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json"
}
claude = Anthropic(api_key=ANTHROPIC_KEY)

# ── Fetch recent activities ──────────────────────────────────────────

def get_recent_activities(object_type, text_properties):
    since = int((datetime.utcnow() - timedelta(hours=LOOKBACK_HOURS)).timestamp() * 1000)
    url = f"https://api.hubapi.com/crm/v3/objects/{object_type}/search"
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "hs_createdate",
                "operator": "GTE",
                "value": str(since)
            }]
        }],
        "properties": text_properties,
        "limit": 100
    }
    r = requests.post(url, headers=hs_headers, json=payload)
    r.raise_for_status()
    return r.json().get("results", [])

# ── Get associated deal ──────────────────────────────────────────────

def get_associated_deal(object_type, object_id):
    url = f"https://api.hubapi.com/crm/v4/objects/{object_type}/{object_id}/associations/deals"
    r = requests.get(url, headers=hs_headers)
    if r.status_code != 200:
        return None
    results = r.json().get("results", [])
    return results[0]["toObjectId"] if results else None

# ── Check if deal already flagged ───────────────────────────────────

def is_already_flagged(deal_id):
    url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
    r = requests.get(url, headers=hs_headers, params={
        "properties": "pricing_discussion_initiated"
    })
    if r.status_code != 200:
        return False
    val = r.json().get("properties", {}).get("pricing_discussion_initiated")
    return val == "true"

# ── Claude analysis ──────────────────────────────────────────────────

def analyse_for_pricing(text, activity_type):
    if not text or not text.strip():
        return False, "No content"
    
    message = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""You are analysing a sales {activity_type} from a CRM.

Determine if there is ANY discussion, mention, or reference to pricing, 
cost, budget, quotes, proposals, fees, discounts, commercial terms, 
or financial aspects of a deal — including indirect references like 
"let's talk numbers", "what would it take", "investment", "what's the damage" etc.

Respond ONLY with valid JSON in this exact format:
{{"pricing_mentioned": true/false, "reason": "brief explanation"}}

{activity_type.upper()} CONTENT:
{text[:3000]}"""
        }]
    )
    
    raw = message.content[0].text.strip()
    result = json.loads(raw)
    return result["pricing_mentioned"], result["reason"]

# ── Update deal ──────────────────────────────────────────────────────

def flag_deal(deal_id):
    url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
    payload = {"properties": {"pricing_discussion_initiated": "true"}}
    r = requests.patch(url, headers=hs_headers, json=payload)
    r.raise_for_status()

# ── Main ─────────────────────────────────────────────────────────────

def main():
    activity_types = [
        ("calls",  ["hs_call_body", "hs_call_transcript"]),
        ("notes",  ["hs_note_body"]),
        ("emails", ["hs_email_text", "hs_email_subject"]),
    ]
    
    log = []

    for obj_type, props in activity_types:
        print(f"\n🔍 Scanning {obj_type}...")
        activities = get_recent_activities(obj_type, props)
        print(f"   Found {len(activities)} recent {obj_type}")

        for activity in activities:
            aid = activity["id"]
            ap  = activity.get("properties", {})

            # Combine all text fields
            text = " ".join(filter(None, [ap.get(p, "") for p in props]))

            # Claude analysis
            pricing_found, reason = analyse_for_pricing(text, obj_type.rstrip("s"))
            
            if not pricing_found:
                continue

            print(f"   ✅ Pricing mention found in {obj_type} {aid}: {reason}")

            # Find deal
            deal_id = get_associated_deal(obj_type, aid)
            if not deal_id:
                print(f"   ⚠️  No deal associated with {obj_type} {aid} — skipping")
                continue

            # Skip if already flagged
            if is_already_flagged(deal_id):
                print(f"   ℹ️  Deal {deal_id} already flagged — skipping")
                continue

            # Update deal
            flag_deal(deal_id)
            print(f"   🎯 Deal {deal_id} updated: Pricing Discussion Initiated = true")

            log.append({
                "timestamp": datetime.utcnow().isoformat(),
                "activity_type": obj_type,
                "activity_id": aid,
                "deal_id": deal_id,
                "reason": reason
            })

    # Save audit log
    with open("pricing_detection_log.json", "w") as f:
        json.dump(log, f, indent=2)
    
    print(f"\n✅ Done. {len(log)} deals updated this run.")

if __name__ == "__main__":
    main()
