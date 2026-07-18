# Large Scale Dedup

A low-memory Python solution designed to process a large text file (`input.txt`) and remove all duplicate lines, producing a unique output file (`output.txt`).

The solution is strictly designed to run on a single machine limited to **1 CPU and 100 MB of RAM**

## Optional input argument:
SAFETY_FACTOR_FOR_OVERHEAD
- **Valid values:** `0 < value <= 1.0` (Default: `0.5`).

Python objects consume more RAM than their raw text size due to internal language overhead.
The `SAFETY_FACTOR_FOR_OVERHEAD` determines what percentage of the allowed 100 MB RAM is safely allocated for raw text strings before flushing chunks to the disk.
---

# How to run:

First, open your terminal and navigate into the project directory:
```bash
cd targil-1
```
Ensure your `input.txt` file is placed directly inside the `targil-1` folder.

## Option 1: Running Globally (Without Docker)

Ensure you have `uv` installed on your machine.

### 1. Run with Safe Default (Safety Factor = 0.5)
```bash
uv run main.py
```

### 2. Run with Custom Safety Overhead Factor (e.g., 1.0 - Theoretical Max)
```bash
uv run main.py 1.0
```

---

## Option 2: Running with Docker

Docker allows you to physically cap the hardware limitations (`--memory="100m"` and `--cpus="1"`) to test the solution under real resource constraints.

### 1. Build the Docker Image
```bash
docker build -t large-scale-dedup .
```

### 2. Run in Safe Mode (Default Safety Factor = 0.5)
```bash
docker run --memory="100m" --cpus="1" large-scale-dedup
```

### 3. Run with safety overhead param (0-1.0)
```bash
docker run --memory="100m" --cpus="1" large-scale-dedup 0.7
```
