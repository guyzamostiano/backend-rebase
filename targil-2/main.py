import os
import time
from functools import lru_cache
from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED


@lru_cache(maxsize=100000)
def is_prime(number: int):
    if number <= 1:
        return False
    if number <= 3:
        return True
    if number % 2 == 0 or number % 3 == 0:
        return False
    i = 5
    while i * i <= number:
        if number % i == 0 or number % (i + 2) == 0:
            return False
        i += 6
    return True


def count_primes(numbers: list[int]) -> int:
    count = 0
    for number in numbers:
        if is_prime(number):
            count += 1
    return count


if __name__ == '__main__':
    prime_counter = 0
    cpu_count = os.cpu_count() or 2

    # Total RAM limit is 500MB
    ram_limit_bytes = 500 * 1024 * 1024

    # SAFETY MARGIN:
    # We must divide the 500MB RAM limit across all CPU cores.
    # safety factor for overhead: 2
    safety_factor = 2
    max_bytes_in_batch = (ram_limit_bytes // cpu_count) // safety_factor

    print(f"CPU Cores detected: {cpu_count}")
    print(f"Total RAM limit: 500 MB")
    print(f"Using: ProcessPoolExecutor (Multi-processing)")
    print(f"Batch size per core: {max_bytes_in_batch / (1024*1024):.2f} MB")
    print(f"----------------------\n")

    chunks_submitted = 0
    start_time = time.time()

    with open("nums_200_mil.txt", "r", encoding="ascii") as file:
        with ProcessPoolExecutor(max_workers=cpu_count) as executor:
            futures = set()
            print("Reading file and processing tasks...")

            while True:
                # Read a chunk from file
                lines = file.readlines(max_bytes_in_batch)
                if not lines:
                    break

                # Convert lines to integers
                numbers_list = [int(line.strip()) for line in lines if line.strip()]
                if numbers_list:
                    # Submit the task directly
                    future = executor.submit(count_primes, numbers_list)
                    futures.add(future)

                    chunks_submitted += 1
                    print(f"  > Submitted chunk {chunks_submitted}. Active tasks: {len(futures)}")

                    # Limit active tasks to cpu_count to stay within RAM
                    if len(futures) >= cpu_count:
                        wait_result = wait(futures, return_when=FIRST_COMPLETED)
                        for f in wait_result.done:
                            prime_counter += f.result()
                        futures = wait_result.not_done
                        print(f"  > Chunk finished. Remaining active: {len(futures)}")

            # Final collection
            print(f"File reading complete. Total chunks submitted: {chunks_submitted}")
            for f in as_completed(futures):
                prime_counter += f.result()

    print(f"\nSuccess! Total primes found: {prime_counter}")
    duration = time.time() - start_time
    print(f"Total time taken: {duration:.2f} seconds")