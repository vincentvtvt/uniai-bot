import logging
import os
import time
import re
import json
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
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
    return 'zh' if re.search(r'[\u4e00-\u9fff]', text) else 'en'

# === Look up WhatsAppConfig by wa_id ===
def fetch_whatsapp_config(wa_id):
    params = {"filterByFormula": f"WhatsAppNumber='{wa_id}'"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params=params)
    res.raise_for_status()
    fields = res.json()["records"][0]["fields"]
    return fields

# === Look for Template step ===
def find_template(business_id, wa_cfg_id, msg, lang):
    params = {"filterByFormula": f"AND(Business='{business_id}', WhatsAppConfig='{wa_cfg_id}', Language='{lang}')"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        if rec["fields"].get("Step", "").lower() in msg.lower():
            return rec["fields"]
    return None

# === Look up KnowledgeBase ===
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

# === Call Claude ===
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

# === Send WhatsApp ===
def send_whatsapp(phone, text, api_key):
    url = "https://waba.360dialog.io/v1/messages"
    headers = {"D360-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"recipient_type": "individual", "to": phone, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload).raise_for_status()

def send_image(phone, image_url, api_key):
    url = "https://waba.360dialog.io/v1/messages"
    headers = {"D360-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"recipient_type": "individual", "to": phone, "type": "image", "image": {"link": image_url}}
    requests.post(url, headers=headers, json=payload).raise_for_status()

# === Record history ===
def record_history(business, wa_cfg_id, phone, step, history):
    data = {"fields": {"Business": business, "WhatsAppConfig": wa_cfg_id, "PhoneNumber": phone, "CurrentStep": step, "History": history}}
    requests.post(f"{AIRTABLE_URL}/{TABLES['history']}", headers={**HEADERS, "Content-Type": "application/json"}, json=data)

# === Record sales ===
def record_sales(business, wa_cfg_id, phone, name, service, status="Pending"):
    data = {"fields": {"Business": business, "WhatsAppConfig": wa_cfg_id, "PhoneNumber": phone, "CustomerName": name, "ServiceBooked": service, "Status": status}}
    requests.post(f"{AIRTABLE_URL}/{TABLES['sales']}", headers={**HEADERS, "Content-Type": "application/json"}, json=data)

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json()
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"].strip()
    wa_id = payload["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"].strip()

    wa_cfg = fetch_whatsapp_config(wa_id)
    business_id = wa_cfg["Business"]
    role = wa_cfg["Role"]
    prompt = wa_cfg["ClaudePrompt"]
    model = wa_cfg["ClaudeModel"]
    api_key = wa_cfg["D360_API_KEY"]

    lang = detect_language(msg)

    # Step 1: Check Template
    template = find_template(business_id, wa_cfg["WhatsAppID"], msg, lang)
    if template:
        if template.get("ImageURL"):
            send_image(wa_id, template["ImageURL"], api_key)
        send_whatsapp(wa_id, template["TemplateBody"], api_key)
        record_history(business_id, wa_cfg["WhatsAppID"], wa_id, template.get("Step", "step"), f"Customer: {msg} | Bot: {template['TemplateBody']}")
        return jsonify({"status": "template_sent"})

    # Step 2: Check KnowledgeBase
    script, image_url = find_knowledge(business_id, msg, role)
    if script:
        if image_url:
            send_image(wa_id, image_url, api_key)
        send_whatsapp(wa_id, script, api_key)
        record_history(business_id, wa_cfg["WhatsAppID"], wa_id, "knowledge", f"Customer: {msg} | Bot: {script}")
        return jsonify({"status": "knowledge_sent"})

    # Step 3: Fallback Claude
    history_text = f"Customer: {msg}"
    reply = call_claude(msg, history_text, prompt, model)
    send_whatsapp(wa_id, reply, api_key)
    record_history(business_id, wa_cfg["WhatsAppID"], wa_id, "fallback", f"Customer: {msg} | Bot: {reply}")

    if "booking" in reply.lower() or "预约" in reply:
        record_sales(business_id, wa_cfg["WhatsAppID"], wa_id, "Unknown", "TBD")

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
