# Container vs MicroVM Benchmark Tool

A comprehensive benchmarking tool that compares **Docker containers** and **Firecracker microVMs** across two key performance metrics: **cold start time** and **resource usage** (CPU & memory).

## 🎯 Purpose

This tool helps you understand the performance differences between traditional container technology (Docker) and lightweight virtualization (Firecracker) by running side-by-side benchmarks on the same application.

## 🚀 Features

### 1. **Cold Start Benchmark** (Always Runs)
- Measures the time from process start until the application is ready to serve HTTP requests
- Tests both Docker containers and Firecracker microVMs
- Uses a simple Python HTTP server for fair comparison
- Reports which technology starts faster and by how much

### 2. **Resource Usage Benchmark** (Runs when GitHub repo provided)
- Monitors CPU and memory usage for 10 seconds
- Samples metrics every 0.5 seconds
- Reports average CPU%, average memory usage, and peak memory
- Provides overhead comparison between Docker and Firecracker

## 📋 Prerequisites

### System Requirements
- **Operating System**: Linux or WSL2 (Windows Subsystem for Linux)
- **KVM Support**: Required for Firecracker (check with `/dev/kvm`)
- **sudo Access**: Required for network setup and filesystem operations

### Software Dependencies
```bash
# Install Firecracker v1.7.0+
curl -L https://github.com/firecracker-microvm/firecracker/releases/download/v1.7.0/firecracker-v1.7.0-x86_64.tgz -o firecracker.tgz
tar -xzf firecracker.tgz
sudo mv release-v1.7.0-x86_64/firecracker-v1.7.0-x86_64 /usr/local/bin/firecracker
sudo chmod +x /usr/local/bin/firecracker

# Install Python dependencies
pip install docker requests psutil

# Verify Docker is installed and running
docker --version
```

### System Tools Required
- `curl` - for downloading assets
- `e2fsck`, `resize2fs` - for filesystem operations
- `ip`, `sudo` - for network configuration
- `git` - for cloning repositories

## 🎮 Usage

### Basic Usage (Default HTTP Server)
```bash
python main.py
# Press Enter when prompted for GitHub repo link
```

This will:
1. Create a default Python HTTP server in a container
2. Run cold start benchmarks on both Docker and Firecracker
3. Show startup time comparison

### Advanced Usage (Custom Application)
```bash
python main.py
# Enter a GitHub repo URL when prompted
```

This will:
1. Clone your GitHub repository
2. Build Docker image from your Dockerfile
3. Run cold start benchmarks
4. Run resource usage benchmarks (CPU & memory monitoring for 10 seconds)
5. Show comprehensive comparison

### Example Output

```
==================================================
COLD START BENCHMARK
==================================================

=== Testing Docker Container ===
Container started in 2.87 seconds

=== Testing Firecracker microVM ===
Firecracker microVM + HTTP server started in 1.86 seconds

--- Cold Start Results ---
Docker Container:     2.873 seconds
Firecracker microVM:  1.859 seconds
Speed difference:     1.55x faster (Firecracker)

==================================================
RESOURCE USAGE BENCHMARK
==================================================

--- Resource Usage Results ---

Docker Container:
  Average CPU:     5.23%
  Average Memory:  45.67 MB
  Peak Memory:     52.34 MB

Firecracker microVM:
  Average CPU:     1.00%
  Average Memory:  58.75 MB
  Peak Memory:     58.75 MB

Comparison:
  CPU overhead:    +80.9% (Docker vs Firecracker)
  Memory overhead: -22.3% (Docker vs Firecracker)
```

## 🏗️ Architecture

### Docker Container Test
1. **Build**: Creates Docker image from Dockerfile
2. **Startup**: Spawns container and waits for HTTP health check
3. **Monitoring**: Uses `docker stats` to track CPU/memory
4. **Cleanup**: Stops and removes container

### Firecracker microVM Test
1. **Asset Download**: Downloads Linux kernel (~10MB) and Ubuntu rootfs (~50MB) - **cached after first run**
2. **Custom Rootfs**: Creates modified rootfs with:
   - Network configuration script
   - Python HTTP server startup
   - Mounts proc/sys/dev filesystems
3. **VM Configuration**: Creates JSON config with:
   - 1 vCPU, 512MB RAM
   - TAP network interface (172.16.0.2/24)
   - Custom init script as PID 1
