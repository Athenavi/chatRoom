import asyncio
import datetime
import json
import os
import pickle
import secrets
from collections import deque
from threading import Lock
from typing import List, Optional

import bleach
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="FastAPI Chat Room", version="1.0.0")

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv('DOMAIN', '*')],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件和模板
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# JWT配置
SECRET_KEY = secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# API配置
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
MODEL_NAME = os.getenv('MODEL_NAME', 'deepseek/deepseek-r1-0528:free')
SITE_URL = os.getenv('SITE_URL', 'http://localhost:8000')
SITE_NAME = os.getenv('SITE_NAME', 'FastAPI Chat Room')

# 初始化OpenAI客户端用于OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# 本地持久化消息文件路径
MESSAGE_FILE = "messages_cache.pkl"


# 数据模型
class MessageRequest(BaseModel):
    message: str


class LoginRequest(BaseModel):
    username: str


# 创建一个线程安全的持久化 LRU 消息缓存类
class PersistentLRUCache:
    def __init__(self, maxlen=9999, filepath=MESSAGE_FILE):
        self.filepath = filepath
        self.maxlen = maxlen
        self.lock = Lock()
        self.cache = self.load()

    def load(self):
        """加载本地持久化的消息队列；若文件不存在则创建空队列"""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "rb") as f:
                    data = pickle.load(f)
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
        self.save()

    def get_all(self):
        """获取所有消息的列表（按顺序保留）"""
        with self.lock:
            return list(self.cache)


# WebSocket连接管理器
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.active_users: dict = {}  # websocket -> username mapping

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.active_users[websocket] = username

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if websocket in self.active_users:
            del self.active_users[websocket]

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                # 连接已断开，移除它
                self.disconnect(connection)

    def get_username(self, websocket: WebSocket) -> Optional[str]:
        return self.active_users.get(websocket)

    def is_user_online(self, username: str) -> bool:
        return username in self.active_users.values()


# 全局实例
messages_cache = PersistentLRUCache(maxlen=9999)
manager = ConnectionManager()


# JWT工具函数
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        return username
    except JWTError:
        return None


# AI回复函数
async def openrouter_reply(message: str) -> str:
    """调用 OpenRouter 接口获取回复内容"""
    try:
        completion = client.chat.completions.create(
            extra_headers={
                "HTTP-Referer": SITE_URL,
                "X-Title": SITE_NAME,
            },
            model=MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": message
                }
            ]
        )

        # 添加适当的空值检查以响应
        if completion.choices and len(completion.choices) > 0:
            content = completion.choices[0].message.content
            if content:
                return content
            else:
                return "[系统] AI助手暂时无法回复，请稍后再试"
        else:
            return "[系统] AI助手响应异常，请稍后再试"

    except Exception as e:
        print(f"OpenRouter API Error: {str(e)}")
        return f"[系统] 模型服务异常：{str(e)}"


async def add_message(message: str):
    """添加消息到队列并通过WebSocket通知所有客户端"""
    messages_cache.append(message)
    await manager.broadcast(json.dumps({"type": "new_message", "message": message}))


async def async_ai_task(message: str):
    """异步处理AI回复"""
    question = message.replace("@ai", "", 1).strip()
    reply = await openrouter_reply(question)
    now = datetime.datetime.now().replace(microsecond=0).time()
    formatted_reply = f'[{now.isoformat()}] 🤖AI助手: {reply}'
    await add_message(formatted_reply)


# 路由
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/api/login")
async def login(username: str = Form(...)):
    username = username.strip()

    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")

    if username == '🤖AI助手':
        raise HTTPException(status_code=400, detail='用户名不能为"🤖AI助手"')

    # 检查用户是否已在线
    if manager.is_user_online(username):
        raise HTTPException(status_code=400, detail="该用户已登录，请勿重复登录")

    # 创建JWT token
    access_token = create_access_token(data={"sub": username})

    # 添加加入消息
    await add_message(f'用户 {username} 加入了房间!')

    return {"access_token": access_token, "token_type": "bearer", "username": username}


@app.post("/api/logout")
async def logout(request: Request):
    # 从header获取token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        username = verify_token(token)
        if username:
            await add_message(f'用户 {username} 退出了房间')

    return {"message": "已退出"}


@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    # 验证token
    username = verify_token(token)
    if not username:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, username)

    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            if message_data["type"] == "send_message":
                message = message_data["message"]
                now = datetime.datetime.now().replace(microsecond=0).time()
                cleaned_message = bleach.clean(message)

                if message.startswith("@ai"):
                    formatted_message = f'[{now.isoformat()}] {username}: {cleaned_message}'
                    await add_message(formatted_message)
                    # 异步处理AI回复
                    asyncio.create_task(async_ai_task(message))
                else:
                    formatted_message = f'[{now.isoformat()}] {username}: {cleaned_message}'
                    await add_message(formatted_message)

            elif message_data["type"] == "get_history":
                # 获取最近50条消息
                all_messages = messages_cache.get_all()
                history = all_messages[-50:] if len(all_messages) > 50 else all_messages

                await manager.send_personal_message(
                    json.dumps({"type": "history_messages", "messages": history}),
                    websocket
                )

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await add_message(f'用户 {username} 断开了连接')


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
