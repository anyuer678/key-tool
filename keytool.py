#!/usr/bin/env python3
"""key-tool CLI：签发 / 管理 / 消费，一条命令完成。

用法示例：
    python keytool.py issue --calls 100 --tokens 1000000 --project proj1
    python keytool.py list --status active
    python keytool.py status <key_id>
    python keytool.py chat <key> "你好" --max-tokens 256
    python keytool.py project --name demo --balance 10

全局配置（或环境变量）：
    --url http://127.0.0.1:8000   （KEYTOOL_URL）
    --admin-token xxx             （KEYTOOL_ADMIN_TOKEN，管理操作需要）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

DEFAULT_URL = os.environ.get("KEYTOOL_URL", "http://127.0.0.1:8000")


def _client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=120.0)


def _headers(admin_token: str | None, api_key: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    if admin_token:
        h["X-Admin-Token"] = admin_token
    return h


def _die(msg: str, code: int = 1) -> None:
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- 命令实现 ----------


def cmd_issue(args) -> None:
    body = {
        "calls": args.calls, "tokens": args.tokens, "expires": args.expires,
        "cycle": args.cycle, "provider": args.provider, "model": args.model,
        "project_id": args.project, "rps_limit": args.rps, "note": args.note,
    }
    body = {k: v for k, v in body.items() if v is not None}
    with _client(args.url) as c:
        r = c.post("/keys", json=body, headers=_headers(args.admin_token))
    if r.status_code != 201:
        _die(f"签发失败 HTTP {r.status_code}: {r.text}")
    data = r.json()
    print(f"ID:    {data['id']}")
    print(f"Key:   {data['key']}")
    print(f"限额:  {json.dumps(data['limits'], ensure_ascii=False)}")
    print("⚠ 明文只显示这一次，请立即保存")


def cmd_list(args) -> None:
    params = {"limit": 100}
    if args.status:
        params["status"] = args.status
    if args.project:
        params["project_id"] = args.project
    with _client(args.url) as c:
        r = c.get("/keys", params=params, headers=_headers(args.admin_token))
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    data = r.json()
    if not data["items"]:
        print("（无 key）")
        return
    print(f"{'ID':<14}{'状态':<8}{'次数':<12}{'Token':<16}{'绑定':<28}{'项目'}")
    for k in data["items"]:
        binding = (k["provider"] or "") + ("/" + k["model"] if k["model"] else "")
        print(f"{k['id']:<14}{k['status']:<8}"
              f"{k['calls']['used']}/{k['calls']['total'] or '∞':<9}"
              f"{k['tokens']['used']}/{k['tokens']['total'] or '∞':<11}"
              f"{binding:<28}{k['project_id'] or ''}")


def cmd_status(args) -> None:
    with _client(args.url) as c:
        r = c.get(f"/keys/{args.key_id}")
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))


def cmd_logs(args) -> None:
    with _client(args.url) as c:
        r = c.get(f"/keys/{args.key_id}/logs", headers=_headers(args.admin_token))
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    for l in r.json()["items"]:
        print(f"{l['created_at']}  {l['action']:<16} amount={l['amount']} "
              f"cost={l['call_cost']}  ¥{l['cost']}")


def cmd_revoke(args) -> None:
    with _client(args.url) as c:
        r = c.post(f"/keys/{args.key_id}/revoke", headers=_headers(args.admin_token))
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    print(f"已吊销 {args.key_id}")


def cmd_rotate(args) -> None:
    with _client(args.url) as c:
        r = c.post(f"/keys/{args.key_id}/rotate", headers=_headers(args.admin_token))
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    data = r.json()
    print(f"旧 key 已吊销，新 key: {data['key']}")
    print("⚠ 明文只显示这一次，请立即保存")


def cmd_recharge(args) -> None:
    body = {}
    if args.calls is not None:
        body["calls"] = args.calls
    if args.tokens is not None:
        body["tokens"] = args.tokens
    if not body:
        _die("请提供 --calls 或 --tokens")
    with _client(args.url) as c:
        r = c.post(f"/keys/{args.key_id}/recharge", json=body,
                   headers=_headers(args.admin_token))
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    print(f"已充值 {args.key_id}: {body}")


def cmd_project(args) -> None:
    body = {"name": args.name, "owner": args.owner, "note": args.note}
    for k, v in (("calls", args.calls), ("tokens", args.tokens), ("balance", args.balance)):
        if v is not None:
            body[k] = v
    with _client(args.url) as c:
        r = c.post("/projects", json=body, headers=_headers(args.admin_token))
    if r.status_code != 201:
        _die(f"HTTP {r.status_code}: {r.text}")
    print(f"项目已创建: {r.json()['id']} {args.name}")


def cmd_projects(args) -> None:
    with _client(args.url) as c:
        r = c.get("/projects", headers=_headers(args.admin_token))
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    for p in r.json()["items"]:
        bal = "不计费" if p["balance"] is None else f"¥{p['balance']}"
        print(f"{p['id']:<12}{p['name']:<20} 次数 {p['calls']['used']}/{p['calls']['total'] or '∞'}  "
              f"token {p['tokens']['used']}/{p['tokens']['total'] or '∞'}  {bal}")


def cmd_chat(args) -> None:
    with _client(args.url) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": args.prompt}],
                  "max_tokens": args.max_tokens, "stream": args.stream},
            headers=_headers(args.admin_token, api_key=args.key),
        )
    if args.stream:
        if r.status_code != 200:
            _die(f"HTTP {r.status_code}: {r.text}")
        for line in r.iter_lines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        if delta.get("content"):
                            print(delta["content"], end="", flush=True)
                    except Exception:
                        pass
        print()
        return
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    data = r.json()
    print(data["choices"][0]["message"]["content"])
    usage = data.get("usage", {})
    print(f"\n[usage] {usage.get('total_tokens', 0)} tokens")


def cmd_providers(args) -> None:
    with _client(args.url) as c:
        r = c.get("/providers")
    if r.status_code != 200:
        _die(f"HTTP {r.status_code}: {r.text}")
    data = r.json()
    for p in data["providers"]:
        mark = " ★默认" if p["id"] == data["default"] else ""
        print(f"{p['id']:<16}{p['name']:<16} 模型: {', '.join(p['models'])}{mark}")


# ---------- 参数解析 ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="keytool", description="key-tool CLI")
    p.add_argument("--url", default=DEFAULT_URL, help=f"服务地址（默认 {DEFAULT_URL}）")
    p.add_argument("--admin-token", default=os.environ.get("KEYTOOL_ADMIN_TOKEN"),
                   help="管理令牌（默认取环境变量 KEYTOOL_ADMIN_TOKEN）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_key_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--calls", type=int)
        sp.add_argument("--tokens", type=int)
        sp.add_argument("--expires")
        sp.add_argument("--cycle", choices=["monthly", "daily", "hourly"])
        sp.add_argument("--provider")
        sp.add_argument("--model")
        sp.add_argument("--project")
        sp.add_argument("--rps", type=int)
        sp.add_argument("--note")

    s = sub.add_parser("issue", help="签发 key")
    add_key_args(s)
    s.set_defaults(func=cmd_issue)

    s = sub.add_parser("list", help="列 key")
    s.add_argument("--status", choices=["active", "revoked"])
    s.add_argument("--project")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("status", help="查余量")
    s.add_argument("key_id")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("logs", help="审计日志")
    s.add_argument("key_id")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("revoke", help="吊销")
    s.add_argument("key_id")
    s.set_defaults(func=cmd_revoke)

    s = sub.add_parser("rotate", help="吊销重签")
    s.add_argument("key_id")
    s.set_defaults(func=cmd_rotate)

    s = sub.add_parser("recharge", help="充值")
    s.add_argument("key_id")
    s.add_argument("--calls", type=int)
    s.add_argument("--tokens", type=int)
    s.set_defaults(func=cmd_recharge)

    s = sub.add_parser("project", help="创建项目")
    s.add_argument("--name", required=True)
    s.add_argument("--owner")
    s.add_argument("--calls", type=int)
    s.add_argument("--tokens", type=int)
    s.add_argument("--balance", type=float)
    s.add_argument("--note")
    s.set_defaults(func=cmd_project)

    s = sub.add_parser("projects", help="列项目")
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("chat", help="对话（OpenAI 兼容端点）")
    s.add_argument("key", help="API key")
    s.add_argument("prompt")
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--stream", action="store_true")
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("providers", help="列服务商")
    s.set_defaults(func=cmd_providers)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