4. **Startup**: Launches Firecracker and waits for HTTP health check
5. **Monitoring**: Uses `psutil` to track process CPU/memory
6. **Cleanup**: Terminates VM and removes TAP device

## 📁 Project Structure

```
os_bench/
├── main.py                 # Main benchmark script
├── README.md              # This file
└── ~/.firecracker/        # Cache directory (created automatically)
    ├── vmlinux.bin        # Linux kernel (~10MB)
    └── base_rootfs.ext4   # Ubuntu 18.04 rootfs (~50MB)
```

## 🔧 Key Components

### Functions Overview

#### Docker Functions
- `build_docker_image()` - Builds Docker image from Dockerfile
- `measure_startup_time()` - Measures cold start time for Docker
- `monitor_docker_resources()` - Tracks CPU/memory using `docker stats`
- `run_docker_with_monitoring()` - Runs container and monitors resources

#### Firecracker Functions
- `download_firecracker_assets()` - Downloads and caches kernel/rootfs
- `create_custom_rootfs()` - Creates modified rootfs with HTTP server
- `create_firecracker_config()` - Generates Firecracker VM configuration
- `setup_tap_device()` - Creates TAP network interface
- `measure_firecracker_startup()` - Measures cold start time for Firecracker
- `monitor_firecracker_resources()` - Tracks CPU/memory using psutil
- `run_firecracker_with_monitoring()` - Runs VM and monitors resources

#### Utility Functions
- `clone_repo()` - Clones GitHub repository
- `create_default_dockerfile()` - Creates basic Python HTTP server Dockerfile
- `cleanup_tap_device()` - Removes TAP network interface

## 🔬 How Measurements Work

### Cold Start Time
- **Start**: Timer begins when process/container is spawned
- **End**: Timer stops when HTTP server returns 200 OK response
- **Fairness**: Both tests measure until the same milestone (HTTP ready)

### Resource Monitoring
- **Duration**: 10 seconds of continuous monitoring
- **Sample Rate**: Every 0.5 seconds (20 samples total)
- **Metrics Collected**:
  - CPU usage percentage
  - Memory usage in MB (RSS for Firecracker, container usage for Docker)
  - Peak memory usage

## 🐛 Troubleshooting

### "No KVM available"
```bash
# Check if KVM is available
ls -l /dev/kvm

# Add user to kvm group
sudo usermod -aG kvm $USER
# Log out and log back in
```

### "TAP device creation failed"
```bash
# Ensure you have sudo privileges
sudo ip link show

# Manually cleanup old TAP device
sudo ip link delete tap0
```

### "Docker stats showing 0% CPU"
- This can happen if the container exits quickly (e.g., short-lived scripts)
- Resource monitoring requires long-running applications
- The tool will warn you if the container exits prematurely

### "Firecracker process exited unexpectedly"
```bash
# Check Firecracker version
firecracker --version  # Should be v1.7.0+

# Verify KVM access
test -r /dev/kvm && test -w /dev/kvm && echo "OK" || echo "FAIL"
```

## 🎓 Understanding the Results

### When Firecracker is Faster
- **Cold Start**: Typically 2-3x faster than Docker
- **Reasons**: 
  - Direct kernel boot (no container runtime overhead)
  - Minimal init process
  - Optimized for speed

### When Docker Uses Less Memory
- **Resource Usage**: Docker containers often use less memory
- **Reasons**:
  - Shared kernel with host
  - No guest OS overhead
  - Container-optimized images

### Trade-offs
| Metric | Docker | Firecracker |
|--------|--------|-------------|
| Cold Start | Slower | **Faster** |
| Memory | **Lower** | Higher (includes guest kernel) |
| Isolation | Process-level | **VM-level** |
| Security | Container | **Stronger (VM)** |
| Ecosystem | **Mature** | Growing |

## 🤝 Contributing

Feel free to:
- Report issues
- Suggest improvements
- Add new benchmark metrics
- Test on different systems

## 📝 License

MIT License - feel free to use and modify!

## 🙏 Acknowledgments

- [Firecracker](https://firecracker-microvm.github.io/) - AWS's lightweight virtualization technology
- [Docker](https://www.docker.com/) - Industry-standard container platform
- Ubuntu 18.04 rootfs from AWS S3 quickstart guide

---

**Made with ❤️ for benchmarking containers vs microVMs**
