# 电商运营 AI 智能体 · 本地运行与部署指南

## 项目结构

```
ecom-agent/
├── app.py            # FastAPI 后端（单文件：/api/chat + 静态页面挂载）
├── static/
│   └── index.html    # 前端（单文件：6 大看板 + AI 对话中心）
├── requirements.txt  # Python 依赖
├── .env.example      # 环境变量示例（复制为 .env 填 Key）
├── Dockerfile        # 容器化部署
└── README.md         # 本指南
```

---

## 一、本地运行

### 1. 环境准备

- Python 3.10+（建议 3.11）
- 一个 DeepSeek API Key（在 https://platform.deepseek.com 申请）

### 2. 安装依赖

```bash
cd ecom-agent

# Windows（PowerShell）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置 API Key（二选一）

**方式 A：.env 文件（推荐本地开发）**

```bash
cp .env .env        # Windows: copy .env .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=sk-你的Key
```

**方式 B：环境变量（推荐服务器/Docker）**

```bash
# Linux / macOS
export DEEPSEEK_API_KEY=sk-你的Key

# Windows PowerShell
$env:DEEPSEEK_API_KEY="sk-你的Key"
```

### 4. 启动服务

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

浏览器打开 **http://127.0.0.1:8000** 即可：

- 前 6 个菜单：订单 / 销售 / 物流 / 成本 / 客服 / 类目看板（KPI 卡片 + SVG 图表，纯前端静态数据）
- 第 7 个菜单「AI 运营助手」：进入全屏聊天，流式打字机输出

### 5. 自检

```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok","api_key_configured":true,...}  → Key 已加载
```

---

## 二、Docker 部署

### 1. 构建镜像

```bash
cd ecom-agent
docker build -t ecom-agent .
```

### 2. 运行容器（Key 以环境变量注入，不进镜像）

```bash
docker run -d --name ecom-agent \
  -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-你的Key \
  --restart unless-stopped \
  ecom-agent
```

访问 `http://服务器IP:8000` 即可。

### 3. docker-compose（可选）

```yaml
# docker-compose.yml
services:
  ecom-agent:
    build: ecom-agent
    ports:
      - "8000:8000"
    environment:
      - DEEPSEEK_API_KEY=sk-你的Key
    restart: unless-stopped
```

```bash
docker compose up -d
```

---

## 三、云服务器公网部署（Linux 为例）

以一台有公网 IP 的云服务器（阿里云/腾讯云等，系统 Ubuntu 22.04）为例：

### 1. 安装 Docker 并拉起容器（推荐）

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | bash
# 构建 + 运行（同上二节）
```

### 2. 放行端口

在云厂商控制台的**安全组**中放行 8000 端口（TCP）。生产环境建议只放行 80/443，由 Nginx 反代。

### 3. Nginx 反向代理 + 域名（可选但推荐）

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        # 流式输出关键：关闭缓冲
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

配好后用 `certbot --nginx` 一键上 HTTPS。

> ⚠️ 流式输出对代理缓冲敏感：`proxy_buffering off` 必须设置，否则打字机效果会变成"一坨一坨"出现。

### 4. 云平台一键托管（备选）

本项目是标准单端口 HTTP 服务，也可直接使用 WorkBuddy 等支持 Node/Python/Go 应用的托管平台一键发布，免运维。

---

## 四、安全与运维要点

| 事项 | 说明 |
|---|---|
| API Key | 只存在于环境变量 / .env，前端代码零 Key；.env 必须加入 .gitignore |
| CORS | 当前 `allow_origins=["*"]` 便于联调；公网部署建议改为你的域名列表 |
| 流式超时 | 后端已设 120s 读超时；Nginx 侧 `proxy_read_timeout` 需 ≥ 该值 |
| 用量控制 | DeepSeek 按量计费；公网暴露建议自行加访问鉴权（如 API Token / 登录墙），防止 Key 被刷 |
| 数据 | 当前 STORE_DATA 为静态假数据；接真实数据时替换前端全局对象并同步后端校验 |

## 五、常见问题

- **AI 不回话，提示"未配置 DEEPSEEK_API_KEY"**：Key 没注入或改完没重启服务。
- **回答一次性出现、没有打字机效果**：中间有代理开了缓冲（Nginx 记得 `proxy_buffering off`）。
- **Docker 容器内读不到 .env**：容器里用 `-e` 注入环境变量，或 `--env-file .env` 挂载。
