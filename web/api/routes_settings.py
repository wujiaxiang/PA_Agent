"""REST routes for settings CRUD."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import asyncio

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from fastapi.responses import JSONResponse

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import load_settings, persist_patch, save_settings
from pa_agent.util.logging import register_settings_secrets

logger = logging.getLogger("pa_agent.web.settings")

router = APIRouter(tags=["settings"])


#: Credential fields masked in ``GET /api/settings``.
#:
#: Previously only ``provider.api_key`` was masked, so Feishu ``secret`` /
#: ``app_secret`` / ``webhook_url``, the PushPlus token, the Tushare token and the
#: TradingView password/session were returned in plaintext. Combined with
#: ``allow_origins=["*"]`` and no authentication, any web page the user visited
#: could read every one of them cross-origin.
#:
#: The settings form refills from this response and posts the value straight back,
#: so :func:`_is_masked_key` + the ``_SECRET_FIELDS`` guard in ``put_settings``
#: ensure a masked placeholder is never written back over the real value.
_SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider", "api_key"),
    ("feishu", "secret"),
    ("feishu", "app_secret"),
    ("feishu", "webhook_url"),
    ("pushplus", "token"),
    ("tushare", "token"),
    ("tradingview", "password"),
    ("tradingview", "session_id"),
)


def _mask_placeholder(value: str) -> str:
    """Render *value* as a non-reversible placeholder that round-trips safely."""
    if not isinstance(value, str) or not value.strip():
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "****"
    return f"{v[:4]}****{v[-4:]}"


#: ``general`` 段里的游标字段 —— 逐个映射到 :func:`resolve_view` 的返回值。
#: 这三个字段属 L3 会话级（``docs/SESSION_STORAGE_DESIGN.md`` §5.1），**不在**
#: 用户层/系统兜底里持久化；GET 只是把「本 tab 此刻在看什么」告诉前端。
_CURSOR_FIELDS: tuple[str, ...] = ("last_symbol", "last_timeframe", "last_tradingview_exchange")

#: 响应体顶层的「本会话没有游标」标记（会话过期 / 从未订阅）。
#:
#: **为什么必须可见**：会话过期后刷新**不是空白，是别人的数据** —— 回落全局
#: 出厂种子（实测 XAUUSD/15m），而用户之前在看 BTCUSDT。静默回落会被读成
#: 「数据丢了」，用户接下来做的第一件事（重跑分析、翻记录、导出）全都建立在
#: 一个自己没选过的品种上。这是 R2 最糟的形态：错的，不是空的。
#:
#: 前端**不需要**改也不会坏：这是纯增量字段，``loadSettings`` 只读
#: ``general.*``，多余键被忽略。想用它提示用户时再单独接（``showToast``）。
SESSION_CURSOR_MISSING_FLAG = "_session_cursor_missing"


def _apply_session_cursor(payload: dict, ctx, request: Request) -> None:
    """把 **本会话游标** 覆盖进响应体的 general 段（仅响应，不改全局配置）。

    **为什么必须做**：前端 ``loadSettings`` 把 ``general.last_symbol`` 灌进
    ``#ds-symbol``，随后的 ``applySubscribe`` 又把它 POST 回 ``/api/subscribe``。
    若这里返回全局游标，B tab 打开时会先读到 A tab 的标的，再把它当成自己的
    意图写回 —— **一次 F5 就串味**，且随后 ``sessions`` 快照也被污染。

    ``resolve_view`` 本身就是「会话游标优先，无会话/无游标时回落全局」，所以
    这里直接复用它，不另写一份判定。无 ``X-Session-Id`` 的调用方（老前端、
    脚本、测试）拿到的仍是全局值，行为与改造前完全一致。

    sid 有效却落到全局分支时额外挂 :data:`SESSION_CURSOR_MISSING_FLAG`
    —— 见该常量说明：会话过期后刷新不是空白，是出厂种子，必须让调用方看得见。
    """
    from web.api.session_ctx import resolve_view, session_cursor_of, session_id_of

    sid = session_id_of(request)
    if not sid:
        return
    symbol, timeframe, exchange = resolve_view(ctx, sid)
    general = payload.get("general")
    if isinstance(general, dict):
        resolved = {
            "last_symbol": symbol,
            "last_timeframe": timeframe,
            "last_tradingview_exchange": exchange,
        }
        for field in _CURSOR_FIELDS:
            value = resolved.get(field)
            if value:
                general[field] = value
    # 会话游标缺失 ⇒ 现在显示的这三个值**不是本会话选的**。
    # 判据用 session_cursor_of（不回落全局的那个），而不是「symbol 是否为空」：
    # 全局回落值恒非空，拿它当判据等于永远不报警。
    if session_cursor_of(sid) is None:
        payload[SESSION_CURSOR_MISSING_FLAG] = True


@router.get("/settings")
async def get_settings(request: Request):
    """Return current settings with every credential masked.

    ## 按请求返回

    传入 ``user_id=current_user_id(request)``，因此每个登录用户看到的是
    **自己**那份（系统兜底 ← 本人覆盖）。绝不能把 A 的覆盖回显给 B：
    那等于把 A 的 base_url / 通知凭证发到 B 的浏览器。

    ``general`` 段的游标字段（symbol/timeframe/exchange）返回的是**本会话游标**
    而非全局值，见 :func:`_apply_session_cursor`。

    本会话没有游标时（会话过期 / 从未订阅）额外返回
    ``_session_cursor_missing: true``：此刻那三个字段是**全局出厂种子**，不是
    用户选的，调用方有权知道这一点。
    """
    ctx = request.app.state.ctx
    from web.api.auth_ctx import current_user_id

    user_id = current_user_id(request)
    settings = await asyncio.to_thread(load_settings, SETTINGS_JSON_PATH, user_id=user_id)
    # 同步本请求的活引用：``ctx.settings`` 是 property，赋值只换**本请求**那份
    # （见 AppContext.settings 的 setter），不会把全局默认换成当前用户的配置。
    ctx.settings = settings
    # 每次载入都刷新脱敏注册表：换密钥后旧值失效、新值生效，
    # 否则新密钥会以明文写进 records/pending/*.json。
    register_settings_secrets(settings)
    d = settings.model_dump()
    for section, field in _SECRET_FIELDS:
        bucket = d.get(section)
        if isinstance(bucket, dict) and bucket.get(field):
            bucket[field] = _mask_placeholder(bucket[field])
    # 游标按本 tab 解析（L3），必须放在脱敏之后、返回之前。
    # 它只改响应体，**不回写** ctx.settings —— 全局游标是别的 tab 的。
    _apply_session_cursor(d, ctx, request)
    # 禁止缓存：防止前端读到旧 bar_count（TODO P0.2）
    return JSONResponse(content=d, headers={"Cache-Control": "no-store"})


def _is_masked_key(value) -> bool:
    """True when *value* is a masked placeholder emitted by ``GET /api/settings``.

    The GET handler renders every credential in :data:`_SECRET_FIELDS` through
    :func:`_mask_placeholder`, which always contains the ``****`` marker. The
    settings form refills from that response and posts the value straight back on
    save, so without this guard one "save settings" click overwrites the real
    credential on disk with the placeholder and every later call fails.
    """
    return isinstance(value, str) and "****" in value


def _should_keep_existing(section: str, field: str, value) -> bool:
    """Whether a submitted value must be ignored to protect the stored one."""
    if _is_masked_key(value):
        return True
    # For the provider key an empty submission means "form had nothing", not
    # "please wipe my key" — the form only ever sends back what it received.
    if (section, field) == ("provider", "api_key"):
        if isinstance(value, str) and not value.strip():
            return True
    return False


@router.put("/settings")
async def put_settings(request: Request, body: dict):
    """Merge *body* into current settings and save.

    赋值走 Pydantic 的 ``validate_assignment=True``：越界/类型不合法的值会被拒，
    而不是像 2026-10-05 那次一样被静默写入、把 base_url 与 api_key 冲成默认值。
    任一字段校验失败即整体回滚（见下方 staged copy），返回 400 并指明字段。

    响应里的 ``db_persisted`` 必须看：True 才表示这次改动进了配置真源（DB 用户层）。
    False 时本次改动只落了 settings.json 灾备副本 —— 系统兜底一旦存在，重启后
    **读不到那份文件**，用户会看到「保存成功」然后配置回到旧值。
    """
    ctx = request.app.state.ctx
    from web.api.auth_ctx import current_user_id

    # 写进**本请求用户**的覆盖区。不传 user_id 会落到 default_user_id()（admin），
    # 于是 B 的保存会写进 A 的配置区 —— 多用户下这是最典型的静默串号。
    user_id = current_user_id(request)
    current = ctx.settings
    masked_key_dropped = False
    # 先快照原值再统一提交：整段赋值是一体的，某个字段校验失败时无法只回滚
    # 那一个，必须靠快照还原 —— 否则会留下半套配置，比全盘拒绝更危险。
    # 元组最后一位记录 section 名：落库时要按段分组（见下方 patch 构建）。
    staged: list[tuple[object, str, object, object, str]] = []
    for section in ("provider", "prompt", "validation", "general", "feishu", "tushare", "pushplus", "tradingview"):
        if section in body and isinstance(body[section], dict):
            target = getattr(current, section, None)
            if target is not None:
                for k, v in body[section].items():
                    if hasattr(target, k):
                        # Never let a masked placeholder overwrite a stored
                        # credential — applies to every field in _SECRET_FIELDS,
                        # not just the provider key.
                        if (section, k) in _SECRET_FIELDS and _should_keep_existing(section, k, v):
                            masked_key_dropped = True
                            continue
                        staged.append((target, k, getattr(target, k), v, section))

    for target, key, original, value, _section in staged:
        try:
            setattr(target, key, value)
        except ValidationError as exc:
            for t2, k2, orig2, _v2, _s2 in staged:
                try:
                    setattr(t2, k2, orig2)
                except Exception:  # noqa: BLE001 - 还原的是已验证过的原值
                    logger.exception("回滚配置字段失败: %s.%s", type(t2).__name__, k2)
            first = exc.errors()[0] if exc.errors() else {}
            loc = ".".join(str(x) for x in (first.get("loc") or (key,)))
            raise HTTPException(
                status_code=400,
                detail=f"配置无效：{loc} = {value!r}（{first.get('msg', '校验失败')}）",
            ) from exc
    # 落盘两份：① 用户自己的配置区（稀疏覆盖，日后此处才是该用户的真源）
    # ② settings.json 作为灾备/遗留副本 —— DB 损坏时仍能凭它启动。
    # 系统兜底区**不被用户改动触碰**：它是所有用户的只读默认值。
    #
    # **只提交 body 里真正声明过的键**（staged 就是「这次改了什么」的完整记录），
    # 绝不整份 `current.model_dump()` 进 DB —— 那会把 apply_env_overrides 盖上的
    # 15 个 .env 字段（含 API_KEY / BASE_URL）永久烧进用户层，此后用户改 .env
    # 不再生效。语义与 settings_store.merge_overrides 的说明一致：设置页保存属于
    # 「内部写入方」，只知道自己改了哪几个键，其余部分必须保留既有覆盖。
    patch: dict[str, dict[str, object]] = {}
    for target, key, _original, _value, section in staged:
        patch.setdefault(section, {})[key] = getattr(target, key)

    # 落库失败不冒泡（persist_patch 自己吞异常并返回 False）。**不再顺手回写
    # settings.json** —— 见 promote_to_baseline 的说明：那个文件是「出厂配置 /
    # 播种源 / 灾备兜底」，不是当前状态。每次保存都回写会让它跟着用户修改漂移，
    # 一旦 DB 被清空、baseline 重新从它播种，用户的历史修改就被当成出厂默认
    # 固化成所有用户继承的基线。想把当前配置定为新出厂默认，请显式调
    # POST /api/settings/promote-default。
    db_persisted = await asyncio.to_thread(persist_patch, patch, user_id=user_id)
    if not db_persisted:
        logger.error(
            "settings 未进入用户配置区（仅存于进程内存，重启会丢）：%s",
            ",".join(sorted(patch)) or "<no change>",
        )

    from pa_agent.util.logging import register_settings_secrets, update_api_key
    update_api_key(current.provider.api_key)
    # 注册全部凭据（飞书 webhook/secret/app_secret、PushPlus/Tushare token、
    # TradingView 凭证）到脱敏注册表，避免它们经由 requests 异常串进日志。
    register_settings_secrets(current)

    # Rebuild AI client if provider changed (may probe the provider; offload it)
    from pa_agent.ai.client_factory import create_ai_client
    ctx.client = await asyncio.to_thread(
        create_ai_client, current.provider, logger_=ctx.logger
    )

    return {
        "status": "saved",
        "api_key_masked_ignored": masked_key_dropped,
        "db_persisted": db_persisted,
    }


_FEISHU_ERR_HINT = {
    19021: "IP 不在白名单（机器人已被禁用）",
    19022: "secret 与机器人配置不匹配",
    19024: "Webhook 已失效或被删除",
}


@router.post("/feishu/test")
async def feishu_test(request: Request, body: dict):
    """发送一条飞书测试消息，验证 webhook 与 secret 配置是否有效。

    Body 字段：webhook_url / secret（与 PUT /api/settings.feishu 一致）。
    """
    webhook = (body.get("webhook_url") or "").strip()
    secret = (body.get("secret") or "").strip()
    if not webhook:
        raise HTTPException(status_code=400, detail="webhook_url 不能为空")

    payload = {
        "msg_type": "text",
        "content": {"text": "PA Agent Web 飞书通知测试"},
    }
    if secret:
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign

    try:
        # Offloaded: this is an outbound HTTPS POST with a 10s timeout. Calling it
        # inline would block the whole event loop for up to 10 seconds, freezing
        # every SSE stream and every other in-flight request.
        resp = await asyncio.to_thread(
            requests.post, webhook, json=payload, timeout=10
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"请求失败: {exc}")

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"非 JSON 响应: {resp.text[:200]}")

    code = data.get("code", 0)
    status_code = data.get("StatusCode", 0)
    if code == 0 and status_code == 0:
        return {"status": "ok", "raw": data}

    hint = _FEISHU_ERR_HINT.get(code) or _FEISHU_ERR_HINT.get(status_code)
    msg = f"飞书返回错误 code={code} StatusCode={status_code} msg={data.get('msg', '')}"
    if hint:
        msg += f"（{hint}）"
    raise HTTPException(status_code=502, detail=msg)


@router.post("/settings/promote-default")
async def promote_settings_to_default(request: Request, body: dict | None = None):
    """把**当前生效配置**显式提升为系统出厂默认（baseline + 播种源）。

    ## 为什么需要显式动作

    ``settings.json`` 的职责只有三件，且都是「出厂」语义：
    首次播种源、灾备兜底、以及出厂配置的落盘。它**不是当前状态**。

    过去 ``PUT /api/settings`` 每次保存都顺手回写它，于是文件跟着用户修改漂移。
    后果很隐蔽：DB 一旦被清空，baseline 从这个文件重新播种，用户的历史修改就被
    当成出厂默认**固化成所有用户继承的基线** —— 谁改过什么、再也分不清。

    ## 行为

    1. 当前生效配置（用户覆盖已合并）写入 ``global_config.settings.baseline``
    2. 同步写 ``settings.json``，使**全新空库**能播种出同一份配置
    3. **清掉本用户的覆盖区** —— 已成出厂默认，留着只会遮蔽后续的系统更新

    ## 影响面

    这是**全局**动作：之后所有用户继承这份配置。body 必须显式带
    ``{"confirm": true}``，避免误触。
    """
    ctx = request.app.state.ctx
    if not (body or {}).get("confirm"):
        raise HTTPException(
            status_code=400,
            detail=(
                "该操作会把当前配置设为所有用户的出厂默认，并清除你的个人覆盖。"
                "确认请提交 {\"confirm\": true}。"
            ),
        )

    current = ctx.settings.model_dump(mode="json")

    from pa_agent.storage.settings_store import promote_to_baseline
    from web.api.auth_ctx import current_user_id

    # 传实际发起者：清的是「本次操作者」的覆盖区。传空会退化成清 admin 的 ——
    # 单机下两者恒等，多用户下会让发起者自己的覆盖继续遮蔽新的出厂默认。
    user_id = current_user_id(request)

    try:
        promoted = await asyncio.to_thread(promote_to_baseline, current, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("promote_to_baseline failed")
        raise HTTPException(status_code=500, detail=f"提升失败：{exc}") from exc

    if not promoted:
        raise HTTPException(status_code=503, detail="存储层不可用，未能写入系统默认")

    return {
        "status": "promoted",
        "settings_json_updated": True,
        "note": "当前配置已成为系统出厂默认，你的个人覆盖已清除",
    }
