import os
import re
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ───── Environment & Airtable Setup ─────
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY    = os.getenv("CLAUDE_API_KEY")

if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and CLAUDE_API_KEY):
    raise RuntimeError("Missing AIRTABLE_API_KEY, AIRTABLE_BASE_ID, or CLAUDE_API_KEY")

AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS      = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

TABLES = {
    "config":    "WhatsappConfig",
    "template":  "WhatsAppReplyTemplate",
    "knowledge": "KnowledgeBase",
    "history":   "CustomerHistory",
    "sales":     "SalesData"
}

# ───── Utilities ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def fetch_whatsapp_config(service_number: str) -> dict:
    """
    Look up your WhatsAppConfig record by the *service* number (no leading '+').
    """
    params = {"filterByFormula": f"WhatsAppNumber='{service_number}'"}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params=params)
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if not recs:
        raise ValueError(f"No config found for service number: {service_number}")
    record = recs[0]
    cfg = record["fields"]
    cfg["RecordID"] = record["id"]
    return cfg

def find_template(business_id, config_id, msg, lang):
    formula = (
        f"AND("
        f"Business='{business_id}',"
        f"WhatsAppConfig='{config_id}',"
        f"Language='{lang}'"
        f")"
    )
    params = {"filterByFormula": formula}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params=params)
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        trigger = rec["fields"].get("TriggerWord", "")
        if trigger and trigger.lower() in msg.lower():
            return rec["fields"]
    return None

def find_knowledge(business_id, msg, role):
    params = {"filterByFormula": f"Business='{business_id}'"}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}", headers=HEADERS, params=params)
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        f = rec["fields"]
        title = f.get("Title", "")
        if title and title.lower() in msg.lower():
            # Role-specific script or default
            scripts = f.get("RoleScripts")
            script = (scripts.get(role) if isinstance(scripts, dict) else None) or f.get("DefaultScript")
            img = None
            if f.get("ImageURL"):
                img = f["ImageURL"][0]["url"]
            return script, img
    return None, None

def call_claude(user_msg, history, prompt, model):
    """
    Uses the current Anthropic /v1/messages chat API.
    """
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {"role": "system",  "content": prompt.format(history=history, user_message=user_msg)},
            {"role": "user",    "content": user_msg}
        ]
    }
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def send_whatsapp(phone, text, api_key):
    url = "https://api.wassenger.com/v1/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"phone": phone, "message": text}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()

def send_image(phone, image_url, api_key):
    url = "https://api.wassenger.com/v1/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"phone": phone, "message": "", "url": image_url}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()

def record_history(business, config_id, phone, step, history):
    data = {"fields": {
        "Business": business,
        "WhatsAppConfig": config_id,
        "PhoneNumber": phone,
        "CurrentStep": step,
        "History": history
    }}
    requests.post(f"{AIRTABLE_URL}/{TABLES['history']}",
                  headers={**HEADERS, "Content-Type":"application/json"},
                  json=data)

def record_sales(business, config_id, phone, name, service, status="Pending"):
    data = {"fields": {
        "Business": business,
        "WhatsAppConfig": config_id,
        "PhoneNumber": phone,
        "CustomerName": name,
        "ServiceBooked": service,
        "Status": status
    }}
    requests.post(f"{AIRTABLE_URL}/{TABLES['sales']}",
                  headers={**HEADERS, "Content-Type":"application/json"},
                  json=data)

# ───── Flask App & Webhook ─────
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", payload)

    # Wassenger v1 inbound message
    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        data = payload["data"]
        # ignore group chats
        if data.get("meta", {}).get("isGroup"):
            return jsonify({"status":"ignored_group"}), 200

        # normalize numbers (strip '+')
        service_number  = data.get("toNumber","").lstrip("+")
        customer_number = data.get("fromNumber","").lstrip("+")
        msg = data.get("body","").strip()

        # fetch config by service number
        cfg = fetch_whatsapp_config(service_number)
        business_id = cfg["Business"]
        config_id   = cfg["RecordID"]
        role        = cfg.get("Role")
        prompt      = cfg.get("ClaudePrompt")
        model       = cfg.get("ClaudeModel")
        wassenger_key = cfg.get("WASSENGER_API_KEY")

        lang = detect_language(msg)

        # Step 1: Template
        template = find_template(business_id, config_id, msg, lang)
        if template:
            if template.get("ImageURL"):
                send_image(customer_number, template["ImageURL"], wassenger_key)
            send_whatsapp(customer_number, template["TemplateBody"], wassenger_key)
            record_history(business_id, config_id, customer_number,
                           template.get("Step","template"),
                           f"Customer: {msg} | Bot: {template['TemplateBody']}")
            return jsonify({"status":"template_sent"}), 200

        # Step 2: Knowledge Base
        script, image_url = find_knowledge(business_id, msg, role)
        if script:
            if image_url:
                send_image(customer_number, image_url, wassenger_key)
            send_whatsapp(customer_number, script, wassenger_key)
            record_history(business_id, config_id, customer_number,
                           "knowledge", f"Customer: {msg} | Bot: {script}")
            return jsonify({"status":"knowledge_sent"}), 200

        # Step 3: Fallback to Claude
        history_text = f"Customer: {msg}"
        reply = call_claude(msg, history_text, prompt, model)
        send_whatsapp(customer_number, reply, wassenger_key)
        record_history(business_id, config_id, customer_number,
                       "fallback", f"Customer: {msg} | Bot: {reply}")

        if "booking" in reply.lower() or "预约" in reply:
            record_sales(business_id, config_id, customer_number,
                         "Unknown", "TBD")

        return jsonify({"status":"ok"}), 200

    # ignore other callbacks
    return jsonify({"status":"ignored"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
