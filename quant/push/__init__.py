# -*- coding: utf-8 -*-
"""统一推送层(M6):微信通道,消息必达策略。

通道:
  wechat_test  微信公众平台·测试号(官方沙箱,自建,免费无第三方,消息进微信「服务通知」)
  wxpusher     WxPusher(第三方认证服务号转发,免费,配置最简)

必达机制:发送失败 → 重试3次 → 仍失败落盘 reports/push_failed.log,
次日复盘报告会提示补发;所有发送记录(成败)入 reports/push_log。
"""

import datetime as dt
import logging
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant.lib.httputil import get_json  # noqa: E402

logger = logging.getLogger(__name__)

_REPORTS = os.path.join(ROOT, "reports")


def _secrets():
    import yaml
    with open(os.path.join(ROOT, "config", "secrets.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _log_push(line, fname):
    os.makedirs(_REPORTS, exist_ok=True)
    with open(os.path.join(_REPORTS, fname), "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")


# ---------------------------------------------------------------- 测试号通道

_token_cache = {"token": None, "expire": 0}


def _wt_token(appid, secret):
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire"]:
        return _token_cache["token"]
    j = get_json("https://api.weixin.qq.com/cgi-bin/token",
                 params={"grant_type": "client_credential",
                         "appid": appid, "secret": secret}, timeout=10)
    tok = (j or {}).get("access_token")
    if not tok:
        raise RuntimeError(f"测试号 token 获取失败: {j}")
    _token_cache.update({"token": tok, "expire": now + 7000})
    return tok


def _tpl_clean(s):
    """模板消息纯文本化:去掉 markdown 星号,换行合并为 | (模板字段不渲染换行)。"""
    s = str(s).replace("**", "").replace("#", "").replace("◆", "")
    return " | ".join(x.strip(" -|") for x in s.splitlines() if x.strip(" -|"))


def _tpl_chunks(content, size=40, max_chunks=6):
    """按语义行切分为 ≤size 字的分片(模板消息 content 仅显示约两行,超出折叠)。"""
    lines = _tpl_clean(content).split(" | ")
    chunks, cur = [], ""
    for ln in lines:
        while len(ln) > size:  # 单行超长先硬切
            chunks.append(ln[:size])
            ln = ln[size:]
        if len(cur) + len(ln) + 3 <= size:
            cur = f"{cur} | {ln}" if cur else ln
        else:
            if cur:
                chunks.append(cur)
            cur = ln
    if cur:
        chunks.append(cur)
    if len(chunks) > max_chunks:
        head, tail = chunks[:max_chunks - 1], chunks[-1]
        chunks = head + [f"…{tail} (其余见晚间本地报告)"]
    return chunks


def send_wechat_test(title, content, retries=3):
    """经测试号发模板消息(自动分片多条)。

    测试号模板消息限制:content 字段卡片内仅显示约两行(~45字)后折叠为…,
    不可点击展开(未设 url),不渲染换行。故长内容拆为多条短消息(≤6条)。
    模板需三个字段:title / content / time。
    """
    import requests
    sec = _secrets().get("wechat_test") or {}
    appid, secret = sec.get("appid"), sec.get("secret")
    openid, tpl_id = sec.get("openid"), sec.get("template_id")
    if not all([appid, secret, openid, tpl_id]):
        raise RuntimeError("secrets.yaml 缺 wechat_test 配置(appid/secret/openid/template_id)")

    chunks = _tpl_chunks(content)
    ok_any = False
    last = None
    for i, part in enumerate(chunks, 1):
        t = str(title)[:20]
        if len(chunks) > 1:
            t = f"{t}({i}/{len(chunks)})"
        body = {
            "touser": openid,
            "template_id": tpl_id,
            "data": {
                "title": {"value": t},
                "content": {"value": part},
                "time": {"value": f"{dt.datetime.now():%m-%d %H:%M}"},
            },
        }
        for _ in range(retries):
            try:
                token = _wt_token(appid, secret)
                r = requests.post(
                    f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}",
                    json=body, timeout=10)
                j = r.json()
                if j.get("errcode") == 0:
                    ok_any = True
                    last = None
                    break
                if j.get("errcode") == 40001:  # token 失效,强制刷新后重试
                    _token_cache.update({"token": None, "expire": 0})
                last = j
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(2)
        if last is not None:
            raise RuntimeError(f"测试号发送失败(第{i}条): {last}")
        time.sleep(1)  # 分片间隔,避免触发频率限制
    return ok_any


# ---------------------------------------------------------------- WxPusher 通道

def _wxpusher_chunks(content, max_chars=3600):
    """按段落边界切分长正文,且保证各片拼接后与原文完全一致。"""
    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    text = str(content)
    if not text:
        return [text]

    chunks = []
    while len(text) > max_chars:
        split_at = text.rfind("\n", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = max_chars
        else:
            split_at += 1  # 换行符保留在前一段,分片拼接不丢字符
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text or not chunks:
        chunks.append(text)
    return chunks


def send_wxpusher(title, content, retries=3):
    """经 WxPusher 发送 Markdown;长报告分段按序发出,不截断正文。"""
    import requests
    sec = _secrets().get("wxpusher") or {}
    token, uid = sec.get("app_token"), sec.get("uid")
    if not all([token, uid]):
        raise RuntimeError("secrets.yaml 缺 wxpusher 配置(app_token/uid)")

    chunks = _wxpusher_chunks(content)
    for part_no, part in enumerate(chunks, 1):
        suffix = f" [{part_no}/{len(chunks)}]" if len(chunks) > 1 else ""
        part_title = f"{title}{suffix}"
        body = {"appToken": token, "content": part,
                "summary": part_title[:100],
                "contentType": 3, "uids": [uid]}  # 3=markdown
        last = None
        for attempt in range(retries):
            try:
                r = requests.post("https://wxpusher.zjiecode.com/api/send/message",
                                  json=body, timeout=10)
                j = r.json()
                if j.get("code") == 1000:
                    data = j.get("data")
                    if isinstance(data, list):  # data=[{uid,code,status}]:逐 uid 投递记录
                        rec = next((x for x in data if x.get("uid") == uid), {})
                        if rec.get("code") == 1000:
                            break
                        last = rec
                    else:
                        break
                else:
                    last = j
            except Exception as e:  # noqa: BLE001
                last = e
            if attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"WxPusher 发送失败(第{part_no}/{len(chunks)}段): {last}")
        if part_no < len(chunks):
            time.sleep(0.5)  # 顺序发送,避免短时间连续请求
    return True


# ---------------------------------------------------------------- 统一入口

_CHANNELS = {"wechat_test": send_wechat_test, "wxpusher": send_wxpusher}


def send_notice(title, content, channel=None, require_full=False):
    """发通知(默认取 config.yaml push.channel)。返回是否成功。

    失败自动降级:主通道异常 → 备用通道补发一次;仍失败才落盘 push_failed.log。
    require_full=True 时跳过会截断长文的测试号模板通道,避免把摘要误报为完整版。
    """
    import yaml
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("push") or {}
    ch = channel or cfg.get("channel")
    attempts = [ch] + ([alt for alt in ("wechat_test", "wxpusher") if alt != ch]
                       if ch in ("wxpusher", "wechat_test") else [])
    for i, name in enumerate(attempts):
        fn = _CHANNELS.get(name)
        if fn is None:
            continue
        if require_full and name == "wechat_test":
            _log_push(f"[SKIP][{name}] {title} (该通道限制无法保证完整版)", "push_log")
            continue
        try:
            ok = fn(title, content)
            tag_note = "" if i == 0 else f"({ch}失败后由{name}补发)"
            _log_push(f"[OK][{name}]{tag_note} {title} ({len(content)}字)", "push_log")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("push %s 失败: %s", name, e)
            tag = "主" if i == 0 else "备"
            _log_push(f"[FAIL][{name}][{tag}] {title} | {e}", "push_failed.log")
    # 全部通道失败:本地留档,内容不丢
    os.makedirs(_REPORTS, exist_ok=True)
    with open(os.path.join(_REPORTS, "push_inbox.md"), "a", encoding="utf-8") as f:
        f.write(f"\n## {dt.datetime.now():%Y-%m-%d %H:%M} {title}\n\n{content}\n")
    return False
