import asyncio
import datetime
import json
import os
import secrets
import asyncpg
import random
import logging
from collections import deque
from threading import Lock
from typing import List, Optional, Dict, Tuple
from contextlib import asynccontextmanager

import bleach
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Form, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
from pydantic import BaseModel, validator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chatroom.log")
    ]
)
logger = logging.getLogger("chatroom")

load_dotenv()


# 应用生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库连接池
    await db_router.init_pools()

    # 创建必要的数据库表（如果不存在）
    try:
        async with db_router.primary_pool.acquire() as conn:
            # 检查表是否存在，如果不存在则创建
            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'chat_messages')"
            )

            if not table_exists:
                logger.info("创建数据库表...")
                await conn.execute("""
                                   CREATE TABLE chat_messages
                                   (
                                       id           BIGSERIAL PRIMARY KEY,
                                       username     VARCHAR(100) NOT NULL,
                                       message      TEXT         NOT NULL,
                                       message_type VARCHAR(20)              DEFAULT 'user',
                                       created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                                       expires_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '30 days'
                                   )
                                   """)

                # 创建索引
                await conn.execute("CREATE INDEX idx_chat_messages_created_at ON chat_messages (created_at)")
                await conn.execute("CREATE INDEX idx_chat_messages_username ON chat_messages (username)")
                await conn.execute("CREATE INDEX idx_chat_messages_expires_at ON chat_messages (expires_at)")
                logger.info("数据库表和索引创建完成")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")

    yield

    # 关闭时清理连接池
    if db_router.primary_pool:
        await db_router.primary_pool.close()

    for pool in db_router.replica_pools:
        await pool.close()


app = FastAPI(
    title="FastAPI Chat Room",
    version="1.0.0",
    lifespan=lifespan
)

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

# 数据库配置
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:password@localhost:5432/chatroom')
PRIMARY_DB_URL = os.getenv('PRIMARY_DB_URL', DATABASE_URL)
REPLICA_DB_URLS = os.getenv('REPLICA_DB_URLS', '').split(',') if os.getenv('REPLICA_DB_URLS') else [DATABASE_URL]

# 消息缓存配置（减少数据库查询）
MESSAGE_CACHE_SIZE = int(os.getenv('MESSAGE_CACHE_SIZE', 100))

# 初始化OpenAI客户端用于OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)


# 数据模型
class MessageRequest(BaseModel):
    message: str

    @validator('message')
    def message_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('消息内容不能为空')
        return v.strip()


class LoginRequest(BaseModel):
    username: str

    @validator('username')
    def username_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('用户名不能为空')
        return v.strip()


# 数据库路由类（支持读写分离）
class DatabaseRouter:
    def __init__(self, primary_url: str, replica_urls: List[str]):
        self.primary_pool = None
        self.replica_pools = []
        self.primary_url = primary_url
        self.replica_urls = replica_urls

    async def init_pools(self):
        try:
            self.primary_pool = await asyncpg.create_pool(
                self.primary_url,
                min_size=5,
                max_size=20,
                command_timeout=60
            )
            logger.info("主数据库连接池初始化成功")

            for replica_url in self.replica_urls:
                if replica_url.strip():
                    pool = await asyncpg.create_pool(
                        replica_url,
                        min_size=5,
                        max_size=20,
                        command_timeout=60
                    )
                    self.replica_pools.append(pool)
                    logger.info(f"从数据库连接池初始化成功: {replica_url}")

            if not self.replica_pools:
                logger.info("没有配置从库，使用主库进行读操作")

        except Exception as e:
            logger.error(f"数据库连接池初始化失败: {e}")
            raise

    async def write_operation(self, query: str, *args):
        # 写操作路由到主库
        try:
            async with self.primary_pool.acquire() as conn:
                return await conn.execute(query, *args)
        except Exception as e:
            logger.error(f"数据库写操作失败: {e}, 查询: {query}, 参数: {args}")
            raise

    async def read_operation(self, query: str, *args):
        # 读操作路由到从库
        try:
            if not self.replica_pools:
                # 如果没有从库，使用主库
                async with self.primary_pool.acquire() as conn:
                    return await conn.fetch(query, *args)

            replica_pool = random.choice(self.replica_pools)
            async with replica_pool.acquire() as conn:
                return await conn.fetch(query, *args)
        except Exception as e:
            logger.error(f"数据库读操作失败: {e}, 查询: {query}, 参数: {args}")
            raise


