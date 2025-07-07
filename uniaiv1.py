import logging
import os
import re
import json
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Environment checks
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
    raise RuntimeError("Missing AIRTABLE_API_KEY or AIRTABLE_BASE_ID in environment")
if not CLAUDE_API_KEY:
    logging.warning("CLAUDE_API_KEY not set; fallback to template/knowledge only")

# App setup
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# Airtable setup
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
TABLES = {
    "config": "WhatsAppConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge": "KnowledgeBase",
    "history": "CustomerHistory",
    "sales": "SalesData"
}

# Utilities

def detect_language(text):
    return 'zh' if re.search(r'[一-鿿]', text) else 'en'

# Airtable fetch

def fetch_whatsapp_config(wa_id):
    params = {"filterByFormula": f"WhatsAppNumber='{wa_id}'"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params=params)
    res.raise_for_status()
    recs = res.json().get("records", [])
    if not recs:
        raise ValueError(f"No config for {wa_id}")
    return recs[0]["fields"]

# Template lookup

def find_template(business_id, wa_cfg_id, msg, lang):
    params = {"filterByFormula": f"AND(Business='{business_id}', WhatsAppConfig='{wa_cfg_id}', Language='{lang}')"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        f = rec["fields"]
        if f.get("Step", "").lower() in msg.lower():
            return f
    return None

# Knowledge lookup

def find_knowledge(business_id, msg, role):
    params = {"filterByFormula": f"Business='{business_id}'"}
    res = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}", headers=HEADERS, params=params)
    res.raise_for_status()
    for rec in res.json().get("records", []):
        f = rec["fields"]
        if f.get("Title", "").lower() in msg.lower():
            scripts = json.loads(f.get("RoleScripts", "{}"))
            script = scripts.get(role) or f.get("DefaultScript")
            imgs = f.get("ImageURL") or []
            return script, imgs[0].get("url") if imgs else None
    return None, None

# Claude call

def call_claude(user_msg, history, prompt, model):
    if not CLAUDE_API_KEY:
        raise RuntimeError("Missing CLAUDE_API_KEY")
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

# Wassenger sends

def send_whatsapp(phone, text, api_key):
    resp = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"phone": phone, "message": text}
    )
    resp.raise_for_status()
    return resp.json()


def send_image(phone, url_image, api_key):
    resp = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"phone": phone, "message": "", "url": url_image}
    )
    resp.raise_for_status()
    return resp.json()

# History & Sales

def record_history(biz, cfg, phone, step, hist):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"fields": {"Business": biz, "WhatsAppConfig": cfg, "PhoneNumber": phone, "CurrentStep": step, "History": hist}}
    )

def record_sales(biz, cfg, phone, name, svc):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"fields": {"Business": biz, "WhatsAppConfig": cfg, "PhoneNumber": phone, "CustomerName": name, "ServiceBooked": svc, "Status": "Pending"}}
    )

# Webhook (health-check + handler)
@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", json.dumps(payload))

    # Wassenger v1
    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        data = payload["data"]
        msg = data.get("body", "").strip()
        wa = data.get("fromNumber", "").strip()
    # Wassenger v2
    elif all(k in payload for k in ("body", "fromNumber")):
        msg = payload.get("body", "").strip()
        wa = payload.get("fromNumber", "").strip()
    else:
        return jsonify({"status": "ignored"}), 200

    cfg = fetch_whatsapp_config(wa)
    bid = cfg.get("Business")
    role = cfg.get("Role")
    pr = cfg.get("ClaudePrompt")
    mdl = cfg.get("ClaudeModel")
    key = cfg.get("WASSENGER_API_KEY")
    lang = detect_language(msg)

    # 1) Template
    tmpl = find_template(bid, cfg.get("WhatsAppID"), msg, lang)
    if tmpl:
        if tmpl.get("ImageURL"):
            send_image(wa, tmpl.get("ImageURL"), key)
        send_whatsapp(wa, tmpl.get("TemplateBody"), key)
        record_history(bid, cfg.get("WhatsAppID"), wa, tmpl.get("Step", "step"), f"User:{msg}|Bot:{tmpl.get('TemplateBody')}")
        return jsonify({"status": "template_sent"}), 200

    # 2) Knowledge
    scr, img = find_knowledge(bid, msg, role)
    if scr:
        if img:
            send_image(wa, img, key)
        send_whatsapp(wa, scr, key)
        record_history(bid, cfg.get("WhatsAppID"), wa, "knowledge", f"User:{msg}|Bot:{scr}")
        return jsonify({"status": "knowledge_sent"}), 200

    # 3) Claude fallback
    hist = f"User: {msg}"
    resp = call_claude(msg, hist, pr, mdl)
    send_whatsapp(wa, resp, key)
    record_history(bid, cfg.get("WhatsAppID"), wa, "fallback", f"User:{msg}|Bot:{resp}")
    if any(x in resp.lower() for x in ("booking","预约")):
                record_sales(bid, cfg.get("WhatsAppID"), wa, "Unknown", "TBD")
