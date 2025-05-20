import datetime
import json
import os
import pickle
import secrets
from collections import deque
from threading import Thread, Lock

import bleach
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, session, redirect, url_for, flash, request
from flask_caching import Cache
from flask_cors import CORS
from flask_socketio import SocketIO, emit

load_dotenv()

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# 配置Flask-Caching
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 1800  # 默认缓存超时30分钟
cache = Cache(app)

# 配置CORS
cors = CORS(app, origins=[os.getenv('DOMAIN')])
socketio = SocketIO(app, cors_allowed_origins=[os.getenv('DOMAIN')])

# 从环境变量获取配置
OPENROUTER_API_URL = os.getenv('OPENROUTER_API_URL')
MODEL_NAME = os.getenv('MODEL_NAME')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# 本地持久化消息文件路径
MESSAGE_FILE = "messages_cache.pkl"


# 创建一个线程安全的持久化 LRU 消息缓存类
class PersistentLRUCache:
    def __init__(self, maxlen=9999, filepath=MESSAGE_FILE):
        self.filepath = filepath
        self.maxlen = maxlen
        self.lock = Lock()
        self.cache = self.load()  # 尝试从文件加载之前的消息

    def load(self):
        """加载本地持久化的消息队列；若文件不存在则创建空队列"""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "rb") as f:
                    data = pickle.load(f)
                    # 保证队列的最大长度
                    return deque(data, maxlen=self.maxlen)
            except Exception as e:
                print(f"加载消息失败: {e}")
        return deque(maxlen=self.maxlen)

    def save(self):
        """将当前队列保存到本地文件"""
        with self.lock:
            try:
                with open(self.filepath, "wb") as f:
                    pickle.dump(list(self.cache), f)
            except Exception as e:
                print(f"保存消息失败: {e}")

    def append(self, message):
        """添加消息，并自动持久化"""
        with self.lock:
            self.cache.append(message)
        self.save()  # 每次添加都同步保存；若消息量较大可考虑异步或批量保存

    def get_all(self):
        """获取所有消息的列表（按顺序保留）"""
        with self.lock:
            return list(self.cache)


# 用持久化 LRU 缓存来替换原来的消息队列
messages_cache = PersistentLRUCache(maxlen=9999)


@app.before_request
def update_user_activity():
    """在每次请求前更新用户活动时间"""
    if 'user' in session:
        username = session['user']
        if cache.get(f'user_{username}'):
            # 每次活动后重置缓存超时时间
            cache.set(f'user_{username}', True, timeout=1800)


def openrouter_reply(message):
    """调用 OpenRouter 接口获取回复内容"""
    try:
        response = requests.post(
            url=OPENROUTER_API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "model": MODEL_NAME,
                "messages": [
                    {
                        "role": "user",
                        "content": message,
                    }
                ],
            })
        )

        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    except Exception as e:
        return f"[系统] 模型服务异常：{str(e)}"


@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', user=session['user'])


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form['user'].strip()

        if not username:
            flash('用户名不能为空')
            return redirect(url_for('login'))

        if username == '🤖AI助手':
            flash('用户名不能为"🤖AI助手"')
            return redirect(url_for('login'))

        # 检查用户是否已登录
        if cache.get(f'user_{username}'):
            flash('该用户已登录，请勿重复登录')
            return redirect(url_for('login'))

        # 设置 session 和缓存
        session['user'] = username
        cache.set(f'user_{username}', True, timeout=1800)

        add_message(f'用户 {username} 加入了房间!')
        return redirect(url_for('index'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    if username := session.get('user'):
        cache.delete(f'user_{username}')
        session.pop('user')
        add_message(f'用户 {username} 退出了房间')
    return redirect(url_for('login'))


def add_message(message):
    """添加消息到队列并通过socket通知客户端（此处引入了持久化及固定长度队列）"""
    messages_cache.append(message)
    socketio.emit('new_message', message)


def async_ai_task(message):
    question = message.replace("@ai", "", 1).strip()
    reply = openrouter_reply(question)
    now = datetime.datetime.now().replace(microsecond=0).time()
    formatted_reply = f'[{now.isoformat()}] 🤖AI助手: {reply}'
    add_message(formatted_reply)


@socketio.on('send_message')
def handle_send_message(message):
    user = session.get('user', 'anonymous')
    now = datetime.datetime.now().replace(microsecond=0).time()
    cleaned_message = bleach.clean(message)

    if message.startswith("@ai"):
        formatted_message = f'[{now.isoformat()}] {user}: {cleaned_message}'
        add_message(formatted_message)
        Thread(target=async_ai_task, args=(message,)).start()
        return

    formatted_message = f'[{now.isoformat()}] {user}: {cleaned_message}'
    add_message(formatted_message)


@socketio.on('get_history')
def handle_get_history():
    if 'user' not in session:
        return

    # 获取最近50条消息（保持原来的消息顺序）
    all_messages = messages_cache.get_all()
    history = all_messages[-50:] if len(all_messages) > 50 else all_messages

    # 发送给请求的客户端
    emit('history_messages', history)


def runserver():
    socketio.run(app, debug=False, allow_unsafe_werkzeug=True, port=5000)


if __name__ == '__main__':
    runserver()
