import logging
import os
import re
import json
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Check required environment variables
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
    raise RuntimeError("Missing AIRTABLE_API_KEY or AIRTABLE_BASE_ID in environment")
if not CLAUDE_API_KEY:
    logging.warning("CLAUDE_API_KEY not set; some features will be disabled")

# App setup
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# Airtable setup
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

# Table names in Airtable
TABLES = {
    "config": "WhatsAppConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge": "KnowledgeBase",
    "history": "CustomerHistory",
    "sales": "SalesData"
}

# Utility: detect language

def detect_language(text):
    return 'zh' if re.search(r'[一-鿿]', text) else 'en'

# Fetch WhatsApp configuration from Airtable

def fetch_whatsapp_config(wa_id):
    params = {"filterByFormula": f"WhatsAppNumber='{wa_id}'"}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params=params)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        raise ValueError(f"No config found for WA ID: {wa_id}")
    return records[0]["fields"]

# Lookup predefined reply templates

def find_template(business_id, wa_cfg_id, msg, lang):
    formula = f"AND(Business='{business_id}', WhatsAppConfig='{wa_cfg_id}', Language='{lang}')"
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params={"filterByFormula": formula})
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        fields = rec["fields"]
        if fields.get("Step", "").lower() in msg.lower():
            return fields
    return None

# Lookup knowledge base entries

def find_knowledge(business_id, msg, role):
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}", headers=HEADERS, params={"filterByFormula": f"Business='{business_id}'"})
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        fields = rec["fields"]
        if fields.get("Title", "").lower() in msg.lower():
            scripts = json.loads(fields.get("RoleScripts", "{}"))
            script = scripts.get(role) or fields.get("DefaultScript")
            attachments = fields.get("ImageURL") or []
            image_url = attachments[0]["url"] if attachments else None
            return script, image_url
    return None, None

# Call Claude for fallback replies

def call_claude(user_msg, history, prompt, model):
    if not CLAUDE_API_KEY:
        raise RuntimeError("CLAUDE_API_KEY not configured")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.format(history=history, user_message=user_msg)},
            {"role": "user", "content": user_msg}
        ],
        "max_tokens_to_sample": 512,
        "temperature": 0.7
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/chat/completions",
        headers={"x-api-key": CLAUDE_API_KEY, "Content-Type": "application/json"},
        json=payload
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

# Send text via Wassenger

def send_whatsapp(phone, text, api_key):
    resp = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"phone": phone, "message": text}
    )
    resp.raise_for_status()
    return resp.json()

# Send image via Wassenger

def send_image(phone, image_url, api_key):
    resp = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"phone": phone, "message": "", "url": image_url}
    )
    resp.raise_for_status()
    return resp.json()

# Record conversation history

def record_history(business, cfg_id, phone, step, history):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"fields": {"Business": business, "WhatsAppConfig": cfg_id, "PhoneNumber": phone, "CurrentStep": step, "History": history}}
    )

# Record bookings/sales

def record_sales(business, cfg_id, phone, name, service):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"fields": {"Business": business, "WhatsAppConfig": cfg_id, "PhoneNumber": phone, "CustomerName": name, "ServiceBooked": service, "Status": "Pending"}}
    )

# Health-check and webhook handler
@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return jsonify({"status": "ok"})

    payload = request.get_json(force=True)
    logging.info("Incoming payload: %s", json.dumps(payload))

    # Wassenger v1
    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        data = payload.get("data", {})
        msg = data.get("body", "").strip()
        wa_id = data.get("fromNumber", "").strip()
    # Wassenger v2
    elif "body" in payload and "fromNumber" in payload:
        msg = payload.get("body", "").strip()
        wa_id = payload.get("fromNumber", "").strip()
    else:
        return jsonify({"status": "ignored"})

    cfg = fetch_whatsapp_config(wa_id)
    business = cfg.get("Business")
    role = cfg.get("Role")
    prompt = cfg.get("ClaudePrompt")
    model = cfg.get("ClaudeModel")
    api_key = cfg.get("WASSENGER_API_KEY")
    lang = detect_language(msg)

    # 1) Template
    tmpl = find_template(business, cfg.get("WhatsAppID"), msg, lang)
    if tmpl:
        if tmpl.get("ImageURL"):
            send_image(wa_id, tmpl.get("ImageURL"), api_key)
        send_whatsapp(wa_id, tmpl.get("TemplateBody"), api_key)
        record_history(business, cfg.get("WhatsAppID"), wa_id, tmpl.get("Step", "step"), f"User:{msg}|Bot:{tmpl.get('TemplateBody')}")
        return jsonify({"status": "template_sent"})

    # 2) Knowledge
    script, img_url = find_knowledge(business, msg, role)
    if script:
        if img_url:
            send_image(wa_id, img_url, api_key)
        send_whatsapp(wa_id, script, api_key)
        record_history(business, cfg.get("WhatsAppID"), wa_id, "knowledge", f"User:{msg}|Bot:{script}")
        return jsonify({"status": "knowledge_sent"})

    # 3) Fallback to Claude
    history_text = f"User: {msg}"
    reply = call_claude(msg, history_text, prompt, model)
    send_whatsapp(wa_id, reply, api_key)
    record_history(business, cfg.get("WhatsAppID"), wa_id, "fallback", f"User:{msg}|Bot:{reply}")
    if any(keyword in reply.lower() for keyword in ("booking", "预约")):
        record_sales(business, cfg.get("WhatsAppID"), wa_id, "Unknown", "TBD")
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
