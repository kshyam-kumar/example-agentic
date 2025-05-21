import subprocess
import json
import time
import requests
import logging
import os
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("pod_monitor.log"),
        logging.StreamHandler()
    ]
)

# Configuration
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gemma:2b"
SCAN_INTERVAL = 60  # seconds
LOG_DIR = "pod_logs"

# Create logs directory if it doesn't exist
os.makedirs(LOG_DIR, exist_ok=True)

def run_kubectl_command(command):
    """Run a kubectl command and return the output"""
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
    """Get all pods in the cluster"""
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
    """Check if a pod is unhealthy"""
    if "status" not in pod:
        return False
    
    status = pod["status"]
    phase = status.get("phase", "")
    
    # Check if pod is in failed or pending state
    if phase in ["Failed", "Unknown"]:
        return True
    
    # Check for pods stuck in pending state
    if phase == "Pending" and "conditions" in status:
        for condition in status["conditions"]:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return True
    
    # Check for containers that are not ready
    if phase == "Running":
        container_statuses = status.get("containerStatuses", [])
        for container in container_statuses:
            if not container.get("ready", True):
                if "state" in container and "waiting" in container["state"]:
                    return True
                if "state" in container and "terminated" in container["state"]:
                    terminated_info = container["state"]["terminated"]
                    if terminated_info.get("exitCode", 0) != 0:
                        return True
    
    return False

def get_pod_logs(namespace, pod_name, container=None):
    """Get logs for a specific pod"""
    command = f"logs -n {namespace} {pod_name}"
    if container:
        command += f" -c {container}"
    return run_kubectl_command(command)

def analyze_with_ollama(logs, pod_info):
    """Send logs to Ollama for analysis"""
    try:
        prompt = f"""
You are a Kubernetes troubleshooting expert. Analyze the following logs from a problematic pod and identify the most likely cause of the issue.
Also suggest possible solutions.

Pod Name: {pod_info['name']}
Namespace: {pod_info['namespace']}
Pod Status: {pod_info['status']}

Logs:
{logs[:4000]}  # Limiting log size to prevent token overflow

What is the likely error and how can it be fixed?
"""
        
        response = requests.post(
            OLLAMA_API_URL,
            json={
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False
            },
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json().get("response", "No analysis provided")
        else:
            logging.error(f"Ollama API error: {response.status_code}, {response.text}")
            return f"Failed to analyze logs: API returned status {response.status_code}"
    
    except Exception as e:
        logging.error(f"Error during Ollama analysis: {e}")
        return f"Failed to analyze logs: {str(e)}"

def save_analysis(pod_info, logs, analysis):
    """Save the logs and analysis to files"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename_base = f"{pod_info['namespace']}_{pod_info['name']}_{timestamp}"
    
    # Save logs
    logs_path = os.path.join(LOG_DIR, f"{filename_base}_logs.txt")
    with open(logs_path, "w", encoding="utf-8") as f:
        f.write(logs)
    
    # Save analysis
    analysis_path = os.path.join(LOG_DIR, f"{filename_base}_analysis.txt")
    with open(analysis_path, "w", encoding="utf-8") as f:
        f.write(f"Pod Name: {pod_info['name']}\n")
        f.write(f"Namespace: {pod_info['namespace']}\n")
        f.write(f"Status: {pod_info['status']}\n\n")
        f.write("ANALYSIS:\n")
        f.write(analysis)
    
    return logs_path, analysis_path

def main_loop():
    """Main monitoring loop"""
    logging.info("Starting Kubernetes pod monitoring with Ollama integration")
    logging.info(f"Using AI model: {MODEL_NAME}")
    
    # Test Ollama connection
    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={"model": MODEL_NAME, "prompt": "Test", "stream": False},
            timeout=10
        )
        if response.status_code == 200:
            logging.info("Successfully connected to Ollama")
        else:
            logging.warning(f"Ollama connection test failed: {response.status_code}")
    except Exception as e:
        logging.warning(f"Couldn't connect to Ollama: {e}")
    
    # Main monitoring loop
    while True:
        try:
            logging.info("Scanning for unhealthy pods...")
            pods = get_pods()
            unhealthy_pods_found = False
            
            for pod in pods:
                metadata = pod.get("metadata", {})
                status = pod.get("status", {})
                
                pod_name = metadata.get("name", "unknown")
                namespace = metadata.get("namespace", "default")
                
                if is_pod_unhealthy(pod):
                    unhealthy_pods_found = True
                    logging.info(f"Found unhealthy pod: {namespace}/{pod_name}")
                    
                    # Get pod logs
                    logs = get_pod_logs(namespace, pod_name)
                    if not logs:
                        logging.warning(f"No logs available for pod {namespace}/{pod_name}")
                        continue
                    
                    # Get container status for more context
                    container_statuses = status.get("containerStatuses", [])
                    status_details = "Unknown"
                    for container in container_statuses:
                        if "state" in container:
                            if "waiting" in container["state"]:
                                reason = container["state"]["waiting"].get("reason", "")
                                message = container["state"]["waiting"].get("message", "")
                                status_details = f"Waiting: {reason} - {message}"
                            elif "terminated" in container["state"]:
                                reason = container["state"]["terminated"].get("reason", "")
                                exit_code = container["state"]["terminated"].get("exitCode", "")
                                status_details = f"Terminated: {reason} (Exit code: {exit_code})"
                    
                    pod_info = {
                        "name": pod_name,
                        "namespace": namespace,
                        "status": status_details
                    }
                    
                    # Analyze logs with Ollama
                    logging.info(f"Analyzing logs with Ollama for pod {namespace}/{pod_name}")
                    analysis = analyze_with_ollama(logs, pod_info)
                    
                    # Save results
                    logs_path, analysis_path = save_analysis(pod_info, logs, analysis)
                    logging.info(f"Analysis complete. Logs saved to {logs_path}")
                    logging.info(f"Analysis saved to {analysis_path}")
                    
                    # Print analysis summary
                    print("\n" + "="*80)
                    print(f"ISSUE DETECTED: Pod {namespace}/{pod_name}")
                    print(f"Status: {status_details}")
                    print("-"*80)
                    print("AI ANALYSIS:")
                    print(analysis[:500] + "..." if len(analysis) > 500 else analysis)
                    print("="*80 + "\n")
            
            if not unhealthy_pods_found:
                logging.info("No unhealthy pods found in this scan")
            
            logging.info(f"Sleeping for {SCAN_INTERVAL} seconds before next scan...")
            time.sleep(SCAN_INTERVAL)
            
        except KeyboardInterrupt:
            logging.info("Monitoring stopped by user")
            break
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            logging.info("Will retry in 60 seconds...")
            time.sleep(60)

if __name__ == "__main__":
    main_loop()