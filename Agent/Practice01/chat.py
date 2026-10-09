import configparser
import json
import requests

cfg = configparser.ConfigParser()
cfg.read("config.ini", encoding="utf-8")
llm = cfg["llm"]

messages = []
while True:
    prompt = input("你: ")
    messages.append({"role": "user", "content": prompt})
    resp = requests.post(
        llm["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + llm["api_key"]},
        json={"model": llm["model"], "messages": messages, "stream": True},
        stream=True,
        timeout=60,
    )
    resp.raise_for_status()
    answer = ""
    for line in resp.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        text = json.loads(data)["choices"][0]["delta"].get("content")
        if text:
            answer += text
            print(text, end="", flush=True)
    print()
    messages.append({"role": "assistant", "content": answer})
