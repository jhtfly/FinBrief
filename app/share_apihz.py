# -*- coding: utf-8 -*-
"""v2.21.5 专报共享（apihz 接口盒子）——"网盘 JSON 索引"方案（免后台建表）：

- 轻量网盘存两样东西：①各专报 Word 文件（每份一个 name）②共享索引
  share_index_v2.json（固定名，含全部共享记录）。检索/下载/删除都基于索引。
- 为什么不用轻云数据表：后台建表无法 API 化（用户需手动建表）、字段/检索受 SQL
  方言约束；而网盘直链 URL 模式可预测（实测 /file/{开发者ID}/{name}{后缀}），
  索引文件自持读写。轻云数据表通道 v2.21.4 曾实现，v2.21.5 起停用。
- 默认凭据：开发者 ID/KEY 以 XOR 混淆内置（用户"一键采用默认"），可在设置页
  覆盖为自己的 apihz 账号（DB 加密态 dpapi1:，优先级高于默认）。
- 共享圈：圈内共用同一开发者 ID/KEY，以 share_uid 辨认上传者。"""
import base64
import json as _j
import secrets as _secrets

import httpx

PAN_URL = "https://cn.apihz.cn/api/ziyuan/pan.php"
PAN_DEL_URL = "https://cn.apihz.cn/api/ziyuan/pandel.php"
PAN_MAX = 2 * 1024 * 1024          # 网盘单文件 base64 后上限 2M
INDEX_NAME = "share_index_v2"      # 索引网盘名（不带后缀）
INDEX_FULL = INDEX_NAME + ".json"
CONTENT_MAX = 8000                 # 文字稿单条正文上限（索引体量控制）
TEXT_KEEP = 25                     # 文字稿记录保留条数（超出淘汰最旧）
WORD_KEEP = 200                    # Word 共享记录保留条数
PAN_HOST = "https://pan.erguanmingmin.com"

# 默认开发者凭据（XOR 0x5A 混淆字面量，避免仓库明文；设计同试用密匙体系——
# 开源后可被还原属已知限制，用户已在设置页确认采用默认信息方案）
_DEF_KEY = bytes([0x5A])
_DEF_ID_ENC = "kjjkmllo"
_DEF_KEY_ENC = "l;8>?no>?;?nc?8>moknk<lh89mic<hk"

def _xor_decode(enc: str) -> str:
    return "".join(chr(ord(ch) ^ _DEF_KEY[0]) for ch in enc)


def _parse_friends(raw: str) -> list:
    """好友列表 JSON [{name, uid}] 解析（容错）。"""
    try:
        arr = _j.loads(raw or "[]")
        if isinstance(arr, list):
            return [{"name": str(x.get("name", "")).strip()[:20],
                     "uid": str(x.get("uid", "")).strip()[:40]}
                    for x in arr if str(x.get("uid", "")).strip()]
    except (ValueError, TypeError):
        pass
    return []


def _default_uid() -> str:
    """首次自动生成稳定个人标识（如 fb-a3f2c81d），存 DB 持久化。"""
    from app import db
    uid = db.get_setting("share_uid") or ""
    if uid:
        return uid
    uid = "fb-" + _secrets.token_hex(4)
    db.set_setting("share_uid", uid)
    return uid


def _mask(v: str) -> str:
    return (v[:2] + "…" + v[-2:]) if len(v) > 6 else v


def db_has_custom_creds() -> bool:
    from app import db
    return bool(db.get_secret("apihz_id") and db.get_secret("apihz_key"))


def default_creds_masked() -> dict:
    d = get_creds()
    return {
        "using_default": not db_has_custom_creds(),
        "id_masked": _mask(d["id"]),
        "key_masked": _mask(d["key"]),
        "uid": d["uid"],
    }


def creds_ready() -> bool:
    c = get_creds()
    return bool(c["id"] and c["key"])


