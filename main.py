import subprocess
import json
import requests
import re

def is_pod_unhealthy(pod):
    status = pod.get("status", {})
    phase = status.get("phase", "")

    if phase in ["Failed", "Unknown"]:
        return True

    conditions = status.get("conditions", [])
    if phase == "Pending" and conditions:
        for condition in conditions:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return True

    for cs in status.get("containerStatuses", []) or []:
        if not cs.get("ready", True):
            state = cs.get("state", {})
            waiting = state.get("waiting")
            if waiting and waiting.get("reason") in [
                "CrashLoopBackOff", "ErrImagePull", "ImagePullBackOff", "RunContainerError"
            ]:
                return True
            if state.get("terminated"):
                return True

    return False

def get_failed_pods():
    result = subprocess.run(
        ["kubectl", "get", "pods", "--all-namespaces", "-o", "json"],
        capture_output=True, text=True
    )
    pods = json.loads(result.stdout)
    failed_pods = []

    for item in pods['items']:
        namespace = item['metadata']['namespace']
        pod_name = item['metadata']['name']

        if is_pod_unhealthy(item):
            failed_pods.append({
                'name': pod_name,
                'namespace': namespace
            })

    return failed_pods

def collect_info(pod_name, namespace):
    logs = subprocess.getoutput(f"kubectl logs {pod_name} -n {namespace}")
    desc = subprocess.getoutput(f"kubectl describe pod {pod_name} -n {namespace}")
    return f"LOGS:\n{logs}\n\nPOD DESCRIPTION:\n{desc}"

def query_gemma(info, pod_name, namespace):
    url = "http://localhost:11434/api/generate"

    prompt = f"""
You are an expert Kubernetes troubleshooter. Below are the logs and pod description for a failed pod:

Logs:
{info}

Analyze the information and answer the following:

1. What is the most likely cause of failure?
2. Suggest the best action(s) to fix the problem. Possible actions include:
   - restart
   - revert_image
   - increase_resources
   - check_config

Answer ONLY in the following JSON format:

{{
  "cause": "<brief cause>",
  "action": "<one of: restart | revert_image | increase_resources | check_config>",
  "pod": "{pod_name}",
  "namespace": "{namespace}",
  "details": "<optional: detailed notes>"
}}
"""

    payload = {
        "model": "gemma:2b",
        "prompt": prompt.strip(),
        "stream": False
    }

    response = requests.post(url, json=payload)
    res_json = response.json()

    raw_response = res_json.get("response", "").strip()
    match = re.search(r'{.*}', raw_response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as e:
            raise Exception(f"❌ Failed to parse JSON: {e}")
    else:
        raise Exception("❌ No valid JSON object found in model response.")

def take_action(action_json):
    action = action_json["action"]
    pod = action_json["pod"]
    ns = action_json["namespace"]
    details = action_json.get("details", "")

    print(f"📌 Cause: {action_json.get('cause')}")
    print(f"📋 Recommended Action: {action}")
    if details:
        print(f"📓 Details: {details}")

    if action == "restart":
        result = subprocess.run(
            ["kubectl", "get", "pod", pod, "-n", ns, "-o", "json"],
            capture_output=True, text=True
        )
        pod_info = json.loads(result.stdout)
        owners = pod_info['metadata'].get('ownerReferences', [])

        for owner in owners:
            if owner['kind'] == 'ReplicaSet':
                rs_name = owner['name']
                deploy_name = "-".join(rs_name.split("-")[:-1])
                print(f"🔁 Restarting deployment: {deploy_name}")
                subprocess.run(["kubectl", "rollout", "restart", "deployment", deploy_name, "-n", ns])
                return

        print("❌ Could not find deployment. Deleting pod instead.")
        subprocess.run(["kubectl", "delete", "pod", pod, "-n", ns])

    elif action == "revert_image":
        print("🔙 Action 'revert_image' not automated. Consider using 'kubectl rollout undo'.")
    elif action == "increase_resources":
        print("📈 Action 'increase_resources': Adjust resources in deployment YAML.")
    elif action == "check_config":
        print("🔍 Action 'check_config': Check config maps, environment vars, or secrets.")
    else:
        print(f"⚠️ Unknown action '{action}'. No operation performed.")

def main():
    failed_pods = get_failed_pods()
    for pod in failed_pods:
        print(f"\n⚠️ Detected failed pod: {pod['name']} in namespace: {pod['namespace']}")
        info = collect_info(pod['name'], pod['namespace'])
        try:
            action_json = query_gemma(info, pod['name'], pod['namespace'])
            take_action(action_json)
        except Exception as e:
            print(f"🚫 Error processing pod {pod['name']}: {e}")

if __name__ == "__main__":
    main()
