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
Ensure your input file (`nums_200_mil.txt`) is placed directly inside the `targil-2` folder.

Ensure you have `uv` installed on your machine.

### 1. Run the script
```bash
uv run main.py
```

---
