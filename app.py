from flask import Flask, render_template, request, session, redirect, url_for, flash
from flask_socketio import SocketIO, emit
from collections import deque
import datetime
import bleach
import secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

socketio = SocketIO(app)

messages_cache = deque(maxlen=9999)


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


@socketio.on('send_message')
def handle_send_message(message):
    user = session.get('user', 'anonymous')
    now = datetime.datetime.now().replace(microsecond=0).time()
    # 使用 bleach 清理消息内容
    cleaned_message = bleach.clean(message)
    formatted_message = f'[{now.isoformat()}] {user}: {cleaned_message}'
    add_message(formatted_message)


if __name__ == '__main__':
    socketio.run(app, debug=False, allow_unsafe_werkzeug=True, port=5000)