# WebSocket连接管理器
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.active_users: dict = {}  # websocket -> username mapping
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        async with self.lock:
            self.active_connections.append(websocket)
            self.active_users[websocket] = username
        logger.info(f"用户 {username} 已连接，当前在线用户数: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
            username = self.active_users.pop(websocket, None)
        if username:
            logger.info(f"用户 {username} 已断开连接，当前在线用户数: {len(self.active_connections)}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except Exception as e:
            logger.warning(f"向用户发送消息失败: {e}")
            await self.disconnect(websocket)

    async def broadcast(self, message: str):
        disconnected = []
        async with self.lock:
            for connection in self.active_connections:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.warning(f"广播消息失败: {e}")
                    disconnected.append(connection)

        # 移除已断开的连接
        for connection in disconnected:
            await self.disconnect(connection)

    def get_username(self, websocket: WebSocket) -> Optional[str]:
        return self.active_users.get(websocket)

    def is_user_online(self, username: str) -> bool:
        return username in self.active_users.values()


# 消息缓存类（减少数据库查询）
class MessageCache:
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self.cache = deque(maxlen=max_size)
        self.lock = asyncio.Lock()

    async def add(self, message: str):
        async with self.lock:
            self.cache.append(message)

    async def get_all(self) -> List[str]:
        async with self.lock:
            return list(self.cache)

    async def refresh_from_db(self, db_router: DatabaseRouter):
        """从数据库刷新缓存"""
        try:
            rows = await db_router.read_operation(
                """SELECT username, message, message_type, created_at
                   FROM chat_messages
                   ORDER BY created_at DESC
                       LIMIT $1""",
                self.max_size
            )

            # 格式化消息
            messages = []
            for row in reversed(rows):
                now = row['created_at'].replace(microsecond=0)
                formatted_message = f'[{now.time().isoformat()}] {row["username"]}: {row["message"]}'
                messages.append(formatted_message)

            async with self.lock:
                self.cache = deque(messages, maxlen=self.max_size)

            logger.info(f"消息缓存已刷新，当前缓存 {len(messages)} 条消息")
        except Exception as e:
            logger.error(f"刷新消息缓存失败: {e}")


# 全局实例
db_router = DatabaseRouter(PRIMARY_DB_URL, REPLICA_DB_URLS)
manager = ConnectionManager()
message_cache = MessageCache(max_size=MESSAGE_CACHE_SIZE)


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
    except JWTError as e:
        logger.warning(f"JWT验证失败: {e}")
        return None


# AI回复函数（添加重试机制）
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((APIConnectionError, APITimeoutError)),
    reraise=True
)
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
            ],
            timeout=30  # 30秒超时
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

    except APITimeoutError:
        logger.warning("OpenRouter API 请求超时")
        return "[系统] AI助手响应超时，请稍后再试"
    except APIConnectionError:
        logger.warning("OpenRouter API 连接错误")
        return "[系统] 无法连接到AI服务，请检查网络连接"
    except APIError as e:
        logger.error(f"OpenRouter API 错误: {e}")
        return f"[系统] AI服务错误: {str(e)}"
    except Exception as e:
        logger.error(f"OpenRouter 未知错误: {e}")
        return f"[系统] 未知错误: {str(e)}"


