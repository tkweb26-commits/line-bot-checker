import os
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify
import requests

app = Flask(__name__)

# LINE 設定
TOKEN    = os.environ.get('LINE_TOKEN', '')
SECRET   = os.environ.get('LINE_SECRET', '')
GROUP_ID = os.environ.get('GROUP_ID', '')
MENTION_USERS = [u.strip() for u in os.environ.get('MENTION_USERS', '').split(',') if u.strip()]
MENTION_NAMES = [n.strip() for n in os.environ.get('MENTION_NAMES', '').split(',') if n.strip()]

# Upstash Redis 設定
UPSTASH_URL   = os.environ.get('UPSTASH_REDIS_REST_URL', '')
UPSTASH_TOKEN = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')

# 偵測關鍵字（只要檔名含此字串即視為已上傳）
KEYWORD = '南工區-新興段三小段1756案'

# 台灣時間 UTC+8
CST = timezone(timedelta(hours=8))

# 備用記憶體（Upstash 不可用時）
_mem_upload = {}
_mem_remind = {}

# 蒐集群組與成員資訊
seen_groups = set()
seen_users  = set()


# ── 時間工具 ──────────────────────────────────────────────────
def now_cst():
    return datetime.now(CST)

def today_str():
    return now_cst().strftime('%Y-%m-%d')

def current_month():
    return now_cst().month


# ── Upstash Redis 工具 ────────────────────────────────────────
def _redis_headers():
    return {'Authorization': f'Bearer {UPSTASH_TOKEN}'}

def redis_get(key):
    if not UPSTASH_URL:
        return None
    try:
        r = requests.get(f'{UPSTASH_URL}/get/{key}',
                         headers=_redis_headers(), timeout=5)
        if r.status_code == 200:
            return r.json().get('result')
    except Exception as e:
        print(f'[Redis GET error] {e}')
    return None

def redis_set(key, value, ex=90000):
    """設定值，預設 25 小時後自動過期"""
    if not UPSTASH_URL:
        return
    try:
        requests.get(f'{UPSTASH_URL}/set/{key}/{value}',
                     params={'ex': ex},
                     headers=_redis_headers(), timeout=5)
    except Exception as e:
        print(f'[Redis SET error] {e}')


# ── 上傳狀態（持久化）─────────────────────────────────────────
def is_uploaded():
    result = redis_get(f'upload:{today_str()}')
    if result:
        return True
    return _mem_upload.get(today_str(), False)

def mark_uploaded():
    redis_set(f'upload:{today_str()}', '1')
    _mem_upload[today_str()] = True


# ── 最後提醒時間（持久化）────────────────────────────────────
def get_last_remind():
    val = redis_get(f'remind:{today_str()}')
    if val:
        try:
            return datetime.fromisoformat(val)
        except Exception:
            pass
    return _mem_remind.get(today_str())

def set_last_remind(dt: datetime):
    redis_set(f'remind:{today_str()}', dt.isoformat())
    _mem_remind[today_str()] = dt


# ── 簽章驗證 ──────────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    h = hmac.new(SECRET.encode('utf-8'), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode('utf-8'), signature)


# ── 健康檢查 ──────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def index():
    return 'LINE Bot Server Running', 200


# ── LINE Webhook ───────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()

    if SECRET and not verify_signature(body, signature):
        abort(400, 'Invalid signature')

    data = json.loads(body)

    for event in data.get('events', []):
        src = event.get('source', {})
        gid = src.get('groupId', '')
        uid = src.get('userId', '')

        if gid: seen_groups.add(gid)
        if uid: seen_users.add(uid)

        # 偵測 Excel 檔案上傳
        if event.get('type') == 'message':
            msg = event.get('message', {})
            if msg.get('type') == 'file':
                fname = msg.get('fileName', '')
                print(f'[FILE] {fname} | group={gid} | user={uid}')
                if KEYWORD in fname and fname.lower().endswith('.xlsx'):
                    mark_uploaded()
                    print(f'[✅] 已上傳：{fname}（{today_str()}）')

    return 'OK', 200


# ── 狀態查詢 ───────────────────────────────────────────────────
@app.route('/status', methods=['GET'])
def status():
    last = get_last_remind()
    return jsonify({
        'today'       : today_str(),
        'uploaded'    : is_uploaded(),
        'last_remind' : str(last) if last else '尚未提醒',
        'group_ids'   : list(seen_groups),
        'user_ids'    : list(seen_users),
        'config': {
            'GROUP_ID'     : GROUP_ID or '(未設定)',
            'MENTION_USERS': MENTION_USERS or ['(未設定)'],
            'MENTION_NAMES': MENTION_NAMES or ['(未設定)'],
        }
    })


# ── 排程呼叫：檢查並提醒 ───────────────────────────────────────
@app.route('/check', methods=['GET', 'POST'])
def check():
    now   = now_cst()
    today = today_str()

    # 週日跳過
    if now.weekday() == 6:
        return jsonify({'skip': '週日不檢查'})

    # 已上傳，不提醒
    if is_uploaded():
        return jsonify({'status': '已上傳', 'date': today})

    # 30 分鐘冷卻，避免重複提醒
    last = get_last_remind()
    if last and (now - last).total_seconds() < 30 * 60:
        remaining = int(30 - (now - last).total_seconds() / 60)
        return jsonify({'status': f'冷卻中，{remaining} 分鐘後可再次提醒'})

    if not GROUP_ID:
        return jsonify({'error': 'GROUP_ID 尚未設定'}), 500

    # 動態取得當前月份
    month = current_month()
    time_str = now.strftime('%H:%M')

    # 建立 textV2 @mention 訊息
    mention_placeholders = ' '.join([f'{{m{i}}}' for i in range(len(MENTION_USERS))])
    body_text = (
        mention_placeholders
        + f'\n⚠️ 提醒（{time_str}）\n'
        + '今日尚未上傳以下檔案，\n'
        + '請盡快上傳，謝謝！🙏\n\n'
        + f'📄 南工區-新興段三小段1756案-\n'
        + f'租約工分攤總表({month}月)聰典.xlsx\n'
        + f'📄 南工區-新興段三小段1756案-\n'
        + f'打石工分攤總表({month}月)有力.xlsx'
    )

    substitution = {
        f'm{i}': {
            'type'     : 'mention',
            'mentionee': {'type': 'user', 'userId': uid}
        }
        for i, uid in enumerate(MENTION_USERS)
    }

    message = {
        'type'        : 'textV2',
        'text'        : body_text,
        'substitution': substitution
    }

    resp = requests.post(
        'https://api.line.me/v2/bot/message/push',
        headers={
            'Authorization': f'Bearer {TOKEN}',
            'Content-Type' : 'application/json'
        },
        json={'to': GROUP_ID, 'messages': [message]},
        timeout=10
    )

    if resp.status_code == 200:
        set_last_remind(now)

    print(f'[REMIND] {resp.status_code} {resp.text}')
    return jsonify({
        'status'  : '提醒已發送' if resp.status_code == 200 else '發送失敗',
        'code'    : resp.status_code,
        'response': resp.text
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
