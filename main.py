import os
import tempfile
import time
import subprocess
import docker
import requests
import json
import socket

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
    rootfs_url = "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/rootfs/bionic.rootfs.ext4"
    
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

def main():
    repo_url = input("Enter GitHub repo link (leave empty to use default): ").strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Docker benchmark
        print("\n=== Testing Docker Container ===")
        if repo_url:
            clone_repo(repo_url, tmpdir)
        else:
            create_default_dockerfile(tmpdir)

        client = docker.from_env()
        build_docker_image(client, tmpdir)
        docker_time = measure_startup_time(client, "test-image:latest")
        
        # Firecracker benchmark
        firecracker_time = measure_firecracker_startup(tmpdir)
        
        # Cleanup
        cleanup_tap_device()
        
        # Results
        print("\n" + "="*50)
        print("BENCHMARK RESULTS")
        print("="*50)
        print(f"Docker Container:     {docker_time:.3f} seconds")
        if firecracker_time:
            print(f"Firecracker microVM:  {firecracker_time:.3f} seconds")
            speedup = docker_time / firecracker_time
            winner = "Firecracker" if speedup > 1 else "Docker"
            print(f"Speed difference:     {abs(speedup):.2f}x faster ({winner})")
        else:
            print(f"Firecracker microVM:  FAILED (check TAP device setup)")
        print("="*50)

if __name__ == "__main__":
    main()