async def add_message(username: str, message: str, message_type: str = 'user'):
    """添加消息到数据库并通过WebSocket通知所有客户端"""
    try:
        # 写入数据库
        await db_router.write_operation(
            """INSERT INTO chat_messages (username, message, message_type)
               VALUES ($1, $2, $3)""",
            username, message, message_type
        )

        # 更新缓存
        now = datetime.datetime.now().replace(microsecond=0).time()
        formatted_message = f'[{now.isoformat()}] {username}: {message}'
        await message_cache.add(formatted_message)

        # 广播消息
        await manager.broadcast(json.dumps({
            "type": "new_message",
            "message": formatted_message
        }))

    except Exception as e:
        logger.error(f"添加消息失败: {e}")
        # 即使数据库写入失败，也尝试广播消息
        now = datetime.datetime.now().replace(microsecond=0).time()
        formatted_message = f'[{now.isoformat()}] {username}: {message}'
        await message_cache.add(formatted_message)
        await manager.broadcast(json.dumps({
            "type": "new_message",
            "message": formatted_message
        }))


async def async_ai_task(username: str, message: str):
    """异步处理AI回复"""
    question = message.replace("@ai", "", 1).strip()

    # 先发送一个等待提示
    now = datetime.datetime.now().replace(microsecond=0).time()
    thinking_message = f'[{now.isoformat()}] 🤖AI助手: 正在思考中...'
    await add_message("🤖AI助手", thinking_message, "ai")

    # 获取AI回复
    reply = await openrouter_reply(question)

    # 发送AI回复
    await add_message("🤖AI助手", reply, "ai")


# 自定义异常处理
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理的异常: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "服务器内部错误，请稍后再试"},
    )


# 路由
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/api/login")
async def login(login_request: LoginRequest):
    username = login_request.username

    if username == '🤖AI助手':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='用户名不能为"🤖AI助手"'
        )

    # 检查用户是否已在线
    if manager.is_user_online(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该用户已登录，请勿重复登录"
        )

    # 创建JWT token
    access_token = create_access_token(data={"sub": username})

    # 添加加入消息
    await add_message("系统", f'用户 {username} 加入了房间!', 'system')

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "username": username
    }


@app.post("/api/logout")
async def logout(request: Request):
    # 从header获取token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        username = verify_token(token)
        if username:
            await add_message("系统", f'用户 {username} 退出了房间', 'system')

    return {"message": "已退出"}


@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    # 验证token
    username = verify_token(token)
    if not username:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket, username)

    try:
        # 发送历史消息
        history_messages = await message_cache.get_all()
        await websocket.send_text(json.dumps({
            "type": "history_messages",
            "messages": history_messages
        }))

        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            if message_data["type"] == "send_message":
                message = message_data["message"]
                cleaned_message = bleach.clean(message)

                if not cleaned_message:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "消息内容不能为空"
                    }))
                    continue

                if message.startswith("@ai"):
                    await add_message(username, cleaned_message)
                    # 异步处理AI回复
                    asyncio.create_task(async_ai_task(username, message))
                else:
                    await add_message(username, cleaned_message)

    except WebSocketDisconnect:
        logger.info(f"WebSocket连接断开: {username}")
        await manager.disconnect(websocket)
        await add_message("系统", f'用户 {username} 断开了连接', 'system')
    except Exception as e:
        logger.error(f"WebSocket处理错误: {e}")
        await manager.disconnect(websocket)
        await add_message("系统", f'用户 {username} 的连接发生错误', 'system')


@app.get("/api/health")
async def health_check():
    try:
        # 检查数据库连接
        async with db_router.primary_pool.acquire() as conn:
            await conn.execute("SELECT 1")

        # 检查在线用户数
        online_users = len(manager.active_connections)

        return {
            "status": "healthy",
            "timestamp": datetime.datetime.now().isoformat(),
            "online_users": online_users,
            "cached_messages": len(message_cache.cache)
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "detail": str(e)
            }
        )


@app.post("/api/refresh_cache")
async def refresh_cache():
    """手动刷新消息缓存"""
    try:
        await message_cache.refresh_from_db(db_router)
        return {"status": "success", "message": "缓存刷新成功"}
    except Exception as e:
        logger.error(f"刷新缓存失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="刷新缓存失败"
        )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_keep_alive=60,
        log_level="info"
    )
