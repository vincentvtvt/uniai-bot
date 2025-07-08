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
    raise RuntimeError("Set AIRTABLE_API_KEY, AIRTABLE_BASE_ID & CLAUDE_API_KEY")

# ───── Airtable setup ─────
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS      = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
TABLES = {
    "business": "BusinessConfig",
    "config":   "WhatsappConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge":"KnowledgeBase",
    "history":  "CustomerHistory",
    "sales":    "SalesData"
}

# ───── Helpers ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def fetch_service_config(svc_no: str) -> dict:
    """Fetch from WhatsappConfig where {WhatsappNumber}=svc_no"""
    formula = f"{{WhatsappNumber}}='{svc_no}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}",
                     headers=HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        raise ValueError(f"No WhatsappConfig for {svc_no}")
    f = recs[0]["fields"]
    # WA_ID & API key
    wa_id = f["WA_ID"]
    wassenger_key = f["WASSENGER_API_KEY"]
    # linked business ID lookup
    biz_list = f.get("BusinessID (from BusinessConfig)", [])
    if not isinstance(biz_list, list) or not biz_list:
        raise ValueError("WhatsappConfig record missing BusinessID lookup")
    business_id = biz_list[0]
    return {"WA_ID": wa_id, "ApiKey": wassenger_key, "BusinessID": business_id}

def fetch_business_settings(business_id: str) -> dict:
    """Fetch from BusinessConfig where {BusinessID}=business_id"""
    formula = f"{{BusinessID}}='{business_id}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['business']}",
                     headers=HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        raise ValueError(f"No BusinessConfig for {business_id}")
    f = recs[0]["fields"]
    return {
        "DefaultLanguage": f.get("DefaultLanguage", "en"),
        "KnowledgeBaseID": f.get("KnowledgeBase"),    # if you link a KB table
        # add more fields here as needed...
    }

def find_template(biz: str, wa: str, lang: str, msg: str) -> dict | None:
    """
    Filter WhatsAppReplyTemplate by:
      {BusinessID (from BusinessConfig)} = biz,
      {WhatsAppConfig}             = wa,
      (optionally) {Language}     = lang
    """
    # If you have a Language column, uncomment the third clause.
    formula = (
        "AND("
          "{BusinessID (from BusinessConfig)}='" + biz + "',"
          "{WhatsAppConfig}='"                 + wa  + "'"
        # + ",{Language}='"                    + lang + "'"
        ")"
    )
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}",
                     headers=HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    for rec in r.json().get("records", []):
        return rec["fields"]
    return None

def find_knowledge(biz: str, msg: str, role: str):
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}",
                     headers=HEADERS,
                     params={"filterByFormula": f"{{BusinessID}}='{biz}'"})
    r.raise_for_status()
    for rec in r.json().get("records", []):
        f = rec["fields"]
        title = f.get("Title","")
        if title and title.lower() in msg.lower():
            scripts = f.get("RoleScripts") or {}
            script  = scripts.get(role) or f.get("DefaultScript")
            img = None
            if f.get("ImageURL"):
                img = f["ImageURL"][0]["url"]
            return script, img
    return None, None

def call_claude(prompt: str, history: str, user_msg: str, model: str) -> str:
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
            {"role":"system",  "content": prompt.format(history=history, user_message=user_msg)},
            {"role":"user",    "content": user_msg}
        ]
    }
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def send_whatsapp(phone: str, text: str, token: str):
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
        json={"phone": phone, "message": text}
    )
    r.raise_for_status()

def send_image(phone: str, url: str, token: str):
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
        json={"phone": phone, "message":"", "url": url}
    )
    r.raise_for_status()

def record_history(biz, wa, phone, step, hist):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**HEADERS, "Content-Type":"application/json"},
        json={"fields":{
            "BusinessID":      biz,
            "WhatsAppConfig":  wa,
            "PhoneNumber":     phone,
            "CurrentStep":     step,
            "History":         hist
        }}
    )

def record_sales(biz, wa, phone, name, svc):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**HEADERS, "Content-Type":"application/json"},
        json={"fields":{
            "BusinessID":      biz,
            "WhatsAppConfig":  wa,
            "PhoneNumber":     phone,
            "CustomerName":    name,
            "ServiceBooked":   svc,
            "Status":          "Pending"
        }}
    )

# ───── Flask App & Webhook ─────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.route("/", methods=["GET","POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method=="GET":
        return "OK", 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", payload)

    if payload.get("object")=="message" and payload.get("event")=="message:in:new":
        data = payload["data"]
        if data.get("meta",{}).get("isGroup"):
            return jsonify(status="ignored_group"),200

        svc_no = data["toNumber"].lstrip("+")
        cus_no = data["fromNumber"].lstrip("+")
        msg    = data["body"].strip()

        # 1) Service config
        scfg = fetch_service_config(svc_no)
        biz  = scfg["BusinessID"]
        wa   = scfg["WA_ID"]
        key  = scfg["ApiKey"]

        # 2) Business settings (e.g. DefaultLanguage)
        bcfg = fetch_business_settings(biz)
        lang = bcfg["DefaultLanguage"]

        # 3) Template
        tpl = find_template(biz, wa, lang, msg)
        if tpl:
            body = tpl.get("TemplateBody","")
            send_whatsapp(cus_no, body, key)
            record_history(biz, wa, cus_no, "template", f"C:{msg}|B:{body}")
            return jsonify(status="template_sent"),200

        # 4) Knowledge
        script, img = find_knowledge(biz, msg, scfg.get("Role",""))
        if script:
            if img:
                send_image(cus_no, img, key)
            send_whatsapp(cus_no, script, key)
            record_history(biz, wa, cus_no, "knowledge", f"C:{msg}|B:{script}")
            return jsonify(status="knowledge_sent"),200

        # 5) Fallback to Claude
        history = f"Customer: {msg}"
        reply  = call_claude(bcfg.get("ClaudePrompt",""), history, msg, bcfg.get("ClaudeModel",""))
        send_whatsapp(cus_no, reply, key)
        record_history(biz, wa, cus_no, "fallback", f"C:{msg}|B:{reply}")

        if "booking" in reply.lower() or "预约" in reply:
            record_sales(biz, wa, cus_no, "Unknown","TBD")

        return jsonify(status="ok"),200

    return jsonify(status="ignored"),200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=True)
