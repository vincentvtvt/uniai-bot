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
    logging.warning("CLAUDE_API_KEY not set; some features will be disabled")

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

# Utility: detect language
def detect_language(text):
    return 'zh' if re.search(r'[一-鿿]', text) else 'en'

# Fetch WhatsApp configuration by service number and customer fallback

def fetch_config_by_service(service_number):
    formula = f"ServiceNumber='{service_number}'"
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params={"filterByFormula": formula})
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if not recs:
        raise ValueError(f"No config for service number: {service_number}")
    rec = recs[0]
    fields = rec['fields']
    fields['RecordID'] = rec['id']
    biz = fields.get('Business')
    fields['BusinessID'] = biz[0] if isinstance(biz, list) and biz else biz
    return fields


def fetch_config_by_customer(customer_number):
    formula = f"WhatsAppNumber='{customer_number}'"
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}", headers=HEADERS, params={"filterByFormula": formula})
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if not recs:
        raise ValueError(f"No config for customer number: {customer_number}")
    rec = recs[0]
    fields = rec['fields']
    fields['RecordID'] = rec['id']
    biz = fields.get('Business')
    fields['BusinessID'] = biz[0] if isinstance(biz, list) and biz else biz
    return fields

# Lookup templates
def find_template(business_id, config_id, msg, lang):
    formula = f"AND(Business='{business_id}', WhatsAppConfig='{config_id}', Language='{lang}')"
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}", headers=HEADERS, params={"filterByFormula": formula})
    resp.raise_for_status()
    for rec in resp.json().get('records', []):
        flds = rec['fields']
        if flds.get('Step', '').lower() in msg.lower():
            return flds
    return None

# Lookup knowledge
def find_knowledge(business_id, msg, role):
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}", headers=HEADERS, params={"filterByFormula": f"Business='{business_id}'"})
    resp.raise_for_status()
    for rec in resp.json().get('records', []):
        flds = rec['fields']
        if flds.get('Title', '').lower() in msg.lower():
            scripts = json.loads(flds.get('RoleScripts', '{}'))
            script = scripts.get(role) or flds.get('DefaultScript')
            imgs = flds.get('ImageURL') or []
            return script, (imgs[0]['url'] if imgs else None)
    return None, None

# Call Claude fallback with classic Anthropic API
def call_claude(user_msg, history, prompt, model):
    if not CLAUDE_API_KEY:
        raise RuntimeError('CLAUDE_API_KEY not configured')
    system_prompt    = prompt.format(history=history, user_message=user_msg)
    human_prompt     = f"Human: {user_msg}"
    assistant_prompt = "Assistant:"
    full_prompt      = f"""{system_prompt}
{human_prompt}
{assistant_prompt}"""
    payload = {
        'model': model,
        'prompt': full_prompt,
        'max_tokens_to_sample': 512,
        'temperature': 0.7
    }
    resp = requests.post(
        'https://api.anthropic.com/v1/complete',
        headers={
            'x-api-key': CLAUDE_API_KEY,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json'
        },
        json=payload
    )
    resp.raise_for_status()
    return resp.json().get('completion', '').strip()

# Send messages
def send_whatsapp(phone, text, api_key):
    resp = requests.post(
        'https://api.wassenger.com/v1/messages',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'phone': phone, 'message': text}
    )
    resp.raise_for_status()
    return resp.json()


def send_image(phone, url, api_key):
    resp = requests.post(
        'https://api.wassenger.com/v1/messages',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'phone': phone, 'message': '', 'url': url}
    )
    resp.raise_for_status()
    return resp.json()

# Record history and sales
def record_history(biz, cfg_id, phone, step, hist):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**HEADERS, 'Content-Type': 'application/json'},
        json={'fields': {'Business': biz, 'WhatsAppConfig': cfg_id, 'PhoneNumber': phone, 'CurrentStep': step, 'History': hist}}
    )

def record_sales(biz, cfg_id, phone, name, svc):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**HEADERS, 'Content-Type': 'application/json'},
        json={'fields': {'Business': biz, 'WhatsAppConfig': cfg_id, 'PhoneNumber': phone, 'CustomerName': name, 'ServiceBooked': svc, 'Status': 'Pending'}}
    )

# Health-check & handler
@app.route('/', methods=['GET', 'POST'])
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return jsonify({'status': 'ok'})
    payload         = request.get_json(force=True)
    logging.info('Incoming payload: %s', json.dumps(payload))
    data            = payload.get('data', {}) if 'data' in payload else payload
    customer_number = data.get('fromNumber', '').strip().lstrip('+')
    service_number  = data.get('toNumber', '').strip().lstrip('+')
    try:
        cfg = fetch_config_by_service(service_number)
    except ValueError:
        cfg = fetch_config_by_customer(customer_number)
    if data.get('meta', {}).get('isGroup'):
        logging.info('Ignoring group message')
        return jsonify({'status': 'ignored_group'})
    msg         = data.get('body', '').strip()
    wa          = customer_number
    biz_id      = cfg.get('BusinessID')
    cfg_id      = cfg.get('RecordID')
    role        = cfg.get('Role')
    prompt      = cfg.get('ClaudePrompt')
    model       = cfg.get('ClaudeModel')
    api_key     = cfg.get('WASSENGER_API_KEY')
    lang        = detect_language(msg)
    tmpl        = find_template(biz_id, cfg_id, msg, lang)
    if tmpl:
        if tmpl.get('ImageURL'):
            send_image(wa, tmpl.get('ImageURL'), api_key)
        send_whatsapp(wa, tmpl.get('TemplateBody'), api_key)
        record_history(biz_id, cfg_id, wa, tmpl.get('Step', 'step'), f'User:{msg}|Bot:{tmpl.get("TemplateBody")}')
        return jsonify({'status': 'template_sent'})
    script, img = find_knowledge(biz_id, msg, role)
    if script:
        if img:
            send_image(wa, img, api_key)
        send_whatsapp(wa, script, api_key)
        record_history(biz_id, cfg_id, wa, 'knowledge', f'User:{msg}|Bot:{script}')
        return jsonify({'status': 'knowledge_sent'})
    history_text = f'User: {msg}'
    reply        = call_claude(msg, history_text, prompt, model)
    send_whatsapp(wa, reply, api_key)
    record_history(biz_id, cfg_id, wa, 'fallback', f'User:{msg}|Bot:{reply}')
    if any(k in reply.lower() for k in ('booking', '预约')):
        record_sales(biz_id, cfg_id, wa, 'Unknown', 'TBD')
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