def get_creds() -> dict:
    """凭据回退链：DB 配置（DPAPI 加密）→ 内置默认（混淆解码）。"""
    from app import db
    cfg_id = db.get_secret("apihz_id")
    cfg_key = db.get_secret("apihz_key")
    uid = (db.get_setting("share_uid") or "").strip()
    return {
        "id": cfg_id or _xor_decode(_DEF_ID_ENC),
        "key": cfg_key or _xor_decode(_DEF_KEY_ENC),
        "uid": uid or _default_uid(),
        "friends": _parse_friends(db.get_setting("share_friends") or ""),
    }


def _record_key(r: dict):
    """排序键：rid 自身（前缀=时间到毫秒，字典序即时间序）。"""
    return str(r.get("rid") or "")


def _post(url: str, data: dict, timeout: int = 30):
    """统一 POST；返回 (json|None, err)。便于测试拦截。
    v2.21.9 补回：v2.21.7 裁剪时 _post 连同数据表函数一起被误删，
    网盘上传/删除（pan.php/pandel.php 走 POST）全部 NameError——上次只按
    "检索链路"验证未覆盖上传链路，本轮起用 pyflakes 全量未定义名检查。"""
    try:
        with httpx.Client(timeout=timeout, verify=False) as cli:
            resp = cli.post(url, data=data)
            resp.raise_for_status()
            return resp.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def _index_url(creds: dict) -> str:
    """共享索引直链：网盘 URL 模式实测为 /file/{开发者ID}/{name}{type}。"""
    return f"{PAN_HOST}/file/{creds['id']}/{INDEX_FULL}"


def _gen_rid() -> str:
    """记录 ID：日期+时分秒+毫秒+6 位强随机 hex（同圈内唯一且字典序=时间序）。"""
    import secrets
    import time as _t
    from app import db
    return (db.now_iso().replace("-", "").replace("T", "").replace(":", "")[:14]
            + f"{_t.time_ns() % 1000:03d}" + secrets.token_hex(3))


def index_fetch(creds: dict) -> tuple:
    """拉取共享索引。返回 (records list, err)。404 视为空索引。"""
    try:
        with httpx.Client(timeout=20, verify=False, follow_redirects=True) as cli:
            resp = cli.get(_index_url(creds))
        if resp.status_code == 404:
            return [], None
        resp.raise_for_status()
        obj = resp.json()
        if isinstance(obj, dict) and isinstance(obj.get("records"), list):
            recs = [r for r in obj["records"] if isinstance(r, dict)]
            if recs:
                _cache_save(recs, replace=False)   # v2.21.9b 只增改不减：陈旧 CDN 副本不得冲掉缓存
            return recs, None
        return [], None
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}"
    except (ValueError, TypeError):
        return [], None


def _cache_load() -> list:
    """本地索引快照（每次成功写入/拉取时更新；CDN 延迟与误删时兜底）。"""
    from app import db
    try:
        arr = _j.loads(db.get_setting("share_index_cache") or "[]")
        return [r for r in arr if isinstance(r, dict)]
    except (ValueError, TypeError):
        return []


def _tombstones() -> set:
    """已删除共享记录的 rid 墓碑（防止 CDN 陈旧副本把删除行复活）。"""
    from app import db
    try:
        arr = _j.loads(db.get_setting("share_tombstones") or "[]")
        return {str(x) for x in arr if str(x)}
    except (ValueError, TypeError):
        return set()


def _add_tombstone(rid: str) -> None:
    from app import db
    ts = _tombstones()
    ts.add(str(rid))
    db.set_setting("share_tombstones", _j.dumps(sorted(ts)[-200:], ensure_ascii=False))


