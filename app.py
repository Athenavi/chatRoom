from flask import Flask, render_template, request, session, redirect
from flask_socketio import SocketIO, emit
from collections import deque
import datetime

app = Flask(__name__)
app.secret_key = 'hard_key'
socketio = SocketIO(app)

messages_cache = deque(maxlen=100)  # 使用 deque 限制缓存大小为100条

@app.route('/')
def index():
    if 'user' not in session:
        return redirect('/login')
    user = session['user']
    return render_template('index.html', user=user)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect('/')
    if request.method == 'POST':
        session['user'] = request.form['user']
        add_message(f'用户{session["user"]}加入了房间!')
        return redirect('/')
    return render_template('login.html')

@app.route('/logout')
def logout():
    user = session.pop('user', None)
    if user:
        add_message(f'用户{user}退出了房间')
    return redirect('/login')

def add_message(message):
    messages_cache.append(message)
    # 通知所有连接的客户端新的消息
    socketio.emit('new_message', message)

@socketio.on('send_message')
def handle_send_message(message):
    user = session.get('user', 'anonymous')
    now = datetime.datetime.now().replace(microsecond=0).time()
    formatted_message = f'[{now.isoformat()}] {user}: {message}'
    add_message(formatted_message)

if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)