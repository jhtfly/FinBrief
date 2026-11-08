"""OpenAI 兼容大模型客户端：httpx 直调，JSON 输出容错与重试。"""
import json
import re
import time
import uuid

import httpx

from app import db


class AIError(Exception):
    pass


def _record_usage(usage: dict):
    """把响应里的 usage 计入每日 token 统计（缺失则跳过）。
    v2.20：试用模式下累计试用用量（按模式判定，不再比对 key 字符串）。"""
    try:
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        if tin or tout:
            db.add_token_usage(tin, tout)
        if _trial_mode() and (tin or tout):
            used_in = int(db.get_setting("trial_used_in", "0") or 0) + tin
            used_out = int(db.get_setting("trial_used_out", "0") or 0) + tout
            with db.get_conn() as conn:
                for k, v in (("trial_used_in", used_in), ("trial_used_out", used_out)):
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, str(v)))
    except Exception:
        pass


def _trial_mode() -> bool:
    """v2.20 试用/自定义模式判定：已激活试用密匙（trial_active_hash 存在）即试用
    模式——与自定义 API 设置完全独立，互不读写。"""
    try:
        return bool(db.get_setting("trial_active_hash", ""))
    except Exception:
        return False


def _route(s: dict, model: str):
    """v2.20 按模式取 (base, key, model)：
    - 试用模式：全部任务（筛选/编译/翻译/摘要…）统一走试用接口与试用模型，
      不区分筛选/编译/翻译模型，也不读写 api_base_url/api_key 设置；
    - 自定义模式：用设置里的地址与密钥（密钥经 DPAPI 加密存储）与各任务模型。"""
    if _trial_mode():
        from app.trial_config import TRIAL_BASE_URL, TRIAL_DEFAULT_MODEL, upstream_key
        m = (db.get_setting("trial_model", "") or TRIAL_DEFAULT_MODEL)
        return TRIAL_BASE_URL, upstream_key(), m
    return ((s.get("api_base_url") or "").rstrip("/"),
            db.get_secret("api_key"),
            model)


def _trial_gate():
    """v2.19 试用额度守门：试用模式且本机累计已达 100 万 tokens 时拦截调用。"""
    if not _trial_mode():
        return
    try:
        from app.trial_config import TRIAL_LIMIT_TOKENS
        used = (int(db.get_setting("trial_used_in", "0") or 0)
                + int(db.get_setting("trial_used_out", "0") or 0))
        if used >= TRIAL_LIMIT_TOKENS:
            raise AIError("试用额度（100 万 tokens）已用尽：请向管理员获取新密匙并在"
                          "「设置 → 试用密匙」中粘贴激活，或停用试用改用自己的 API Key")
    except AIError:
        raise
    except Exception:
        pass


