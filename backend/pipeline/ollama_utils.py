import time
import requests

def build_payload(config, task_type, prompt, system=None):
    """根据传入的 config 构建 Ollama 请求体"""
    if task_type not in config["ollama"]:
        raise ValueError(f"Unknown task type: {task_type}")
    task_cfg = config["ollama"][task_type]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": task_cfg["model"],
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": task_cfg.get("temperature", 0.1),
            "num_ctx": task_cfg.get("num_ctx", 2048),
            "num_predict": task_cfg.get("max_tokens", 512)
        }
    }
    return payload

def chat(config, task_type, prompt, system=None, json_mode=False):
    """统一对话接口，需要传入 config"""
    payload = build_payload(config, task_type, prompt, system)
    if json_mode:
        payload["format"] = "json"
    url = f"{config['ollama'][task_type]['base_url']}/api/chat"
    start = time.time()
    resp = requests.post(url, json=payload)
    end = time.time()
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama API error ({resp.status_code}): {resp.text}")
    data = resp.json()
    content = data["message"]["content"]
    model = payload["model"]
    print(f"[{task_type}] model={model}, time={end-start:.1f}s, chars={len(content)}")
    return content

def get_concurrency(config, task_type):
    return config["ollama"].get(task_type, {}).get("concurrency", 1)