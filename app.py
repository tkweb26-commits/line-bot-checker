import os
import json
import hmac
import hashlib
import base64
from datetime import datetime, date, timezone, timedelta
from flask import Flask, request, abort, jsonify
import requests

app = Flask(__name__)

# 環境變數
TOKEN    = os.environ.get('LINE_TOKEN', '')
SECRET   = os.environ.get('LINE_SECRET', '')
GROUP_ID = os.environ.get('GROUP_ID', '')
# 以逗號分隔的成員 User ID，例如: Uabc123,Uxyz456
MENTION_USERS = [u.strip() for u in os.environ.get('MENTION_USERS', '').split(',') if u.strip()]
# 對應 MENTION_USERS 的顯示名稱，逗號分隔，例如: 修荃,姿萍
MENTION_NAMES = [n.strip() for n in os.environ.get('MENTION_NAMES', '').split(',') if n.strip()]

KEYWORD = '南工區-新興段三小段1756案-'

# 台灣時間 UTC+8
CST = timezone(timedelta(hours=8))

# 記錄當天是否已上傳
upload_status = {}   # { 'YYYY-MM-DD': True }

# 記錄最後一次提醒時間（防止重複發送）
last_remind = {}     # { 'YYYY-MM-DD': datetime }

# 自動蒐集群組與成員資訊
seen_groups = set()
seen_users  = set()


def today_str():
    return datetime.now(CST).strftime('%Y-%m-%d')


def verify_signature(body: bytes, signature: str) -> bool:
    h = hmac.new(SECRET.encode('utf-8'), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode('utf-8'), signature)


def get_display_name(user_id: str) -> str:
    """從 LINE API 取得群組成員的顯示名稱"""
    if not GROUP_ID or not TOKEN:
        return '同仁'
    try:
        resp = requests.get(
            f'https://api.line.me/v2/bot/group/{GROUP_ID}/member/{user_id}',
            headers={'Authorization': f'Bearer {TOKEN}'},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json().get('displayName', '同仁')
    except Exception:
        pass
    return '同仁'


# ── 健康檢查（Keep-alive ping 用）──────────────────────────────
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
    today = today_str()

    for event in data.get('events', []):
        src = event.get('source', {})
        gid = src.get('groupId', '')
        uid = src.get('userId', '')

        if gid:
            seen_groups.add(gid)
        if uid:
            seen_users.add(uid)

        # 偵測檔案訊息
        if event.get('type') == 'message':
            msg = event.get('message', {})
            if msg.get('type') == 'file':
                fname = msg.get('fileName', '')
                print(f'[FILE] {fname} | group={gid} | user={uid}')
                if KEYWORD in fname:
                    upload_status[today] = True
                    print(f'[✅] 關鍵字符合，已標記 {today} 為已上傳')

    return 'OK', 200


# ── 狀態查詢 ───────────────────────────────────────────────────
@app.route('/status', methods=['GET'])
def status():
    today = today_str()
    return jsonify({
        'today'       : today,
        'uploaded'    : upload_status.get(today, False),
        'last_remind' : str(last_remind.get(today, '尚未提醒')),
        'history'     : upload_status,
        'group_ids'   : list(seen_groups),
        'user_ids'    : list(seen_users),
        'config': {
            'GROUP_ID'     : GROUP_ID or '(未設定)',
            'MENTION_USERS': MENTION_USERS or ['(未設定)'],
        }
    })


# ── 排程呼叫：檢查並提醒 ───────────────────────────────────────
@app.route('/check', methods=['GET', 'POST'])
def check():
    now   = datetime.now(CST)
    today = today_str()

    # 週日跳過
    if now.weekday() == 6:
        return jsonify({'skip': '週日不檢查'})

    # 已上傳，不提醒
    if upload_status.get(today, False):
        return jsonify({'status': '已上傳', 'date': today})

    # 30 分鐘冷卻：防止重複提醒
    last = last_remind.get(today)
    if last and (now - last).total_seconds() < 30 * 60:
        remaining = int(30 - (now - last).total_seconds() / 60)
        return jsonify({'status': f'冷卻中，{remaining} 分鐘後可再次提醒'})

    if not GROUP_ID:
        return jsonify({'error': 'GROUP_ID 尚未設定'}), 500

    # 取得成員顯示名稱並建立 @mention 訊息
    text_parts = []
    mentions   = []
    pos = 0

    for i, uid in enumerate(MENTION_USERS):
        name = MENTION_NAMES[i] if i < len(MENTION_NAMES) else f'同仁{i+1}'
        placeholder = f'@{name} '
        mentions.append({
            'index'    : pos,
            'length'   : len(f'@{name}'),
            'mentionee': {'type': 'user', 'userId': uid}
        })
        text_parts.append(placeholder)
        pos += len(placeholder)

    time_str = now.strftime('%H:%M')
    body_text = (
        ''.join(text_parts)
        + f'\n⚠️ 提醒（{time_str}）\n'
        + '今日尚未上傳以下檔案，\n'
        + '請盡快上傳，謝謝！🙏\n\n'
        + '📄 南工區-新興段三小段1756案-\n'
        + '租約工分攤總表(6月)聰典.xlsx\n'
        + '📄 南工區-新興段三小段1756案-\n'
        + '打石工分攤總表(6月)有力.xlsx'
    )

    message = {'type': 'text', 'text': body_text}
    if mentions:
        message['mentions'] = mentions

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
        last_remind[today] = now

    print(f'[REMIND] {resp.status_code} {resp.text}')
    return jsonify({
        'status'  : '提醒已發送' if resp.status_code == 200 else '發送失敗',
        'code'    : resp.status_code,
        'response': resp.text
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
