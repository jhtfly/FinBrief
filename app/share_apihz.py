# -*- coding: utf-8 -*-
"""v2.21.4 专报共享（apihz 接口盒子）：
- 轻云数据表（cunchu/qyun.php）：共享记录的增删改查（标题/期号/uid/直链/快捷分享正文）
- 联盟轻量网盘（ziyuan/pan.php / pandel.php）：Word 文件上传/删除；下载=直链公开 GET

开发者 ID/KEY 存 DB 加密态（DPAPI，db.get/set_secret）；共享圈=共用同一 ID/KEY，
圈内以 share_uid 辨认分享者，无额外验证机制。表结构（用户在 apihz 后台按此重建）：
share_uid, share_name, kind(text/file), title, issue_no, file_name, resurl,
content, note, created_at（主键 id 自动）。"""
import base64
import json as _j

import httpx

QYUN_URL = "https://cn.apihz.cn/api/cunchu/qyun.php"
PAN_URL = "https://cn.apihz.cn/api/ziyuan/pan.php"
PAN_DEL_URL = "https://cn.apihz.cn/api/ziyuan/pandel.php"
PAN_MAX = 2 * 1024 * 1024   # 网盘单文件 base64 后上限 2M

_FIELDS = ["share_uid", "share_name", "kind", "title", "issue_no",
           "file_name", "resurl", "content", "note", "created_at"]


def get_creds() -> dict:
    """读取共享凭据：ID/KEY 走 DPAPI 加密存储；表 ID/uid/好友为普通设置。"""
    from app import db
    return {
        "id": db.get_secret("apihz_id"),
        "key": db.get_secret("apihz_key"),
        "table": (db.get_setting("apihz_table") or "").strip(),
        "uid": (db.get_setting("share_uid") or "").strip(),
        "friends": _parse_friends(db.get_setting("share_friends") or ""),
    }


def _parse_friends(raw: str) -> list:
    try:
        arr = _j.loads(raw or "[]")
        if isinstance(arr, list):
            return [{"name": str(x.get("name", "")).strip()[:20],
                     "uid": str(x.get("uid", "")).strip()[:40]}
                    for x in arr if str(x.get("uid", "")).strip()]
    except (ValueError, TypeError):
        pass
    return []


def creds_ready() -> bool:
    c = get_creds()
    return bool(c["id"] and c["key"] and c["table"])


def esc(v: str) -> str:
    """SQL 值转义：单引号翻倍；半角 + 换全角＋（qyun 网关会把 + 吃成空格）。"""
    s = str(v or "")
    return s.replace("'", "''").replace("+", "＋")


def _post(url: str, data: dict, timeout: int = 25):
    """统一 POST；返回 (json|None, err)。便于测试拦截。"""
    try:
        with httpx.Client(timeout=timeout, verify=False) as cli:
            resp = cli.post(url, data=data)
            resp.raise_for_status()
            return resp.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def qyun(creds: dict, sql: str, op: int):
    """轻云数据表执行：op=1 增删改 / 2 查询。返回 (data|None, msg, err)。"""
    if not (creds.get("id") and creds.get("key") and creds.get("table")):
        return None, "", "共享凭据未配置（设置 → 专报共享）"
    body = {"id": creds["id"], "key": creds["key"], "type": str(op),
            "name": creds["table"], "sql": sql}
    data, err = _post(QYUN_URL, body)
    if err:
        return None, "", err
    if not isinstance(data, dict):
        return None, "", "接口返回异常"
    if data.get("code") != 200:
        return None, str(data.get("msg") or "接口返回错误"), None
    return data.get("data"), str(data.get("msg") or ""), None


def record_insert(creds: dict, rec: dict) -> tuple:
    cols = ",".join(f"`{f}`" for f in _FIELDS)
    vals = ",".join(f"'{esc(rec.get(f, ''))}'" for f in _FIELDS)
    return qyun(creds, f"INSERT INTO `{creds['table']}` ({cols}) VALUES ({vals})", 1)


def record_query(creds: dict, uid: str = "", kw: str = "", limit: int = 100) -> tuple:
    where = "WHERE 1=1"
    if uid.strip():
        where += f" AND `share_uid`='{esc(uid.strip())}'"
    if kw.strip():
        k = esc(kw.strip())
        where += (f" AND (`title` LIKE '%{k}%' OR `note` LIKE '%{k}%'"
                  f" OR `issue_no` LIKE '%{k}%' OR `content` LIKE '%{k}%')")
    sql = (f"SELECT `id`,{','.join(f'`{f}`' for f in _FIELDS)} "
           f"FROM `{creds['table']}` {where} ORDER BY `id` DESC LIMIT {int(limit)}")
    return qyun(creds, sql, 2)


