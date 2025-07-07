import logging
import os
import re
import json
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# Airtable setup\AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
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
    fields = res.json()["records"][0]["fields"]
    return fields

# === Template lookup ===
def find_template(business_id, wa_cfg_id, msg, lang):
    params = {"filterByFormula": f"AND(Business='{business_id}', WhatsAppConfig='{wa_cfg_id}', Language='{lang}')"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        if rec["fields"].get("Step", "").lower() in msg.lower():
            return rec["fields"]
    return None

# === KnowledgeBase lookup ===
def find_knowledge(business_id, msg, role):
    params = {"filterByFormula": f"Business='{business_id}'"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        fields = rec["fields"]
        if fields.get("Title", "").lower() in msg.lower():
            script = json.loads(fields.get("RoleScripts", "{}")).get(role) or fields.get("DefaultScript")
            image_url = fields.get("ImageURL", [{}])[0].get("url") if fields.get("ImageURL") else None
            return script, image_url
    return None, None

# === Claude call ===
def call_claude(user_msg, history, prompt, model):
    url = "https://api.anthropic.com/v1/chat/completions"
    headers = {"x-api-key": os.getenv("CLAUDE_API_KEY"), "Content-Type": "application/json"}
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
    """
    Send a plain-text WhatsApp message via Wassenger.
    """
    url = "https://api.wassenger.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "phone": phone,
        "message": text
    }
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def send_image(phone: str, image_url: str, api_key: str):
    """
    Send an image message via Wassenger by passing a URL.
    """
    url = "https://api.wassenger.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "phone": phone,
        "message": "",      # optional caption
        "url": image_url     # Wassenger treats this as an image
    }
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
def webhook():
    payload = request.get_json(force=True)
    logging.info("Wassenger webhook payload: %s", payload)

    # Only handle inbound messages
    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        incoming = payload.get("data", {})
        msg   = incoming.get("body", "").strip()
        wa_id = incoming.get("fromNumber", "").strip()
    else:
        return jsonify({"status": "ignored"}), 200

    # Fetch config from Airtable
    wa_cfg      = fetch_whatsapp_config(wa_id)
    business_id = wa_cfg["Business"]
    role        = wa_cfg["Role"]
    prompt      = wa_cfg["ClaudePrompt"]
    model       = wa_cfg["ClaudeModel"]
    api_key     = wa_cfg.get("WASSENGER_API_KEY")  # rename your Airtable field if needed

    lang = detect_language(msg)

    # Step 1: Template
    template = find_template(business_id, wa_cfg.get("WhatsAppID"), msg, lang)
    if template:
        if template.get("ImageURL"):
            send_image(wa_id, template["ImageURL"], api_key)
        send_whatsapp(wa_id, template["TemplateBody"], api_key)
        record_history(business_id, wa_cfg.get("WhatsAppID"), wa_id, template.get("Step", "step"),
                       f"Customer: {msg} | Bot: {template['TemplateBody']}")
        return jsonify({"status": "template_sent"})

    # Step 2: KnowledgeBase
    script, image_url = find_knowledge(business_id, msg, role)
    if script:
        if image_url:
            send_image(wa_id, image_url, api_key)
        send_whatsapp(wa_id, script, api_key)
        record_history(business_id, wa_cfg.get("WhatsAppID"), wa_id, "knowledge", f"Customer: {msg} | Bot: {script}")
        return jsonify({"status": "knowledge_sent"})

    # Step 3: Fallback to Claude
    history_text = f"Customer: {msg}"
    reply = call_claude(msg, history_text, prompt, model)
    send_whatsapp(wa_id, reply, api_key)
    record_history(business_id, wa_cfg.get("WhatsAppID"), wa_id, "fallback", f"Customer: {msg} | Bot: {reply}")

    # Record booking sales if detected
    if any(k in reply.lower() for k in ("booking", "预约")):
        record_sales(business_id, wa_cfg.get("WhatsAppID"), wa_id, "Unknown", "TBD")

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