def _extract_json(text: str):
    """从模型回复中提取 JSON 对象（容错：代码栅栏、前后缀文本、截断补闭合）。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 从文本中截取第一个平衡的大括号块
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
    # v2.13.1 截断容错：deepseek 系输出经常超限被截断——按最后一次合法闭合位置截断，
    # 丢弃尾部未完成段，再把未闭合的括号按开括号栈反向补齐后整体解析。
    stack, in_str, esc, safe_end = [], False, False, -1
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            safe_end = i   # 最后一次合法闭合的位置（哪一层都算）
    if stack and safe_end > 0:
        cand = text[:safe_end + 1]
        # 在截断后的文本上重算未闭合栈（截断点之后的栈信息失效）
        st2, strs, esc2 = [], False, False
        for ch in cand:
            if strs:
                if esc2:
                    esc2 = False
                elif ch == "\\":
                    esc2 = True
                elif ch == '"':
                    strs = False
            elif ch == '"':
                strs = True
            elif ch in "{[":
                st2.append(ch)
            elif ch in "}]":
                if st2:
                    st2.pop()
        closers = ""
        for op in reversed(st2):
            closers += ("}" if op == "{" else "]")
        cand += closers
        try:
            fixed = json.loads(cand)
            db.log("ai", f"JSON 截断修复成功（补 {len(closers)} 层闭合，"
                         f"丢弃尾部未完成段 {max(0, len(text) - safe_end - 1)} 字符）")
            return fixed
        except json.JSONDecodeError:
            pass
    raise AIError("模型输出无法解析为 JSON")


def _extra_headers(s: dict, base: str):
    """请求头合成（v2.15.0）：api_extra_headers 设置（JSON）+ opencode.ai 会话头。
    - 用户自定义头优先（可覆盖内置）；
    - opencode.ai 的 /go 端点要求每对话稳定 x-opencode-session：自动生成一次并存
      settings.opencode_session_id，之后复用（不影响其它服务商）。"""
    headers = {}
    try:
        extra = json.loads((s.get("api_extra_headers") or "").strip() or "{}")
        if isinstance(extra, dict):
            for k, v in extra.items():
                if str(k).strip():
                    headers[str(k)] = str(v)
    except ValueError:
        try:
            db.log("ai", "api_extra_headers 不是合法 JSON，已忽略该设置")
        except Exception:
            pass
    if "opencode.ai" in base:
        sid = (s.get("opencode_session_id") or "").strip()
        if not sid:
            sid = uuid.uuid4().hex
            try:
                db.set_setting("opencode_session_id", sid)
            except Exception:
                pass
        headers.setdefault("x-opencode-session", sid)
    return headers


def chat(messages: list, model: str, temperature: float = 0.3,
         max_tokens: int = 8000, json_mode: bool = True,
         retries: int | None = None, timeout: float = 300.0,
         system: str = "") -> str:
    """调用 chat/completions，带稳健重试（v2.8 增强）：
    - 重试次数取设置 ai_retry_max（默认 3）；
    - 超时/断联/5xx/429/空内容均按指数退避重试（3s→6s→12s…，429 较长）；
    - 每次重试写入运行日志，便于任务中心/运行日志观察；
    - 4xx（密钥错、参数错，除 429）不重试；
    - v2.20.1：新增 system 形参（此前 RSSHub AI 命名传 system= 会 TypeError
      被 except 静默吞掉，AI 中文名从未真正生效——体检发现）。"""
    s = db.all_settings()
    if isinstance(messages, str):   # v2.20.1 兜底：裸字符串按单条 user 消息处理
        messages = [{"role": "user", "content": messages}]
    base, key, model = _route(s, model)
    if not base or not key:
        raise AIError("未配置 API 地址或密钥：请激活试用密匙，或在「设置」中填写自定义 API")
    if not model:   # v2.20.2：默认模型名不再预填，漏填时给出可操作的报错
        raise AIError("未配置模型名：请在「设置 → 大模型 API」填写筛选 / 编译 / 翻译模型")
    if retries is None:
        try:
            retries = max(0, min(6, int(s.get("ai_retry_max", "3"))))
        except (TypeError, ValueError):
            retries = 3
    _trial_gate()

    body = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] + messages) if system else messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    url = f"{base}/chat/completions"
    last_err = None
    for attempt in range(retries + 1):
        if attempt:
            wait = min(60, 3 * (2 ** (attempt - 1)))   # 3/6/12/24/48…
            db.log("ai", f"AI 调用重试第 {attempt}/{retries} 次（{wait}s 后）：{last_err}")
            time.sleep(wait)
        try:
            with httpx.Client(timeout=timeout, verify=False) as client:
                resp = client.post(url, json=body,
                                   headers={"Authorization": f"Bearer {key}",
                                            **_extra_headers(s, base)})
            if resp.status_code == 429:  # 限流：等待后重试
                wait = 5 * (attempt + 1)
                last_err = AIError(f"API 限流(429)，等待{wait}秒重试")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            call_data = resp.json()
            data = call_data
            msg_obj = (data["choices"][0].get("message") or {})
            content = msg_obj.get("content")
            if not content:
                # v2.13.5：deepseek 系 reasoning 模型思考会占满 max_tokens，正文为空时
                # 加大 max_tokens 重试（递进上限 32000），而不是空转三次
                rc = msg_obj.get("reasoning_content") or ""
                fr = data["choices"][0].get("finish_reason") or ""
                err = AIError("API 返回空内容" if not rc
                              else f"正文为空（思考占满 max_tokens，finish={fr}）")
                if rc:
                    err.bump_tokens = True
                raise err
            _record_usage(data.get("usage") or {})   # token 统计
            return content
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("error", {}).get("message", "")[:200]
            except Exception:
                pass
            last_err = AIError(f"API HTTP {e.response.status_code}: {detail}")
            if e.response.status_code < 500 and e.response.status_code != 429:
                break  # 4xx（密钥错/参数错）重试无意义
        except AIError as e:
            # v2.20.1：空内容/思考占满属瞬时问题，纳入重试（此前直接抛出不重试，
            # 用户报"翻译失败：未返回内容"即此）；思考占满同步加大 max_tokens 再试
            last_err = e
            if getattr(e, "bump_tokens", False):
                body["max_tokens"] = min(32000, max(int(body.get("max_tokens") or 2000) * 2, 4000))
                db.log("ai", f"思考占满 max_tokens，已加大到 {body['max_tokens']} 后重试")
        except (httpx.HTTPError, KeyError, IndexError) as e:
            # 含 ConnectTimeout/ReadTimeout/ConnectError 等断联、超时 → 重试
            last_err = AIError(f"API 调用失败: {type(e).__name__}: {e}")
    raise last_err or AIError("API 调用失败")


def chat_json(messages: list, model: str, **kw):
    """调用并解析为 JSON 对象。
    v2.13.1：解析失败（含截断）自动再调一次并把 max_tokens 加倍（最多 32000）。"""
    try:
        text = chat(messages, model, json_mode=True, **kw)
    except Exception:
        raise
    try:
        return _extract_json(text)
    except Exception as e:
        if "无法解析为 JSON" not in str(e):
            raise
        mt = int(kw.get("max_tokens") or 8000)
        kw2 = dict(kw)
        kw2["max_tokens"] = min(32000, mt * 2)
        db.log("ai", f"JSON 解析失败，自动加倍 max_tokens（{mt}→{kw2['max_tokens']}）重试一次")
        text2 = chat(messages, model, json_mode=True, **kw2)
        return _extract_json(text2)


def chat_stream(messages: list, model: str, temperature: float = 0.3,
                max_tokens: int = 8000, json_mode: bool = True,
                retries: int | None = None, timeout: float = 300.0,
                on_delta=None) -> str:
    """流式 chat/completions（v2.9）：逐块回调思考与生成内容，干扰界面实时显示。

    - on_delta(kind, text)：kind = "reasoning"（思考流，若模型输出）或
      "content"（正文增量）；供前端显示思考过程/生成字数；
    - 重试语义与 chat 相同（指数退避，回调 on_delta("retry", 提示)）；
    - 返回完整正文文本（去掉思考后可供 chat_json 解析）。"""
    s = db.all_settings()
    base, key, model = _route(s, model)
    if not base or not key:
        raise AIError("未配置 API 地址或密钥：请激活试用密匙，或在「设置」中填写自定义 API")
    if not model:   # v2.20.2：默认模型名不再预填，漏填时给出可操作的报错
        raise AIError("未配置模型名：请在「设置 → 大模型 API」填写筛选 / 编译 / 翻译模型")
    if retries is None:
        try:
            retries = max(0, min(6, int(s.get("ai_retry_max", "3"))))
        except (TypeError, ValueError):
            retries = 3
    _trial_gate()

    body = {
        "model": model, "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": True,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    url = f"{base}/chat/completions"
    last_err = None
    for attempt in range(retries + 1):
        if attempt:
            wait = min(60, 3 * (2 ** (attempt - 1)))
            db.log("ai", f"AI 流式调用重试第 {attempt}/{retries} 次（{wait}s 后）：{last_err}")
            if on_delta:
                on_delta("retry", f"连接中断，{wait}s 后自动重试（第 {attempt}/{retries} 次）…")
            time.sleep(wait)
        chunks = []
        try:
            with httpx.Client(timeout=timeout, verify=False) as client:
                with client.stream("POST", url, json=body,
                                   headers={"Authorization": f"Bearer {key}",
                                            "Accept": "text/event-stream",
                                            **_extra_headers(s, base)}) as resp:
                    if resp.status_code == 429:
                        wait = 5 * (attempt + 1)
                        last_err = AIError(f"API 限流(429)，等待{wait}秒重试")
                        if on_delta:
                            on_delta("retry", f"限流(429)，{wait}s 后自动重试…")
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    got_content = False
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            if payload == "[DONE]":
                                break
                            continue
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        usage = evt.get("usage")
                        if usage:
                            _record_usage(usage)
                        choices = evt.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        think = delta.get("reasoning_content") or ""
                        piece = delta.get("content") or ""
                        if think and on_delta:
                            on_delta("reasoning", think)
                        if piece:
                            got_content = True
                            chunks.append(piece)
                            if on_delta:
                                on_delta("content", piece)
                    content = "".join(chunks)
                    if not content:
                        raise AIError("API 流式返回空内容")
                    return content
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            last_err = AIError(f"API HTTP {e.response.status_code}: {detail}")
            if e.response.status_code < 500 and e.response.status_code != 429:
                break
        except AIError as e:
            last_err = e   # v2.20.1：流式空返回也纳入重试（瞬时问题）
        except (httpx.HTTPError, KeyError, IndexError) as e:
            last_err = AIError(f"API 调用失败: {type(e).__name__}: {e}")
    raise last_err or AIError("API 调用失败")
