from flask import Flask, render_template, request, session, redirect, Response
from flask_caching import Cache
import datetime
import time

app = Flask(__name__)
app.secret_key = 'hard_key'
cache = Cache(app, config={'CACHE_TYPE': 'simple', 'CACHE_DEFAULT_TIMEOUT': 60})
messages_cache = []
clients = []

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

@app.route('/send', methods=['POST'])
def post_message():
    message = request.form['message']
    user = session.get('user', 'anonymous')
    now = datetime.datetime.now().replace(microsecond=0).time()
    add_message(f'[{now.isoformat()}] {user}: {message}')
    return Response(status=204)

def add_message(message):
    messages_cache.append(message)
    # Limit cache size
    if len(messages_cache) > 100:  # Keep the most recent 100 messages
        messages_cache.pop(0)

    # Notify all clients about new message
    for client in clients:
        client.append(message)

@app.route('/stream')
def stream():
    def generate():
        client_message_cache = []
        while True:
            if messages_cache:
                # Only send new messages to this client
                new_messages = messages_cache[len(client_message_cache):]
                if new_messages:
                    for msg in new_messages:
                        yield f"data: {msg}\n\n"
                    client_message_cache.extend(new_messages)
            time.sleep(1)

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, threaded=True)