def record_get(creds: dict, rid: int) -> tuple:
    sql = (f"SELECT `id`,{','.join(f'`{f}`' for f in _FIELDS)} "
           f"FROM `{creds['table']}` WHERE `id`={int(rid)} LIMIT 1")
    data, msg, err = qyun(creds, sql, 2)
    if err or not isinstance(data, list):
        return None, msg, err or "接口返回异常"
    return (data[0] if data else None), msg, None


def record_delete(creds: dict, rid: int, uid: str) -> tuple:
    """删除记录（仅允许删除本人 share_uid 的记录，防误删他人）。"""
    sql = (f"DELETE FROM `{creds['table']}` "
           f"WHERE `id`={int(rid)} AND `share_uid`='{esc(uid)}'")
    return qyun(creds, sql, 1)


def _pan_base_name(file_name: str) -> str:
    """网盘 name 参数不含后缀（type 参数带后缀，网盘会自动拼接；实测 name 带后缀会变成 xxx.docx.docx）。"""
    base = file_name[:200]
    if "." in base:
        head, ext = base.rsplit(".", 1)
        if 1 <= len(ext) <= 5 and "/" not in ext and "\\" not in ext:
            base = head
    return base


def pan_upload(creds: dict, file_name: str, data: bytes) -> tuple:
    """网盘上传：BASE64 放 res 参数（非 multipart）。返回 (resurl|None, err)。"""
    if len(data) > PAN_MAX:
        return None, f"文件 base64 后超过 2M 上限（当前 {len(data)/1048576:.2f}M）"
    suffix = "." + (file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin")
    if suffix in (".php", ".html", ".htm"):
        return None, "网盘禁止该类型文件"
    body = {"id": creds["id"], "key": creds["key"], "type": suffix,
            "name": _pan_base_name(file_name), "res": base64.b64encode(data).decode("ascii")}
    out, err = _post(PAN_URL, body, timeout=60)
    if err:
        return None, err
    if not isinstance(out, dict):
        return None, "接口返回异常"
    if out.get("code") != 200:
        return None, str(out.get("msg") or "上传失败")
    resurl = out.get("resurl") or ""
    if not resurl:
        return None, "上传成功但未返回直链"
    return resurl, None


def pan_delete(creds: dict, file_name: str) -> tuple:
    """pandel 的 name 需完整文件名（含后缀，文档示例 name=接口盒子.jpg）。"""
    base = _pan_base_name(file_name)
    dot = "." + (file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "docx")
    full = base if base.lower().endswith(dot) else base + dot
    body = {"id": creds["id"], "key": creds["key"], "name": full[:200]}
    out, err = _post(PAN_DEL_URL, body)
    if err:
        return err
    if not isinstance(out, dict):
        return "接口返回异常"
    if out.get("code") != 200:
        return str(out.get("msg") or "删除失败")
    return None


def upload_word_share(creds: dict, kind: str, rid: int, note: str = "") -> dict:
    """把一份专报 Word 共享：取已导出文件（无则先导出）→ 网盘上传 → 数据表写记录。

    kind: single=单篇专报 / multi=综合专报（含自定义专报）。
    期号语义不变（导出走原有 exporter，共享不额外占号）。"""
    from app import db
    from app.exporter import docx_builder as ex
    if kind == "single":
        rep = db.get_report(rid)
        if not rep:
            return {"ok": False, "error": "专报不存在"}
        title = rep.get("title") or ""
        issue = str(rep.get("issue_no") or "")
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
        title = m.get("title") or ""
        issue = str(m.get("issue_no") or "")
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
    uid = creds.get("uid") or "user"
    stamp = (db.now_iso()[:10]).replace("-", "")
    base_name = "".join(ch if (ch.isalnum() or ch in "_-（）()") else "_"
                        for ch in f"{uid}_{issue}_{title[:60]}_{stamp}")[:150] or f"share_{rid}"
    file_name = base_name + ".docx"
    resurl, err = pan_upload(creds, file_name, blob)
    if err:
        # 同名文件已在网盘时提示可先删除重传（网盘限制同名一次）
        return {"ok": False, "error": f"网盘上传失败：{err}", "file_name": file_name}
    rec = {"share_uid": uid,
           "share_name": (db.get_setting("share_display_name") or "")[:60],
           "kind": "file", "title": title[:190], "issue_no": issue[:30],
           "file_name": file_name, "resurl": resurl, "content": "",
           "note": (note or "")[:400], "created_at": db.now_iso()}
    data, msg, qerr = record_insert(creds, rec)
    if qerr:
        return {"ok": False, "error": f"共享记录写入失败：{qerr}", "resurl": resurl,
                "file_name": file_name}
    db.log("share", f"专报共享已上传：{title[:40]} -> {file_name}")
    return {"ok": True, "title": title, "file_name": file_name,
            "resurl": resurl, "kind_label": "综合/自定义" if kind == "multi" else "单篇"}


def _os_path_exists(path: str) -> bool:
    import os
    return os.path.isfile(path)
