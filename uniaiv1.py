import logging
import os
import re
import json
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Make sure we have API keys from environment
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
    raise RuntimeError("Missing AIRTABLE_API_KEY or AIRTABLE_BASE_ID in environment")
if not CLAUDE_API_KEY:
    logging.warning("CLAUDE_API_KEY not set; fallback to template/knowledge only")

# Configure logging
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# Airtable endpoints
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

TABLES = {
    "config": "WhatsAppConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge": "KnowledgeBase",
    "history": "CustomerHistory",
    "sales": "SalesData"
}

# === Utilities ===
def detect_language(text):
    return 'zh' if re.search(r'[一-鿿]', text) else 'en'

# === Airtable fetch ===
def fetch_whatsapp_config(wa_id):
    params = {"filterByFormula": f"WhatsAppNumber='{wa_id}'"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params=params)
    res.raise_for_status()
    records = res.json().get("records", [])
    if not records:
        raise ValueError(f"No WhatsAppConfig found for {wa_id}")
    return records[0]["fields"]

# === Template lookup ===
def find_template(business_id, wa_cfg_id, msg, lang):
    params = {"filterByFormula": f"AND(Business='{business_id}', WhatsAppConfig='{wa_cfg_id}', Language='{lang}')"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        fields = rec["fields"]
        if fields.get("Step", "").lower() in msg.lower():
            return fields
    return None

# === KnowledgeBase lookup ===
def find_knowledge(business_id, msg, role):
    params = {"filterByFormula": f"Business='{business_id}'"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        fields = rec["fields"]
        if fields.get("Title", "").lower() in msg.lower():
            scripts = json.loads(fields.get("RoleScripts", "{}"))
            script = scripts.get(role) or fields.get("DefaultScript")
            attachments = fields.get("ImageURL") or []
            image_url = attachments[0]["url"] if attachments else None
            return script, image_url
    return None, None

# === Claude call ===
def call_claude(user_msg, history, prompt, model):
    if not CLAUDE_API_KEY:
        raise RuntimeError("Cannot call Claude: CLAUDE_API_KEY not set")
    url = "https://api.anthropic.com/v1/chat/completions"
    headers = {"x-api-key": CLAUDE_API_KEY, "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": prompt.format(history=history, user_message=user_msg)},
        {"role": "user", "content": user_msg}
    ]
    payload = {"model": model, "messages": messages, "max_tokens_to_sample": 512, "temperature": 0.7}
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

# === Wassenger send functions ===
def send_whatsapp(phone: str, text: str, api_key: str):
    url = "https://api.wassenger.com/v1/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"phone": phone, "message": text}
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()

def send_image(phone: str, image_url: str, api_key: str):
    url = "https://api.wassenger.com/v1/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"phone": phone, "message": "", "url": image_url}
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()

# === Airtable record functions ===
def record_history(business, wa_cfg_id, phone, step, history):
    data = {"fields": {"Business": business, "WhatsAppConfig": wa_cfg_id, "PhoneNumber": phone, "CurrentStep": step, "History": history}}
    requests.post(f"{AIRTABLE_URL}/{TABLES['history']}", headers={**HEADERS, "Content-Type": "application/json"}, json=data)

def record_sales(business, wa_cfg_id, phone, name, service, status="Pending"):
    data = {"fields": {"Business": business, "WhatsAppConfig": wa_cfg_id, "PhoneNumber": phone, "CustomerName": name, "ServiceBooked": service, "Status": status}}
    requests.post(f"{AIRTABLE_URL}/{TABLES['sales']}", headers={**HEADERS, "Content-Type": "application/json"}, json=data)

# === Webhook endpoint ===
@app.route("/webhook", methods=["POST"])
@app.route("/", methods=["POST"])
def webhook():
    payload = request.get_json(force=True)
    logging.info("Wassenger webhook payload: %s", json.dumps(payload))

    # Wassenger v1
    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        incoming = payload.get("data", {})
        msg = incoming.get("body", "").strip()
        wa_id = incoming.get("fromNumber", "").strip()
    # Wassenger v2 (direct message payload)
    elif "body" in payload and "fromNumber" in payload:
        msg = payload.get("body", "").strip()
        wa_id = payload.get("fromNumber", "").strip()
    else:
        return jsonify({"status": "ignored"}), 200

    # Fetch config
    wa_cfg = fetch_whatsapp_config(wa_id)
    business_id = wa_cfg.get("Business")
    role = wa_cfg.get("Role")
    prompt = wa_cfg.get("ClaudePrompt")
    model = wa_cfg.get("ClaudeModel")
    api_key = wa_cfg.get("WASSENGER_API_KEY")

    lang = detect_language(msg)

    # 1) Template
    template = find_template(business_id, wa_cfg.get("WhatsAppID"), msg, lang)
    if template:
        if template.get("ImageURL"):
            send_image(wa_id, template.get("ImageURL"), api_key)
        send_whatsapp(wa_id, template.get("TemplateBody"), api_key)
        record_history(business_id, wa_cfg.get("WhatsAppID"), wa_id, template.get("Step", "step"),
                       f"Customer: {msg} | Bot: {template.get('TemplateBody')}")
        return jsonify({"status": "template_sent"})

    # 2) Knowledge
    script, image_url = find_knowledge(business_id, msg, role)
    if script:
        if image_url:
            send_image(wa_id, image_url, api_key)
        send_whatsapp(wa_id, script, api_key)
        record_history(business_id, wa_cfg.get("WhatsAppID"), wa_id, "knowledge", f"Customer: {msg} | Bot: {script}")
        return jsonify({"status": "knowledge_sent"})

    # 3) Fallback
    history_text = f"Customer: {msg}"
    reply = call_claude(msg, history_text, prompt, model)
    send_whatsapp(wa_id, reply, api_key)
    record_history(business_id, wa_cfg.get("WhatsAppID"), wa_id, "fallback", f"Customer: {msg} | Bot: {reply}")

    # Record sales
    if any(k in reply.lower() for k in ("booking", "预约")):
        record_sales(business_id, wa_cfg.get("WhatsAppID"), wa_id, "Unknown", "TBD")

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
