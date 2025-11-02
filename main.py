import os
import tempfile
import time
import subprocess
import docker
import requests
import json
import psutil

DEFAULT_DOCKERFILE = """
FROM python:3.9-slim
WORKDIR /app
COPY . /app
RUN pip install flask
EXPOSE 8080
CMD ["python3", "-m", "http.server", "8080"]
"""

def clone_repo(repo_url, workdir):
    print(f"Cloning {repo_url}...")
    subprocess.run(["git", "clone", repo_url, workdir], check=True)

def create_default_dockerfile(workdir):
    dockerfile_path = os.path.join(workdir, "Dockerfile")
    with open(dockerfile_path, "w") as f:
        f.write(DEFAULT_DOCKERFILE)
    print("Default Dockerfile created at", dockerfile_path)
    return dockerfile_path

def build_docker_image(client, path, tag="test-image:latest"):
    print("Building Docker image...")
    image, logs = client.images.build(path=path, tag=tag)
    for line in logs:
        if "stream" in line:
            print(line["stream"].strip())
    print("Image built successfully:", image.tags)
    return image

def measure_startup_time(client, image_tag):
    print("Spawning container and measuring startup time...")
    start = time.time()
    container = client.containers.run(
        image_tag,
        detach=True,
        ports={"8080/tcp": 8080}
    )

    # Wait for health check or successful response
    health_url = "http://localhost:8080"
    for _ in range(30):
        try:
            r = requests.get(health_url, timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    end = time.time()
    startup_time = end - start
    print(f"Container started in {startup_time:.2f} seconds")

    # Cleanup
    container.stop()
    container.remove()
    return startup_time

def download_firecracker_assets():
    """Download kernel and base rootfs for Firecracker (cached permanently)"""
    # Use permanent cache directory in home folder
    home_dir = os.path.expanduser("~")
    cache_dir = os.path.join(home_dir, ".firecracker")
    
    # Create cache directory if it doesn't exist
    os.makedirs(cache_dir, exist_ok=True)
    
    kernel_url = "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin"
    rootfs_url = "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/rootfs/bionic.rootfs.ext4" # rn locally I'm using custom build rootfs with python baked in, so... uk...
    
    # Use cached paths
    kernel_path = os.path.join(cache_dir, "vmlinux.bin")
    base_rootfs_path = os.path.join(cache_dir, "base_rootfs.ext4")
    
    # Download kernel only if not cached
    if not os.path.exists(kernel_path):
        print("Downloading kernel (one-time download)...")
        subprocess.run(["curl", "-fsSL", "-o", kernel_path, kernel_url], check=True)
        print(f"Kernel cached at: {kernel_path}")
    else:
        print(f"Using cached kernel from: {kernel_path}")
    
    # Download rootfs only if not cached
    if not os.path.exists(base_rootfs_path):
        print("Downloading base rootfs (one-time download, ~50MB)...")
        subprocess.run(["curl", "-fsSL", "-o", base_rootfs_path, rootfs_url], check=True)
        print(f"Base rootfs cached at: {base_rootfs_path}")
    else:
        print(f"Using cached base rootfs from: {base_rootfs_path}")
    
    return kernel_path, base_rootfs_path

def create_custom_rootfs(base_rootfs_path, workdir):
    """Create a custom rootfs with the HTTP server startup script"""
    rootfs_path = os.path.join(workdir, "custom_rootfs.ext4")
    
    # Copy base rootfs
    print("Creating custom rootfs with HTTP server...")
    subprocess.run(["cp", base_rootfs_path, rootfs_path], check=True)
    
    # Expand the rootfs to add space for our application (expand by 100MB)
    # First, add 100MB to the file
    subprocess.run(["truncate", "-s", "+100M", rootfs_path], check=True)
    
    # Fix filesystem errors
    subprocess.run(["e2fsck", "-f", "-y", rootfs_path], 
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    
    # Resize filesystem to use all available space
    subprocess.run(["resize2fs", rootfs_path], check=True)
    
    # Mount the rootfs and add startup script
    mount_point = os.path.join(workdir, "rootfs_mount")
    os.makedirs(mount_point, exist_ok=True)
    
    try:
        # Mount
        subprocess.run(["sudo", "mount", "-o", "loop", rootfs_path, mount_point], check=True)
        
        # Create startup script that runs HTTP server
        startup_script = """#!/bin/bash
# Mount necessary filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

# Configure network
ip addr add 172.16.0.2/24 dev eth0
ip link set eth0 up
ip route add default via 172.16.0.1

# Start HTTP server
cd /root
python3 -m http.server 8080 &

# Keep system running
while true; do sleep 1000; done
"""
        
        # Write script using sudo
        script_path = os.path.join(mount_point, "root", "startup.sh")
        subprocess.run(["sudo", "tee", script_path], 
                      input=startup_script.encode(), 
                      stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["sudo", "chmod", "+x", script_path], check=True)
        
    finally:
        # Unmount
        subprocess.run(["sudo", "umount", mount_point], check=False)
    
    return rootfs_path

def create_firecracker_config(workdir, kernel_path, rootfs_path):
    """Create Firecracker VM configuration"""
    config = {
        "boot-source": {
            "kernel_image_path": kernel_path,
            "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/root/startup.sh"
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": rootfs_path,
                "is_root_device": True,
                "is_read_only": False
            }
        ],
        "machine-config": {
            "vcpu_count": 1,
            "mem_size_mib": 512,
            "smt": False
        },
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "guest_mac": "AA:FC:00:00:00:01",
                "host_dev_name": "tap0"
            }
        ]
    }
    
    config_path = os.path.join(workdir, "vm_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    
    return config_path

def setup_tap_device():
    """Setup TAP network device for Firecracker"""
    try:
        # Check if tap0 already exists
        result = subprocess.run(["ip", "link", "show", "tap0"], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            print("TAP device already exists")
            return True
        
        # Create tap device
        subprocess.run(["sudo", "ip", "tuntap", "add", "tap0", "mode", "tap"], check=True)
        subprocess.run(["sudo", "ip", "addr", "add", "172.16.0.1/24", "dev", "tap0"], check=True)
        subprocess.run(["sudo", "ip", "link", "set", "tap0", "up"], check=True)
        print("TAP device created successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not setup TAP device: {e}")
        return False

def cleanup_tap_device():
    """Cleanup TAP network device"""
    try:
        subprocess.run(["sudo", "ip", "link", "delete", "tap0"], 
                      stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass

def measure_firecracker_startup(workdir):
    """Measure Firecracker microVM startup time until HTTP server is responding"""
    print("\n=== Testing Firecracker microVM ===")
    
    # Download assets if needed
    kernel_path, base_rootfs_path = download_firecracker_assets()
    
    # Create custom rootfs with HTTP server
    rootfs_path = create_custom_rootfs(base_rootfs_path, workdir)
    
    # Create config
    config_path = create_firecracker_config(workdir, kernel_path, rootfs_path)
    
    # Setup network
    tap_available = setup_tap_device()
    if not tap_available:
        print("Warning: TAP device not available, skipping Firecracker test")
        return None
    
    # Create socket path
    socket_path = os.path.join(workdir, "firecracker.socket")
    
    print("Starting Firecracker microVM and waiting for HTTP server...")
    start = time.time()
    
    # Start Firecracker in background
    firecracker_process = subprocess.Popen(
        ["firecracker", "--api-sock", socket_path, "--config-file", config_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE
    )
    
    # Wait for HTTP server to respond (like Docker test)
    health_url = "http://172.16.0.2:8080"
    startup_time = None
    
    for i in range(100):  # 10 seconds max
        if firecracker_process.poll() is not None:
            print("Firecracker process exited unexpectedly")
            stdout, stderr = firecracker_process.communicate()
            print("STDOUT:", stdout.decode())
            print("STDERR:", stderr.decode())
            break
        
        try:
            # Try to connect to HTTP server inside VM
            r = requests.get(health_url, timeout=0.5)
            if r.status_code == 200:
                startup_time = time.time() - start
                print(f"Firecracker microVM + HTTP server started in {startup_time:.3f} seconds")
                break
        except Exception:
            pass
        
        time.sleep(0.1)
    
    if startup_time is None:
        print("Warning: HTTP server did not respond in time")
        startup_time = time.time() - start
    
    # Cleanup
    time.sleep(0.5)  # Let it run briefly
    firecracker_process.terminate()
    try:
        firecracker_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        firecracker_process.kill()
    
    if os.path.exists(socket_path):
        os.remove(socket_path)
    
    return startup_time

def monitor_docker_resources(container, duration=10):
    """Monitor CPU and memory usage of a Docker container"""
    print(f"Monitoring Docker container resources for {duration} seconds...")
    
    cpu_samples = []
    memory_samples = []
    container_id = container.id[:12]  # Use short ID
    
    start_time = time.time()
    sample_count = 0
    
    while time.time() - start_time < duration:
        try:
            # Use docker stats command for better compatibility
            result = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}},{{.MemUsage}}", container_id],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                sample_count += 1
                
                # Format: "1.23%,45.67MiB / 512MiB"
                parts = output.split(',')
                
                # Parse CPU percentage
                if len(parts) > 0:
                    cpu_str = parts[0].replace('%', '').strip()
                    try:
                        cpu_percent = float(cpu_str)
                        cpu_samples.append(cpu_percent)
                    except ValueError:
                        pass
                
                # Parse memory usage
                if len(parts) > 1:
                    mem_str = parts[1].split('/')[0].strip()
                    # Convert to MB
                    try:
                        if 'GiB' in mem_str:
                            memory_mb = float(mem_str.replace('GiB', '').strip()) * 1024
                        elif 'MiB' in mem_str:
                            memory_mb = float(mem_str.replace('MiB', '').strip())
                        elif 'KiB' in mem_str:
                            memory_mb = float(mem_str.replace('KiB', '').strip()) / 1024
                        else:
                            # Try without unit suffix
                            memory_mb = float(mem_str.replace('B', '').strip()) / (1024 * 1024)
                        
                        memory_samples.append(memory_mb)
                    except (ValueError, AttributeError):
                        pass
            
        except (subprocess.TimeoutExpired, Exception) as e:
            pass  # Silent fail, keep trying
        
        time.sleep(0.5)
    
    print(f"Collected {sample_count} samples ({len(cpu_samples)} CPU, {len(memory_samples)} memory)")
    
    avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0
    avg_memory = sum(memory_samples) / len(memory_samples) if memory_samples else 0
    max_memory = max(memory_samples) if memory_samples else 0
    
    return {
        'avg_cpu': avg_cpu,
        'avg_memory': avg_memory,
        'max_memory': max_memory
    }

def monitor_firecracker_resources(process_pid, duration=10):
    """Monitor CPU and memory usage of Firecracker process"""
    print(f"Monitoring Firecracker microVM resources for {duration} seconds...")
    
    cpu_samples = []
    memory_samples = []
    
    try:
        process = psutil.Process(process_pid)
        start_time = time.time()
        
        while time.time() - start_time < duration:
            try:
                # Get CPU percentage (interval for accurate measurement)
                cpu_percent = process.cpu_percent(interval=0.5)
                
                # Get memory usage in MB
                memory_info = process.memory_info()
                memory_mb = memory_info.rss / (1024 * 1024)
                
                cpu_samples.append(cpu_percent)
                memory_samples.append(memory_mb)
                
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
        
        avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0
        avg_memory = sum(memory_samples) / len(memory_samples) if memory_samples else 0
        max_memory = max(memory_samples) if memory_samples else 0
        
        return {
            'avg_cpu': avg_cpu,
            'avg_memory': avg_memory,
            'max_memory': max_memory
        }
    except Exception as e:
        print(f"Error monitoring Firecracker: {e}")
        return {'avg_cpu': 0, 'avg_memory': 0, 'max_memory': 0}

def run_docker_with_monitoring(client, image_tag, duration=10):
    """Run Docker container and monitor its resources"""
    print("\nSpawning Docker container for resource monitoring...")
    
    container = client.containers.run(
        image_tag,
        detach=True,
        ports={"8080/tcp": 8080}
    )
    
    # Give container a moment to start
    time.sleep(1)
    
    # Check if container is still running
    container.reload()
    if container.status != "running":
        print(f"Warning: Container exited quickly with status: {container.status}")
        print("This might be a short-lived script rather than a long-running service.")
        container.remove()
        return {'avg_cpu': 0, 'avg_memory': 0, 'max_memory': 0}
    
    print("Container is running, starting monitoring...")
    
    # Monitor resources
    stats = monitor_docker_resources(container, duration)
    
    # Cleanup
    try:
        container.stop(timeout=2)
    except:
        pass
    container.remove()
    
    return stats

def run_firecracker_with_monitoring(workdir, duration=10):
    """Run Firecracker microVM and monitor its resources"""
    print("\nSpawning Firecracker microVM for resource monitoring...")
    
    # Download assets if needed
    kernel_path, base_rootfs_path = download_firecracker_assets()
    
    # Create custom rootfs with HTTP server
    rootfs_path = create_custom_rootfs(base_rootfs_path, workdir)
    
    # Create config
    config_path = create_firecracker_config(workdir, kernel_path, rootfs_path)
    
    # Setup network
    tap_available = setup_tap_device()
    if not tap_available:
        print("Warning: TAP device not available, skipping Firecracker test")
        return None
    
    # Create socket path
    socket_path = os.path.join(workdir, "firecracker.socket")
    
    # Start Firecracker in background
    firecracker_process = subprocess.Popen(
        ["firecracker", "--api-sock", socket_path, "--config-file", config_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE
    )
    
    # Wait for HTTP server to respond
    health_url = "http://172.16.0.2:8080"
    for i in range(100):
        if firecracker_process.poll() is not None:
            print("Firecracker process exited unexpectedly")
            return None
        
        try:
            r = requests.get(health_url, timeout=0.5)
            if r.status_code == 200:
                print("Firecracker microVM is ready, starting monitoring...")
                break
        except Exception:
            pass
        
        time.sleep(0.1)
    
    # Monitor resources
    stats = monitor_firecracker_resources(firecracker_process.pid, duration)
    
    # Cleanup
    firecracker_process.terminate()
    try:
        firecracker_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        firecracker_process.kill()
    
    if os.path.exists(socket_path):
        os.remove(socket_path)
    
    return stats

def main():
    repo_url = input("Enter GitHub repo link (leave empty to use default): ").strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Prepare the application
        if repo_url:
            clone_repo(repo_url, tmpdir)
        else:
            create_default_dockerfile(tmpdir)

        client = docker.from_env()
        build_docker_image(client, tmpdir)
        
        # Run cold start tests (always)
        print("\n" + "="*50)
        print("COLD START BENCHMARK")
        print("="*50)
        
        print("\n=== Testing Docker Container ===")
        docker_time = measure_startup_time(client, "test-image:latest")
        
        print("\n=== Testing Firecracker microVM ===")
        firecracker_time = measure_firecracker_startup(tmpdir)
        
        # Display cold start results
        print("\n--- Cold Start Results ---")
        print(f"Docker Container:     {docker_time:.3f} seconds")
        if firecracker_time:
            print(f"Firecracker microVM:  {firecracker_time:.3f} seconds")
            speedup = docker_time / firecracker_time
            winner = "Firecracker" if speedup > 1 else "Docker"
            print(f"Speed difference:     {abs(speedup):.2f}x faster ({winner})")
        else:
            print(f"Firecracker microVM:  FAILED")
        
        # If user provided a repo, also run resource monitoring
        if repo_url:
            print("\n" + "="*50)
            print("RESOURCE USAGE BENCHMARK")
            print("="*50)
            
            # Monitor Docker
            print("\n=== Docker Container Resource Monitoring ===")
            docker_stats = run_docker_with_monitoring(client, "test-image:latest", duration=10)
            
            # Monitor Firecracker
            print("\n=== Firecracker microVM Resource Monitoring ===")
            firecracker_stats = run_firecracker_with_monitoring(tmpdir, duration=10)
            
            # Display resource results
            print("\n--- Resource Usage Results ---")
            print("\nDocker Container:")
            print(f"  Average CPU:     {docker_stats['avg_cpu']:.2f}%")
            print(f"  Average Memory:  {docker_stats['avg_memory']:.2f} MB")
            print(f"  Peak Memory:     {docker_stats['max_memory']:.2f} MB")
            
            if firecracker_stats:
                print("\nFirecracker microVM:")
                print(f"  Average CPU:     {firecracker_stats['avg_cpu']:.2f}%")
                print(f"  Average Memory:  {firecracker_stats['avg_memory']:.2f} MB")
                print(f"  Peak Memory:     {firecracker_stats['max_memory']:.2f} MB")
                
                # Compare
                cpu_diff = ((docker_stats['avg_cpu'] - firecracker_stats['avg_cpu']) / docker_stats['avg_cpu'] * 100) if docker_stats['avg_cpu'] > 0 else 0
                mem_diff = ((docker_stats['avg_memory'] - firecracker_stats['avg_memory']) / docker_stats['avg_memory'] * 100) if docker_stats['avg_memory'] > 0 else 0
                
                print("\nComparison:")
                print(f"  CPU overhead:    {cpu_diff:+.1f}% (Docker vs Firecracker)")
                print(f"  Memory overhead: {mem_diff:+.1f}% (Docker vs Firecracker)")
        
        # Cleanup
        cleanup_tap_device()
        
        print("\n" + "="*50)

if __name__ == "__main__":
    main()
