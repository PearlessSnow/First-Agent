# -*- coding: utf-8 -*-
"""
电商运营 AI 智能体 · 后端服务
- FastAPI 单文件，httpx 调用 DeepSeek，流式转发（SSE -> 纯文本流）
- 服务端历史对话模块：conversations.json 持久化，支持列表 / 详情 / 删除 / 续聊
- StaticFiles 挂载前端页面，前后端同端口
- API Key 只从环境变量 DEEPSEEK_API_KEY 读取，绝不写死在前端
"""
import json
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()  # 读取同目录 .env（存在才生效）

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
HISTORY_FILE = os.getenv("CONVERSATIONS_FILE", os.path.join(BASE_DIR, "conversations.json"))  # 历史对话持久化文件（路径可用环境变量覆盖，便于 Docker 挂载卷）

app = FastAPI(title="电商运营 AI 智能体")

# ---- 跨域：允许跨域（生产环境建议把 origins 收敛为你的前端域名） ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Conversation-Id"],  # 跨域场景下前端也能读到会话 ID
)

SYSTEM_PROMPT_TEMPLATE = """你是一名有 5 年经验的资深电商运营专家，服务对象是中小电商店主和运营人员。
回答要求：
1. 先给结论，再给依据，最后给建议动作，控制在 300 字以内（用户明确要求展开时可放宽）。
2. 必须基于下方「店铺看板数据（STORE_DATA）」作答，涉及数字必须与数据一致；数据中没有的信息，明确说"当前无数据"，严禁凭空捏造。
3. 能够跨板块综合分析，例如结合成本板块的推广费变化与销售板块的销售额/转化率，解释利润下滑原因。
4. 说话直接、务实，不堆砌术语。
5. 不提供刷单、虚假宣传、规避平台规则等违规建议；涉及财务审计、法律合规问题时建议咨询专业人士。

店铺看板数据（STORE_DATA，JSON）：
{store_data}
"""


# ==================== 历史对话存储 ====================

_file_lock = threading.Lock()


def _load_conversations() -> Dict:
    """启动时从 conversations.json 恢复历史对话。"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_conversations() -> None:
    """原子写回（先写临时文件再替换），避免写一半损坏。"""
    with _file_lock:
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CONVERSATIONS, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_FILE)


CONVERSATIONS: Dict[str, dict] = _load_conversations()


def _conv_summary(c: dict) -> dict:
    return {
        "id": c["id"],
        "title": c.get("title") or "新对话",
        "updated_at": c.get("updated_at", 0),
        "message_count": len(c.get("messages", [])),
    }


@app.get("/api/conversations")
async def list_conversations():
    """历史对话列表（按最近更新倒序）。"""
    items = [_conv_summary(c) for c in CONVERSATIONS.values()]
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"conversations": items}


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """对话详情：完整消息记录。"""
    c = CONVERSATIONS.get(conversation_id)
    if not c:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})
    return {
        "id": c["id"],
        "title": c.get("title") or "新对话",
        "messages": c.get("messages", []),
        "updated_at": c.get("updated_at", 0),
    }


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """删除指定对话。"""
    if conversation_id not in CONVERSATIONS:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})
    del CONVERSATIONS[conversation_id]
    _save_conversations()
    return {"ok": True}


# ==================== 对话接口 ====================

class ChatMessage(BaseModel):
    role: str  # user / assistant
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]                 # 前端维护的对话历史（含本轮 user 消息）
    store_data: Optional[Dict] = None           # 前端全局看板数据
    conversation_id: Optional[str] = None       # 续聊时传；空则服务端新建对话


@app.get("/api/health")
async def health():
    """部署自检：确认服务与 Key 状态（不暴露 Key 本身）。"""
    return {
        "status": "ok",
        "api_key_configured": bool(DEEPSEEK_API_KEY),
        "model": DEEPSEEK_MODEL,
        "conversations": len(CONVERSATIONS),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    # 1. Key 校验
    if not DEEPSEEK_API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "服务端未配置 DEEPSEEK_API_KEY，请在 .env 或环境变量中填写后重启服务。"},
        )

    # 2. 定位/创建服务端会话，先落库本轮用户消息（即使后续模型失败，提问也不丢）
    conv_id = req.conversation_id or uuid.uuid4().hex[:12]
    now = time.time()
    conv = CONVERSATIONS.get(conv_id)
    if conv is None:
        conv = {
            "id": conv_id,
            "title": "",
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "store_data": req.store_data or {},
        }
        CONVERSATIONS[conv_id] = conv

    last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
    if last_user:
        conv["messages"].append({"role": "user", "content": last_user.content})
        if not conv.get("title"):
            conv["title"] = last_user.content.strip()[:20] or "新对话"
    conv["updated_at"] = now
    if req.store_data:
        conv["store_data"] = req.store_data  # 每轮刷新看板快照
    _save_conversations()

    # 3. 组装系统提示词：注入 STORE_DATA，约束模型基于真实看板数据作答
    store_json = json.dumps(conv.get("store_data") or {}, ensure_ascii=False, indent=2)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(store_data=store_json)

    # 4. 历史截断（最多保留最近 20 轮，防止 token 超限）
    history = [m.model_dump() for m in req.messages[-20:]]
    full_messages = [{"role": "system", "content": system_prompt}] + history

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": full_messages,
        "stream": True,
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async def stream_generator():
        answer = ""
        ok = True
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{DEEPSEEK_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        ok = False
                        detail = (await resp.aread()).decode("utf-8", "ignore")
                        yield f"\n\n[ERROR] 模型服务返回 {resp.status_code}：{detail[:300]}"
                    else:
                        # 5. 解析上游 SSE，逐段转发 delta 文本（前端配合做打字机效果）
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            content = (choices[0].get("delta") or {}).get("content")
                            if content:
                                answer += content
                                yield content
        except httpx.HTTPError as e:
            ok = False
            yield f"\n\n[ERROR] 调用模型服务失败：{e}"
        except Exception as e:
            ok = False
            yield f"\n\n[ERROR] 服务内部错误：{e}"
        finally:
            # 6. 回复完整才落库；失败轮次不写入 assistant，避免脏数据
            if ok and answer:
                conv["messages"].append({"role": "assistant", "content": answer})
                conv["updated_at"] = time.time()
            _save_conversations()

    return StreamingResponse(
        stream_generator(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Conversation-Id": conv_id,  # 前端据此记住当前会话，用于续聊
        },
    )


# ---- 前端页面挂载：放在所有 API 路由之后，"/" 直接打开 index.html ----
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
