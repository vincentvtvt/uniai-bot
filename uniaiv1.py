import os
import json
import time
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Flask app
app = Flask(__name__)

# Configuration
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AI_CONFIG_TABLE = os.getenv('AI_CONFIG_TABLE', 'AI_Role_Prompt_Config')
CUSTOMER_HISTORY_TABLE = os.getenv('CUSTOMER_HISTORY_TABLE', 'Customer_History')
TEMPLATE_TABLE = os.getenv('TEMPLATE_TABLE', 'WhatsApp_Reply_Templates')

CLAUDE_API_KEY = os.getenv('CLAUDE_API_KEY')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'claude-3-7-sonnet-20250219')
CLAUDE_API_URL = "https://api.anthropic.com/v1/chat/completions"

MAX_HISTORY = int(os.getenv('MAX_HISTORY', '5'))

# Airtable client
class AirtableClient:
    def __init__(self, api_key, base_id):
        self.api_key = api_key
        self.base_id = base_id
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

    def fetch_records(self, table, filter_formula=None):
        url = f'https://api.airtable.com/v0/{self.base_id}/{table}'
        params = {}
        if filter_formula:
            params['filterByFormula'] = filter_formula
        resp = requests.get(url, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json().get('records', [])

    def create_record(self, table, fields):
        url = f'https://api.airtable.com/v0/{self.base_id}/{table}'
        resp = requests.post(url, headers=self.headers, json={'fields': fields})
        resp.raise_for_status()
        return resp.json()

    def update_record(self, table, record_id, fields):
        url = f'https://api.airtable.com/v0/{self.base_id}/{table}/{record_id}'
        resp = requests.patch(url, headers=self.headers, json={'fields': fields})
        resp.raise_for_status()
        return resp.json()

# Instantiate Airtable client
airtable = AirtableClient(AIRTABLE_API_KEY, AIRTABLE_BASE_ID)

# Language detection (basic)
def detect_language(text: str) -> str:
    return 'Mandarin' if any('\u4e00' <= ch <= '\u9fff' for ch in text) else 'Malay' if ' ?' in text else 'English'

# Fetch business config by WhatsApp number
def get_business_config(whatsapp_number: str) -> dict:
    formula = f"{{whatsapp_number}}='{whatsapp_number}'"
    recs = airtable.fetch_records(AI_CONFIG_TABLE, filter_formula=formula)
    if not recs:
        raise ValueError(f'No config for number: {whatsapp_number}')
    fields = recs[0]['fields']
    # Parse JSON blobs
    fields['tools'] = json.loads(fields.get('tools_json', '[]'))
    fields['product_info'] = json.loads(fields.get('product_info_blob', '[]'))
    fields['faq_list'] = json.loads(fields.get('faq_blob', '[]'))
    fields['flow_list'] = json.loads(fields.get('flow_blob', '[]'))
    return fields

# Customer history
def get_or_create_customer(phone: str, business_id: str) -> dict:
    formula = f"AND({{phone_number}}='{phone}',{{business_id}}='{business_id}')"
    recs = airtable.fetch_records(CUSTOMER_HISTORY_TABLE, filter_formula=formula)
    if recs:
        return recs[0]
    new = airtable.create_record(CUSTOMER_HISTORY_TABLE, {
        'phone_number': phone,
        'business_id': business_id,
        'conversation_history': json.dumps([]),
        'current_step': 1,
        'status': 'New',
        'partial_data': json.dumps({})
    })
    return new

# Append to conversation history
def append_history(record: dict, role: str, message: str) -> list:
    history = json.loads(record['fields'].get('conversation_history', '[]'))
    history.append({'role': role, 'message': message, 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ')})
    history = history[-MAX_HISTORY:]
    airtable.update_record(CUSTOMER_HISTORY_TABLE, record['id'], {
        'conversation_history': json.dumps(history),
        'last_interaction': time.strftime('%Y-%m-%dT%H:%M:%SZ')
    })
    return history

# Search product info
def search_product_info(products: list, msg: str) -> dict:
    key = msg.lower()
    for p in products:
        if p['id'].lower() in key or p['name'].lower() in key:
            return p
    return None

# Search FAQ
def search_faq(faq_list: list, msg: str) -> str:
    key = msg.lower()
    for faq in faq_list:
        if faq['q'].lower() in key:
            return faq['a']
    return None

# Search flow triggers
def search_flow(flow_list: list, msg: str) -> tuple:
    key = msg.lower()
    for flow in flow_list:
        for step in flow.get('steps', []):
            if step.get('trigger','').lower() in key:
                return flow['flow_id'], step
    return None, None

# Fetch template record
def fetch_template(business_id: str, type_: str, language: str, step: int) -> dict:
    formula = (
        f"AND({{business_id}}='{business_id}',"
         f"{{type}}='{type_}',"
         f"{{language}}='{language}',"
         f"{{step}}={step})"
    )
    recs = airtable.fetch_records(TEMPLATE_TABLE, filter_formula=formula)
    return recs[0]['fields'] if recs else None

# Build Claude prompt
def build_claude_prompt(config: dict, history: list, msg: str) -> dict:
    return {
        'business_info': {
            'business_id': config['business_id'],
            'business_name': config['business_name'],
            'industry': config['industry'],
            'main_function': config['main_function'],
            'agent_name': config['agent_name'],
            'default_language': config['default_language'],
            'conversation_tone': config['conversation_tone']
        },
        'salesperson_knowledge': {
            'conversation_history': history,
            'partial_data': json.loads(config.get('partial_data','{}')),
            'current_needs': config.get('current_needs',''),
            'current_message': msg
        },
        'tools': config['tools'],
        'task_instruction': config['task_instruction']
    }

# Call Claude API
def call_claude_system(prompt_json: dict) -> str:
    headers = {'x-api-key': CLAUDE_API_KEY, 'Content-Type': 'application/json'}
    system_msg = json.dumps(prompt_json)
    messages = [
        {'role':'system','content': system_msg},
        {'role':'user','content': prompt_json['salesperson_knowledge']['current_message']}
    ]
    payload = {
        'model': CLAUDE_MODEL,
        'messages': messages,
        'max_tokens_to_sample': 512,
        'temperature': 0.7
    }
    resp = requests.post(CLAUDE_API_URL, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content'].strip()

# Send WhatsApp message
def send_whatsapp(config: dict, to: str, text: str=None, media_type: str=None, media_url: str=None):
    url = config['whatsapp_api_url']
    headers = {'Authorization': f"Bearer {config['whatsapp_api_key']}"}
    payload = {'phonenumber': to}
    if media_type and media_url:
        payload['type'] = media_type
        payload[media_type] = media_url
    else:
        payload['text'] = text
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()

# Extract phone, message, and to-number
def extract_payload(payload: dict) -> tuple:
    # 360messenger format
    if 'entry' in payload:
        entry = payload['entry'][0]
        changes = entry.get('changes',[{}])[0]
        value = changes.get('value',{})
        contacts = value.get('contacts',[{}])[0]
        msgs = value.get('messages',[{}])[0]
        return contacts.get('wa_id'), msgs.get('text',{}).get('body'), value.get('metadata',{}).get('phone_number_id')
    # UltraMsg or others
    phone = payload.get('from') or payload.get('user')
    msg = payload.get('message') or payload.get('text')
    to = payload.get('to')
    return phone, msg, to

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    phone, msg, to_number = extract_payload(data)
    if not phone or not msg or not to_number:
        return jsonify({'status':'ignored'}),200

    # Multi-tenant: fetch config
    config = get_business_config(to_number)
    business_id = config['business_id']

    # Customer record
    cust_rec = get_or_create_customer(phone, business_id)
    history = append_history(cust_rec, 'user', msg)

    # Detect language
    lang = detect_language(msg)

    # 1) Product info
    prod = search_product_info(config['product_info'], msg)
    if prod:
        text = f"Our {prod['name']} is priced at {prod['price']}."
        send_whatsapp(config, phone, text=text)
        append_history(cust_rec, 'bot', text)
        return jsonify({'status':'ok'}),200

    # 2) FAQ lookup
    faq_ans = search_faq(config['faq_list'], msg)
    if faq_ans:
        # Translate if needed
        send_whatsapp(config, phone, text=faq_ans)
        append_history(cust_rec, 'bot', faq_ans)
        return jsonify({'status':'ok'}),200

    # 3) Flow trigger
    flow_id, step = search_flow(config['flow_list'], msg)
    if flow_id and step:
        tpl = fetch_template(business_id, 'sales_flow', lang, step['step'])
        if tpl:
            parts = [tpl.get('message_part_1'), tpl.get('message_part_2')]
            next_step = step['step'] + 1
            airtable.update_record(CUSTOMER_HISTORY_TABLE, cust_rec['id'], {'current_step': next_step})
            for part in parts:
                if part:
                    send_whatsapp(config, phone, text=part)
                    time.sleep(0.5)
            append_history(cust_rec, 'bot', '\n'.join(filter(None, parts)))
            return jsonify({'status':'ok'}),200

    # 4) Claude fallback
    prompt_json = build_claude_prompt(config, history, msg)
    cl_reply = call_claude_system(prompt_json)
    send_whatsapp(config, phone, text=cl_reply)
    append_history(cust_rec, 'bot', cl_reply)

    return jsonify({'status':'ok'}),200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',5000)), debug=False)
