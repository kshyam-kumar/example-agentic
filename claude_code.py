import subprocess
import json
import time
import requests
import logging
import sys
from datetime import datetime

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Configuration
OLLAMA_API_URL = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT = 60
MODEL_NAME = "gemma:2b"
SCAN_INTERVAL = 60
OFFLINE_MODE = False

def run_kubectl_command(command):
    try:
        result = subprocess.run(
            f"kubectl {command}",
            shell=True,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logging.error(f"Command failed: {result.stderr}")
            return None
        return result.stdout
    except Exception as e:
        logging.error(f"Error executing kubectl command: {e}")
        return None

def get_pods():
    output = run_kubectl_command("get pods --all-namespaces -o json")
    if not output:
        return []
    try:
        pods_data = json.loads(output)
        return pods_data.get("items", [])
    except json.JSONDecodeError:
        logging.error("Failed to parse pod JSON data")
        return []

def is_pod_unhealthy(pod):
    status = pod.get("status", {})
    phase = status.get("phase", "")

    if phase in ["Failed", "Unknown"]:
        return True

    if phase == "Pending" and "conditions" in status:
        for condition in status["conditions"]:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return True

    if phase == "Running":
        for container in status.get("containerStatuses", []):
            if not container.get("ready", True):
                state = container.get("state", {})
                if "waiting" in state or "terminated" in state:
                    return True
    return False

def get_pod_logs(namespace, pod_name, container=None):
    command = f"logs -n {namespace} {pod_name}"
    if container:
        command += f" -c {container}"
    return run_kubectl_command(command)

def get_pod_description(namespace, pod_name):
    return run_kubectl_command(f"describe pod -n {namespace} {pod_name}")

def check_ollama_status():
    global OFFLINE_MODE
    if OFFLINE_MODE:
        return False
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code != 200:
            return False
        models = response.json().get("models", [])
        if not any(m.get("name") == MODEL_NAME for m in models):
            logging.warning(f"Model {MODEL_NAME} not loaded. Run 'ollama run {MODEL_NAME}'")
            return False
        test = requests.post(
            OLLAMA_API_URL,
            json={"model": MODEL_NAME, "prompt": "hello", "stream": False},
            timeout=10
        )
        return test.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Ollama connection issue: {e}")
        return False

def analyze_with_ollama(logs, pod_description, pod_info):
    if OFFLINE_MODE:
        return "AI analysis disabled (offline mode). Please review logs and pod description manually."

    try:
        logging.info(f"Analyzing pod {pod_info['namespace']}/{pod_info['name']} with Ollama")

        pod_desc_excerpt = pod_description[:2000] if pod_description else "No description available"
        logs_excerpt = logs[:2000] if logs else "No logs available"

        prompt = f"""
You are analyzing a Kubernetes pod that is experiencing an error.

Your task is to:
1. Explain what steps occurred **before the error**.
2. Identify **what the error is**.
3. Determine **what caused the error**, based on logs and pod description.
4. Suggest practical steps to resolve the issue.

--- Pod Name ---
{pod_info['name']}

--- Namespace ---
{pod_info['namespace']}

--- Pod Status ---
{pod_info['status']}

--- Logs ---
{logs_excerpt}

--- Pod Description ---
{pod_desc_excerpt}
"""

        response = requests.post(
            OLLAMA_API_URL,
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT
        )

        if response.status_code == 200:
            return response.json().get("response", "No analysis provided")
        else:
            return f"Ollama error: {response.status_code} - {response.text}"

    except requests.exceptions.Timeout:
        return "Ollama analysis timed out."
    except requests.exceptions.ConnectionError:
        return "Could not connect to Ollama API."
    except Exception as e:
        return f"Unexpected error during analysis: {e}"

def main_loop():
    global OFFLINE_MODE
    logging.info("Starting pod monitor with AI integration (Ollama)")
    logging.info(f"Model: {MODEL_NAME}")

    if not check_ollama_status():
        if input("Ollama not ready. Type 'offline' to continue without AI: ").lower() == "offline":
            OFFLINE_MODE = True
            logging.info("Running in OFFLINE mode.")

    while True:
        try:
            logging.info("Scanning for unhealthy pods...")
            pods = get_pods()
            unhealthy_found = False

            for pod in pods:
                metadata = pod.get("metadata", {})
                status = pod.get("status", {})
                pod_name = metadata.get("name", "unknown")
                namespace = metadata.get("namespace", "default")

                if is_pod_unhealthy(pod):
                    unhealthy_found = True
                    logging.info(f"Found unhealthy pod: {namespace}/{pod_name}")

                    logs = get_pod_logs(namespace, pod_name) or "No logs available"
                    pod_description = get_pod_description(namespace, pod_name) or "No description available"

                    container_statuses = status.get("containerStatuses", [])
                    status_details = "Unknown"
                    for container in container_statuses:
                        state = container.get("state", {})
                        if "waiting" in state:
                            reason = state["waiting"].get("reason", "")
                            message = state["waiting"].get("message", "")
                            status_details = f"Waiting: {reason} - {message}"
                        elif "terminated" in state:
                            reason = state["terminated"].get("reason", "")
                            exit_code = state["terminated"].get("exitCode", "")
                            status_details = f"Terminated: {reason} (Exit code: {exit_code})"

                    pod_info = {
                        "name": pod_name,
                        "namespace": namespace,
                        "status": status_details
                    }

                    if not OFFLINE_MODE and check_ollama_status():
                        analysis = analyze_with_ollama(logs, pod_description, pod_info)
                    else:
                        analysis = "AI analysis not available. Manual review required."

                    print("\n" + "="*80)
                    print(f"ISSUE DETECTED: Pod {namespace}/{pod_name}")
                    print(f"Status: {status_details}")
                    print("-"*80)
                    print("POD LOGS:")
                    print(logs[:500] + "..." if len(logs) > 500 else logs)
                    print("-"*80)
                    print("AI ANALYSIS:")
                    print(analysis)
                    print("="*80 + "\n")

            if not unhealthy_found:
                logging.info("No unhealthy pods found.")

            logging.info(f"Sleeping for {SCAN_INTERVAL} seconds...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            logging.info("Monitoring stopped by user.")
            break
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main_loop()