def _cache_save(records: list, replace: bool = True) -> None:
    """本地快照写策略（v2.21.9b 事故根修）：
    - replace=True（index_save 权威写入后）整表替换——写者掌握全量真值；
    - replace=False（远端拉取成功后）按 rid 只增改不减——CDN 刷新窗口内拉到
      陈旧残缺副本（如事故中只剩 1 条）不得冲掉本地更全的快照，否则"下次
      写入拿残缺列表重写索引"会再次覆盖掉圈内其他人的记录。
    删除的记录由墓碑过滤，不会被陈旧副本/旧缓存复活。"""
    from app import db
    ts = _tombstones()
    if replace:
        merged = [r for r in records if isinstance(r, dict) and str(r.get("rid")) not in ts]
    else:
        try:
            cur = {str(r.get("rid")): r for r in _cache_load()
                   if str(r.get("rid")) not in ts}
        except (ValueError, TypeError):
            cur = {}
        for r in records:
            if isinstance(r, dict) and str(r.get("rid")) not in ts:
                cur[str(r.get("rid"))] = r
        merged = list(cur.values())
    merged = sorted(merged, key=_record_key)[-220:]
    db.set_setting("share_index_cache", _j.dumps(merged, ensure_ascii=False))


def index_fetch_merged(creds: dict) -> tuple:
    """远端索引 ∪ 本地快照（按 rid 并集、远端优先）——抵御 CDN 刷新延迟/误删。
    v2.21.9b：结果过滤删除墓碑（陈旧远端副本不再展示已删记录）。"""
    recs, err = index_fetch(creds)
    if err:
        return None, err
    ts = _tombstones()
    cache = [r for r in _cache_load() if str(r.get("rid")) not in ts]
    by_rid = {}
    for r in cache:
        by_rid[str(r.get("rid"))] = r
    for r in recs:
        if str(r.get("rid")) in ts:
            continue   # 陈旧远端副本里的已删记录不复活
        by_rid[str(r.get("rid"))] = r   # 远端覆盖同 rid
    merged = list(by_rid.values())
    merged = _trim(merged)
    return merged, None


