import subprocess
import json
import requests

# Pod status patterns to check
ERROR_STATUSES = [
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "Pending", "Failed"
]

# Local Ollama endpoint
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma:2b"

def get_all_pods():
    cmd = ["kubectl", "get", "pods", "-A", "-o", "json"]
    output = subprocess.check_output(cmd)
    return json.loads(output)

def get_pod_logs(namespace, pod_name):
    try:
        logs = subprocess.check_output(["kubectl", "logs", "-n", namespace, pod_name], stderr=subprocess.STDOUT)
        return logs.decode("utf-8")
    except subprocess.CalledProcessError as e:
        return f"Error fetching logs: {e.output.decode()}"

def get_pod_description(namespace, pod_name):
    try:
        description = subprocess.check_output(["kubectl", "describe", "pod", pod_name, "-n", namespace], stderr=subprocess.STDOUT)
        return description.decode("utf-8")
    except subprocess.CalledProcessError as e:
        return f"Error fetching description: {e.output.decode()}"

def send_to_gemma(logs, description):
    prompt = f"""
You are analyzing a Kubernetes pod that is experiencing an error.

Your task is to:
1. Explain what steps occurred **before the error**.
2. Identify **what the error is**.
3. Determine **what caused the error**, based on logs and pod description.

--- Logs ---
{logs}

--- Pod Description ---
{description}
"""
    response = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "prompt": prompt,
        "stream": False
    })

    return response.json().get("response", "No response from model")

def print_log(namespace, pod_name):
    try:
        logs = subprocess.check_output(["kubectl", "logs", "-n", namespace, pod_name], stderr=subprocess.STDOUT)
        logs_decoded = logs.decode("utf-8")
        return "\n".join(logs_decoded.splitlines()[:10])
    except subprocess.CalledProcessError as e:
        return f"Error fetching logs: {e.output.decode()}"

def main():
    pods = get_all_pods()
    for item in pods["items"]:
        status = item["status"]
        pod_name = item["metadata"]["name"]
        namespace = item["metadata"]["namespace"]

        phase = status.get("phase", "")
        reason = status.get("reason", "")

        conditions = [
            status.get("containerStatuses", []),
            status.get("initContainerStatuses", [])
        ]

        found_error = False

        if phase in ERROR_STATUSES or reason in ERROR_STATUSES:
            found_error = True
        else:
            for container_list in conditions:
                for container in container_list:
                    waiting = container.get("state", {}).get("waiting")
                    if waiting and waiting.get("reason") in ERROR_STATUSES:
                        found_error = True
                        break

        if found_error:
            print(f"\n🚨 Found problematic pod: {namespace}/{pod_name}")
            print("📄 First 10 lines of logs:")
            print(print_log(namespace, pod_name))

            logs = get_pod_logs(namespace, pod_name)
            description = get_pod_description(namespace, pod_name)
            response = send_to_gemma(logs, description)
            print(f"🔎 Gemma Analysis:\n{response}\n")

if __name__ == "__main__":
    main()
