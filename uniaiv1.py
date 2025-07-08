import os
import re
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ───── Load & validate environment variables ─────
load_dotenv()
AIRTABLE_PAT     = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")

if not (AIRTABLE_PAT and AIRTABLE_BASE_ID and CLAUDE_API_KEY):
    raise RuntimeError("Please set AIRTABLE_PAT, AIRTABLE_BASE_ID, and CLAUDE_API_KEY in your .env")

# ───── Airtable configuration ─────
AIRTABLE_URL     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
AIRTABLE_HEADERS = {"Authorization": f"Bearer {AIRTABLE_PAT}"}
TABLES = {
    "business": "BusinessConfig",
    "config":   "WhatsappConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge":"KnowledgeBase",
    "history":  "CustomerHistory",
    "sales":    "SalesData",
}

# ───── Helper functions ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def fetch_service_config(svc_no: str) -> dict:
    # Lookup row in WhatsappConfig by numeric WhatsappNumber
    formula = f"{{WhatsappNumber}}='{svc_no}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        raise ValueError(f"No WhatsappConfig found for number {svc_no}")
    f = recs[0]["fields"]

    wa_id = f.get("WA_ID")
    token = f.get("WASSENGER_API_KEY")
    if not (wa_id and token):
        raise ValueError("Config row missing WA_ID or WASSENGER_API_KEY")

    # Unpack the linked BusinessID (from Business)
    biz_list = f.get("BusinessID (from Business)", [])
    if not isinstance(biz_list, list) or not biz_list:
        raise ValueError("Config row missing BusinessID lookup")
    business_id = biz_list[0]

    return {
        "WA_ID":           wa_id,
        "BusinessID":      business_id,
        "WassengerApiKey": token
    }

def fetch_business_settings(biz_id: str) -> dict:
    # Lookup row in BusinessConfig by BusinessID
    formula = f"{{BusinessID}}='{biz_id}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['business']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        raise ValueError(f"No BusinessConfig for ID {biz_id}")
    f = recs[0]["fields"]
    return {
        "DefaultLanguage": f.get("DefaultLanguage", "en"),
        "ClaudePrompt":    f.get("ClaudePrompt", ""),
        "ClaudeModel":     f.get("ClaudeModel", "claude-2.1"),
    }

def find_template(biz: str, wa: str, msg: str) -> dict | None:
    # Lookup row in WhatsAppReplyTemplate by BusinessID & WA_ID
    formula = (
        "AND("
          "{BusinessID (from Business)}='" + biz + "',"
          "{WhatsAppConfig}='"             + wa  + "'"
        ")"
    )
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    for rec in r.json().get("records", []):
        return rec["fields"]
    return None

def find_knowledge(biz: str, msg: str, role: str):
    # Lookup KnowledgeBase by BusinessID, then match Title in msg
    formula = f"{{BusinessID}}='{biz}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    for rec in r.json().get("records", []):
        f = rec["fields"]
        title = f.get("Title", "")
        if title and title.lower() in msg.lower():
            scripts = f.get("RoleScripts") or {}
            script  = scripts.get(role) or f.get("DefaultScript")
            img_url = None
            if f.get("ImageURL"):
                img_url = f["ImageURL"][0]["url"]
            return script, img_url
    return None, None

def call_claude(user_msg: str, history: str, prompt: str, model: str) -> str:
    # Send to Anthropic's current chat API at /v1/messages
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key":         CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type":      "application/json"
    }
    payload = {
        "model":      model,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": prompt.format(history=history, user_message=user_msg)},
            {"role": "user",   "content": user_msg}
        ]
    }
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def send_whatsapp(phone: str, text: str, token: str):
    # Wassenger expects header "Token" and E.164 phone with leading '+'
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={
            "Token":        token,
            "Content-Type": "application/json"
        },
        json={"phone": phone, "message": text}
    )
    r.raise_for_status()

def send_image(phone: str, url: str, token: str):
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={
            "Token":        token,
            "Content-Type": "application/json"
        },
        json={"phone": phone, "message": "", "url": url}
    )
    r.raise_for_status()

def record_history(biz: str, wa: str, phone: str, step: str, hist: str):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**AIRTABLE_HEADERS, "Content-Type": "application/json"},
        json={"fields": {
            "BusinessID":     biz,
            "WhatsAppConfig": wa,
            "PhoneNumber":    phone,
            "CurrentStep":    step,
            "History":        hist
        }}
    )

def record_sales(biz: str, wa: str, phone: str, name: str, svc: str):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**AIRTABLE_HEADERS, "Content-Type": "application/json"},
        json={"fields": {
            "BusinessID":     biz,
            "WhatsAppConfig": wa,
            "PhoneNumber":    phone,
            "CustomerName":   name,
            "ServiceBooked":  svc,
            "Status":         "Pending"
        }}
    )

# ───── Flask app & webhook ─────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", payload)

    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        d = payload["data"]
        # ignore group messages
        if d.get("meta", {}).get("isGroup"):
            return jsonify(status="ignored_group"), 200

        # For lookup, strip '+'
        svc_no_lookup = d["toNumber"].lstrip("+")
        cus_no_lookup = d["fromNumber"].lstrip("+")
        # For sending, keep E.164 with '+'
        svc_no_raw = d["toNumber"]
        cus_no_raw = d["fromNumber"]
        msg = d["body"].strip()

        # Step 1: fetch service config
        scfg = fetch_service_config(svc_no_lookup)
        biz  = scfg["BusinessID"]
        wa   = scfg["WA_ID"]
        key  = scfg["WassengerApiKey"]

        # Step 2: fetch business settings
        bcfg = fetch_business_settings(biz)
        lang = bcfg["DefaultLanguage"]

        # Step 3: template lookup
        tpl = find_template(biz, wa, msg)
        if tpl:
            body = tpl.get("TemplateBody", "")
            send_whatsapp(cus_no_raw, body, key)
            record_history(biz, wa, cus_no_lookup, "template", f"C:{msg}|B:{body}")
            return jsonify(status="template_sent"), 200

        # Step 4: knowledge base lookup
        script, img = find_knowledge(biz, msg, scfg.get("Role", ""))
        if script:
            if img:
                send_image(cus_no_raw, img, key)
            send_whatsapp(cus_no_raw, script, key)
            record_history(biz, wa, cus_no_lookup, "knowledge", f"C:{msg}|B:{script}")
            return jsonify(status="knowledge_sent"), 200

        # Step 5: fallback to Claude
        history = f"Customer: {msg}"
        reply  = call_claude(msg, history, bcfg["ClaudePrompt"], bcfg["ClaudeModel"])
        send_whatsapp(cus_no_raw, reply, key)
        record_history(biz, wa, cus_no_lookup, "fallback", f"C:{msg}|B:{reply}")

        # optional: record sales if booking detected
        if "booking" in reply.lower() or "预约" in reply:
            record_sales(biz, wa, cus_no_lookup, "Unknown", "TBD")

        return jsonify(status="ok"), 200

    return jsonify(status="ignored"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