def index_save(creds: dict, records: list) -> tuple:
    """覆盖写共享索引：先删旧（允许"未查询到"=首次），再上传。返回 err|None。"""
    blob = _j.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    if len(base64.b64encode(blob)) > PAN_MAX:
        # 索引超限：裁掉最旧文字稿直到可存（Word 记录不含 content，体积小）
        recs = sorted(records, key=_record_key)
        texts = [r for r in recs if str(r.get("kind", "")).startswith("text")]
        drop = {id(r) for r in texts[:max(1, len(texts) // 4)]}
        records = [r for r in records if id(r) not in drop]
        blob = _j.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    derr = pan_delete(creds, INDEX_FULL)
    if derr and "未查询到" not in derr:
        return f"删除旧索引失败：{derr}"
    err = pan_upload_raw(creds, INDEX_FULL, blob)
    if not err:
        _cache_save(records)
    return err


def pan_upload_raw(creds: dict, file_name: str, data: bytes) -> tuple:
    """网盘上传：BASE64 放 res（非 multipart）。name 不带后缀（网盘自动拼 type）。"""
    if len(base64.b64encode(data)) > PAN_MAX:
        return f"文件 base64 后超过 2M 上限（当前 {len(data)/1048576:.2f}M）"
    suffix = "." + (file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin")
    if suffix in (".php", ".html", ".htm"):
        return "网盘禁止该类型文件"
    base = file_name[:200]
    if "." in base:
        head, ext = base.rsplit(".", 1)
        if 1 <= len(ext) <= 5 and "/" not in ext and "\\" not in ext:
            base = head
    body = {"id": creds["id"], "key": creds["key"], "type": suffix,
            "name": base, "res": base64.b64encode(data).decode("ascii")}
    out, err = _post(PAN_URL, body, timeout=60)
    if err:
        return err
    if not isinstance(out, dict):
        return "接口返回异常"
    if out.get("code") != 200:
        return str(out.get("msg") or "上传失败")
    if not (out.get("resurl") or ""):
        return "上传成功但未返回直链"
    return None


def pan_upload(creds: dict, file_name: str, data: bytes) -> tuple:
    """上传 Word。返回 (resurl|None, err)。"""
    err = pan_upload_raw(creds, file_name, data)
    if err:
        return None, err
    return f"{PAN_HOST}/file/{creds['id']}/{_pan_url_name(file_name)}", None


def _pan_url_name(file_name: str) -> str:
    """网盘存储名 = name + type 后缀（实测 URL 模式）。"""
    base = file_name[:200]
    dot = "." + (file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "docx")
    return base if base.lower().endswith(dot) else base + dot


def pan_delete(creds: dict, file_name: str) -> tuple:
    """pandel 的 name 需完整文件名（含后缀，文档示例 name=接口盒子.jpg）。"""
    body = {"id": creds["id"], "key": creds["key"],
            "name": _pan_url_name(file_name)}
    out, err = _post(PAN_DEL_URL, body)
    if err:
        return err
    if not isinstance(out, dict):
        return "接口返回异常"
    if out.get("code") != 200:
        return str(out.get("msg") or "删除失败")
    return None


# ---------------- 共享记录高层操作 ----------------
def _trim(records: list) -> list:
    """索引体量控制：文字稿/Word 记录各保留最近 N 条（淘汰最旧）。"""
    texts = [r for r in records if str(r.get("kind", "")).startswith("text")]
    words = [r for r in records if not str(r.get("kind", "")).startswith("text")]
    texts = sorted(texts, key=_record_key)[-TEXT_KEEP:]
    words = sorted(words, key=_record_key)[-WORD_KEEP:]
    return sorted(words + texts, key=_record_key, reverse=True)


def share_word_file(creds: dict, rid: str, title: str, issue: str,
                    file_name: str, blob: bytes, note: str = "",
                    share_name: str = "") -> dict:
    """上传一份 Word 并登记进共享索引（返回 {ok, resurl|error}）。"""
    resurl, err = pan_upload(creds, file_name, blob)
    if err:
        return {"ok": False, "error": f"网盘上传失败：{err}", "file_name": file_name}
    from app import db
    # v2.21.9b 关键修复：写索引前必须用 merged（远端∪本地快照）——
    # 批量上传/删除后 CDN 有 1-2 分钟刷新窗口，期间远端 404，index_fetch 会把它
    # 当成"空索引"，下一份上传就拿空列表重写索引，把圈内其他人的记录全部冲掉
    # （2026-11-19 实证事故：索引只剩最后一条上传）。404+快照为空才是真"空圈"。
    recs, ferr = index_fetch_merged(creds)
    if ferr:
        return {"ok": False, "error": f"共享索引读取失败：{ferr}", "resurl": resurl}
    recs = _trim(recs or [])[:WORD_KEEP - 1]
    recs.insert(0, {"rid": _gen_rid(),
                    "share_uid": creds["uid"], "share_name": share_name[:60],
                    "kind": "file", "title": (title or "")[:190],
                    "issue_no": (issue or "")[:30], "file_name": file_name[:200],
                    "resurl": resurl, "content": "", "note": (note or "")[:400],
                    "created_at": db.now_iso()})
    serr = index_save(creds, recs)
    if serr:
        return {"ok": False, "error": f"共享索引写入失败：{serr}", "resurl": resurl}
    return {"ok": True, "resurl": resurl, "file_name": file_name}


def share_text_record(creds: dict, title: str, issue: str, content: str,
                      note: str = "", share_name: str = "", kind_tag: str = "single") -> dict:
    """快捷分享：文字稿直接入索引（content 截 CONTENT_MAX）。"""
    from app import db
    recs, ferr = index_fetch_merged(creds)
    if ferr:
        return {"ok": False, "error": f"共享索引读取失败：{ferr}"}
    recs = _trim(recs or [])[:TEXT_KEEP - 1]
    recs.insert(0, {"rid": _gen_rid(),
                    "share_uid": creds["uid"], "share_name": share_name[:60],
                    "kind": "text:" + kind_tag, "title": (title or "")[:190],
                    "issue_no": (issue or "")[:30], "file_name": "", "resurl": "",
                    "content": (content or "")[:CONTENT_MAX], "note": (note or "")[:400],
                    "created_at": db.now_iso()})
    serr = index_save(creds, recs)
    if serr:
        return {"ok": False, "error": f"共享索引写入失败：{serr}"}
    return {"ok": True}


def share_delete_record(creds: dict, rid: str, uid: str) -> dict:
    """删除本人记录（Word 文件一并删；索引重写）。"""
    recs, ferr = index_fetch_merged(creds)
    if ferr:
        return {"ok": False, "error": f"共享索引读取失败：{ferr}"}
    target = next((r for r in (recs or []) if str(r.get("rid")) == str(rid)), None)
    if not target:
        return {"ok": False, "error": "记录不存在"}
    if (target.get("share_uid") or "") != uid:
        return {"ok": False, "error": "只能删除自己上传的共享记录"}
    fname = target.get("file_name") or ""
    pan_msg = "成功"
    if fname:
        pan_msg = pan_delete(creds, fname) or "成功"
    new_recs = [r for r in recs if str(r.get("rid")) != str(rid)]
    serr = index_save(creds, new_recs)
    if serr:
        return {"ok": False, "error": f"共享索引写入失败：{serr}", "pan_msg": pan_msg}
    _add_tombstone(rid)   # v2.21.9b 墓碑：其他机器的陈旧缓存/CDN 副本不得复活已删记录
    return {"ok": True, "pan_msg": pan_msg}


def test_creds(creds: dict) -> tuple:
    """测试连接 = 拉共享索引（404 视为正常空圈）。"""
    recs, err = index_fetch(creds)
    if err:
        return False, err
    return True, "网盘与共享索引访问正常"


def upload_word_share(creds: dict, kind: str, rid: int, note: str = "") -> dict:
    """把一份专报 Word 共享：取已导出文件（无则先导出）→ 网盘上传 → 索引登记。"""
    from app import db
    from app.exporter import docx_builder as ex
    if kind == "custom":   # v2.21.8 修复：自定义专报与综合同表 multi_reports，此前一律报"未知专报类型"
        kind = "multi"
    if kind == "single":
        rep = db.get_report(rid)
        if not rep:
            return {"ok": False, "error": "专报不存在"}
        title, issue = rep.get("title") or "", str(rep.get("issue_no") or "")
        path = rep.get("docx_path") or ""
        if not path or not _os_path_exists(path):
            r = ex.export_report(rid)
            if not r.get("ok"):
                return {"ok": False, "error": "该专报尚未导出，自动导出失败：" + str(r.get("error") or "")}
            path = r["path"]
    elif kind == "multi":
        m = db.get_multi_report(rid)
        if not m:
            return {"ok": False, "error": "综合/自定义专报不存在"}
        title, issue = m.get("title") or "", str(m.get("issue_no") or "")
        path = m.get("docx_path") or ""
        if not path or not _os_path_exists(path):
            r = ex.export_multi_report(rid)
            if not r.get("ok"):
                return {"ok": False, "error": "该专报尚未导出，自动导出失败：" + str(r.get("error") or "")}
            path = r["path"]
    else:
        return {"ok": False, "error": "未知专报类型"}

    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        return {"ok": False, "error": f"读取导出文件失败：{e}"}
    uid = creds["uid"]
    stamp = (db.now_iso()[:10]).replace("-", "")
    base_name = "".join(ch if (ch.isalnum() or ch in "_-（）()") else "_"
                        for ch in f"{uid}_{issue}_{title[:60]}_{stamp}")[:150] or f"share_{rid}"
    file_name = base_name + ".docx"
    r = share_word_file(creds, str(rid), title, issue, file_name, blob,
                        note=note, share_name=db.get_setting("share_display_name") or "")
    if r.get("ok"):
        db.log("share", f"专报共享已上传：{title[:40]} -> {file_name}")
        r["title"] = title
        r["kind_label"] = "综合/自定义" if kind == "multi" else "单篇"
    return r


def _os_path_exists(path: str) -> bool:
    import os
    return os.path.isfile(path)
