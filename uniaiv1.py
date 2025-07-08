import os
import re
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ───── Load & validate env vars ─────
load_dotenv()
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")

if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and CLAUDE_API_KEY):
    raise RuntimeError("Set AIRTABLE_API_KEY, AIRTABLE_BASE_ID, and CLAUDE_API_KEY in .env")

# ───── Airtable setup ─────
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS      = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
TABLES = {
    "config":    "WhatsappConfig",
    "template":  "WhatsAppReplyTemplate",
    "knowledge": "KnowledgeBase",
    "history":   "CustomerHistory",
    "sales":     "SalesData"
}

# ───── Helpers ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def fetch_whatsapp_config(service_number: str) -> dict:
    """
    1) Filter WhatsappConfig by {WhatsappNumber}='service_number'
    2) Extract WA_ID, lookup BusinessID, and Wassenger key.
    """
    formula = f"{{WhatsappNumber}}='{service_number}'"
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['config']}",
        headers=HEADERS,
        params={"filterByFormula": formula}
    )
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if not recs:
        raise ValueError(f"No config found for WhatsappNumber={service_number}")
    row = recs[0]
    flds = row["fields"]

    # BusinessID (from BusinessConfig) is a lookup => list of strings
    biz_lookup = flds.get("BusinessID (from BusinessConfig)", [])
    if not isinstance(biz_lookup, list) or not biz_lookup:
        raise ValueError("No BusinessID lookup in config record")
    business_id = biz_lookup[0]

    return {
        "WA_ID":           flds["WA_ID"],                        # e.g. "WA001"
        "BusinessID":      business_id,                          # plain string e.g. "BIZ001"
        "WassengerApiKey": flds.get("WASSENGER_API_KEY")         # your token
    }

def find_template(business_id: str, wa_id: str, msg: str) -> dict | None:
    """
    Filter WhatsAppReplyTemplate by:
      {BusinessID (from BusinessConfig)} = business_id
      AND {WhatsAppConfig} = wa_id
    """
    # build Airtable formula (no Python-list repr)
    formula = (
        "AND("
          "{BusinessID (from BusinessConfig)}='" + business_id + "',"
          "{WhatsAppConfig}='"                 + wa_id       + "'"
        ")"
    )
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['template']}",
        headers=HEADERS,
        params={"filterByFormula": formula}
    )
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        # return the first matched row’s fields
        return rec["fields"]
    return None

def find_knowledge(business_id: str, msg: str, role: str):
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['knowledge']}",
        headers=HEADERS,
        params={"filterByFormula": f"{{Business}}='{business_id}'"}
    )
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        flds = rec["fields"]
        title = flds.get("Title","")
        if title and title.lower() in msg.lower():
            scripts = flds.get("RoleScripts") or {}
            script  = scripts.get(role) or flds.get("DefaultScript")
            img_url = None
            if flds.get("ImageURL"):
                img_url = flds["ImageURL"][0]["url"]
            return script, img_url
    return None, None

def call_claude(user_msg: str, history: str, prompt: str, model: str) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }
    payload = {
        "model":     model,
        "max_tokens":1024,
        "messages": [
            {"role":"system", "content": prompt.format(history=history, user_message=user_msg)},
            {"role":"user",   "content": user_msg}
        ]
    }
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def send_whatsapp(phone: str, text: str, api_key: str):
    resp = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={
          "Authorization": f"Bearer {api_key}",
          "Content-Type": "application/json"
        },
        json={"phone": phone, "message": text}
    )
    resp.raise_for_status()

def send_image(phone: str, image_url: str, api_key: str):
    resp = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={
          "Authorization": f"Bearer {api_key}",
          "Content-Type": "application/json"
        },
        json={"phone": phone, "message": "", "url": image_url}
    )
    resp.raise_for_status()

def record_history(biz: str, wa_id: str, phone: str, step: str, hist: str):
    payload = {"fields":{
        "BusinessID":       biz,
        "WhatsAppConfig":   wa_id,
        "PhoneNumber":      phone,
        "CurrentStep":      step,
        "History":          hist
    }}
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**HEADERS, "Content-Type":"application/json"},
        json=payload
    )

def record_sales(biz: str, wa_id: str, phone: str, name: str, svc: str):
    payload = {"fields":{
        "BusinessID":       biz,
        "WhatsAppConfig":   wa_id,
        "PhoneNumber":      phone,
        "CustomerName":     name,
        "ServiceBooked":    svc,
        "Status":           "Pending"
    }}
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**HEADERS, "Content-Type":"application/json"},
        json=payload
    )

# ───── Flask App & Webhook ─────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.route("/", methods=["GET","POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", payload)

    # Wassenger inbound message
    if payload.get("object")=="message" and payload.get("event")=="message:in:new":
        data = payload["data"]
        # ignore group chats
        if data.get("meta",{}).get("isGroup"):
            return jsonify({"status":"ignored_group"}), 200

        svc_no = data.get("toNumber","").lstrip("+")
        cus_no = data.get("fromNumber","").lstrip("+")
        msg    = data.get("body","").strip()

        cfg = fetch_whatsapp_config(svc_no)
        biz = cfg["BusinessID"]
        wa  = cfg["WA_ID"]
        key = cfg["WassengerApiKey"]

        # 1) Template lookup
        tpl = find_template(biz, wa, msg)
        if tpl:
            body = tpl.get("TemplateBody","")
            send_whatsapp(cus_no, body, key)
            record_history(biz, wa, cus_no, "template", f"C:{msg}|B:{body}")
            return jsonify({"status":"template_sent"}), 200

        # 2) KnowledgeBase
        script, img = find_knowledge(biz, msg, cfg.get("Role",""))
        if script:
            if img:
                send_image(cus_no, img, key)
            send_whatsapp(cus_no, script, key)
            record_history(biz, wa, cus_no, "knowledge", f"C:{msg}|B:{script}")
            return jsonify({"status":"knowledge_sent"}), 200

        # 3) Fallback to Claude
        hist  = f"Customer: {msg}"
        reply = call_claude(msg, hist, cfg.get("ClaudePrompt",""), cfg.get("ClaudeModel",""))
        send_whatsapp(cus_no, reply, key)
        record_history(biz, wa, cus_no, "fallback", f"C:{msg}|B:{reply}")

        # optional sales recording
        if "booking" in reply.lower() or "预约" in reply:
            record_sales(biz, wa, cus_no, "Unknown","TBD")

        return jsonify({"status":"ok"}), 200

    return jsonify({"status":"ignored"}), 200

if __name__=="__main__":
    port = int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=True)
