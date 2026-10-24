import json

import httpx

GW = "http://gw/v1/chat/completions"
ING = "http://ing/search"


def llm_reply(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def tool_call(name, args, id_="c1"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def hit(score=0.8, title="ПВТР", page=3, doc_id="d1", text="Рабочее время с 9 до 18."):
    return {"score": score, "doc_id": doc_id, "title": title, "page": page, "text": text}


SENT_JSON = json.dumps({"sentiment": "neutral", "score": 0, "topics": ["график и отпуск"], "red_flag": False, "reason": ""})
