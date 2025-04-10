import datetime
import json
import os
import secrets
from collections import deque
from threading import Thread

import bleach
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, session, redirect, url_for, flash, request
from flask_socketio import SocketIO

load_dotenv()

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
socketio = SocketIO(app)

# 从环境变量获取配置
OPENROUTER_API_URL = os.getenv('OPENROUTER_API_URL')
MODEL_NAME = os.getenv('MODEL_NAME')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

messages_cache = deque(maxlen=9999)


# OpenRouter接口调用函数
def openrouter_reply(message):
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

        # 检查响应状态码
        response.raise_for_status()

        # 解析 JSON 响应
        response_data = response.json()

        # 根据提供的 JSON 格式提取内容
        return response_data["choices"][0]["message"]["content"]

    except Exception as e:
        return f"[系统] 模型服务异常：{str(e)}"


@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = session['user']
    return render_template('index.html', user=user)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        if not request.form['user']:
            flash('用户名不能为空')
            return redirect(url_for('login'))

        if request.form['user'] == '🤖AI助手':
            flash('用户名不能为"🤖AI助手"')
            return redirect(url_for('login'))

        session['user'] = request.form['user']
        add_message(f'用户{session["user"]}加入了房间!')
        return redirect(url_for('index'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    user = session.pop('user', None)
    if user:
        add_message(f'用户{user}退出了房间')
    return redirect(url_for('login'))


def add_message(message):
    messages_cache.append(message)
    # 通知所有连接的客户端新的消息
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


if __name__ == '__main__':
    socketio.run(app, debug=False, allow_unsafe_werkzeug=True, port=5000)
