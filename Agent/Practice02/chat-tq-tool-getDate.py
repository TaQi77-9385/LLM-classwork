import configparser
import json
import subprocess
import requests

cfg = configparser.ConfigParser()
cfg.read("config.ini", encoding="utf-8")
llm = cfg["llm"]

tools = [
    {
        "type": "function",
        "function": {
            "name": "getDate",
            "description": "获取当前电脑的日期和时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "isLeapYear",
            "description": "判断输入的年份是否为闰年",
            "parameters": {
                "type": "object",
                "properties": {"year": {"type": "integer", "description": "年份"}},
                "required": ["year"],
            },
        },
    },
]


def getDate():
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", "date"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return out.strip()


def isLeapYear(year):
    year = int(year)
    return "是闰年" if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else "不是闰年"


def run_tool(call):
    name = call["function"]["name"]
    if name == "getDate":
        return getDate()
    if name == "isLeapYear":
        args = json.loads(call["function"]["arguments"] or "{}")
        return isLeapYear(args["year"])
    return "未知工具"


def ask(messages):
    resp = requests.post(
        llm["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + llm["api_key"]},
        json={"model": llm["model"], "messages": messages, "tools": tools, "stream": True},
        stream=True,
        timeout=60,
    )
    resp.raise_for_status()
    answer = ""
    calls = {}
    for line in resp.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        delta = json.loads(data)["choices"][0]["delta"]
        text = delta.get("content")
        if text:
            answer += text
            print(text, end="", flush=True)
        for call in delta.get("tool_calls") or []:
            item = calls.setdefault(call.get("index", 0), {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if call.get("id"):
                item["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                item["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                item["function"]["arguments"] += fn["arguments"]
    if answer:
        print()
    return answer, [calls[i] for i in sorted(calls)]


prompt = input("你: ")
messages = [{"role": "user", "content": prompt}]
while True:
    answer, calls = ask(messages)
    if not calls:
        break
    messages.append({"role": "assistant", "content": answer or None, "tool_calls": calls})
    for call in calls:
        result = run_tool(call)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
