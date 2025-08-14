# FastAPI 聊天室

一款采用 FastAPI 构建的简易室聊天应用程序。

## 特性

- **实时消息传递**：基于 WebSocket 的即时消息传递，带有连接状态指示器
- **AI 助手**：由 OpenRouter API 驱动的集成 AI 助手（使用 `@ai` 前缀触发）
- **用户认证**：基于安全 JWT 的认证系统
- **持久存储**：使用 pickle 存储和 LRU 缓存的消息历史记录
- **现代用户界面**：采用 Tailwind CSS 的响应式设计和聊天气泡样式
- **消息清理**：内置的安全性，带有 HTML 清理功能
- **连接管理**：自动重连和用户在线状态跟踪

## 技术栈

- **后端**：FastAPI、WebSockets、JWT 认证
- **前端**：HTML5、Tailwind CSS、JavaScript、Socket.IO
- **AI 集成**：使用 OpenAI 客户端和 OpenRouter API
- **存储**：基于 Pickle 的持久化存储，带 LRU 缓存
- **安全**：Bleach 用于消息清理、CORS 中间件

## 配置

### 环境变量

| 变量 | 描述 | 是否必填 |
|----------|-------------|----------|
| `SECRET_KEY` | 用于身份验证的 JWT 秘钥 | 是 |
| `OPENROUTER_API_KEY` | OpenRouter AI 服务的 API 密钥 | 是 |
| `SITE_URL` | 用于 OpenRouter 排名的您的网站 URL | 否 |
| `SITE_NAME` | 用于 OpenRouter 排名的您的网站名称 | 否 |

### 人工智能模型

该应用默认使用 OpenRouter 的免费套餐：
- 模型：`deepseek/deepseek-r1-0528：free`
- 触发方式：输入 `@ai` 后跟您的问题
- 示例：`@ai 今天天气怎么样？``
