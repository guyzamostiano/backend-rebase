# Large Scale Primality Test

Multi-processed Python solution designed to process a large text file (e.g., `input.txt`) and count the number of prime numbers it contains, utilizing all available CPU cores while staying within a **500 MB RAM** limit.

## Features:
- **Parallel Processing:** Uses `ProcessPoolExecutor` to process the file chunks in parallel.
- **Memory Efficient:** Processes data in streaming chunks to maintain a strict 500 MB RAM footprint.
- **Caching:** Uses `functools.lru_cache` to avoid redundant calculations for duplicate numbers (subprocesses are cached).

---

# How to run:

First, open your terminal and navigate into the project directory:
```bash
cd targil-2
```
Ensure your input file (`input.txt`) is placed directly inside the `targil-2` folder.

Ensure you have `uv` installed on your machine.

### 1. Run the script
```bash
uv run main.py
```

### 2. Run with safety parameter
You can control the memory usage by providing a `--safety` parameter (between 0 and 1).
- `0.75` (default): Uses 0.75 of the 500MB RAM (375MB) to allow for overhead.
- `1.0`: Uses the full 500MB RAM.

```bash
uv run main.py --safety 0.8
```

---
