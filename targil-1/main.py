import heapq
import sys

min_heap =[]
file_number=0
file_names =[]
RAM_SIZE_IN_KB = 100000
LINE_SIZE_IN_KB = 1
SAFETY_FACTOR_FOR_OVERHEAD = 0.5

if len(sys.argv) > 1:
    try:
        passed_value = float(sys.argv[1])
        if 0 < passed_value <= 1.0:
            SAFETY_FACTOR_FOR_OVERHEAD = passed_value
        else:
            print("Error: SAFETY_FACTOR_FOR_OVERHEAD must be between 0 (exclusive) and 1.0 (inclusive). Using default 0.5.")
    except ValueError:
        print("Error: SAFETY_FACTOR_FOR_OVERHEAD must be a valid number. Using default 0.5.")


MAX_AMOUNT_OF_LINES_IN_MEMORY = int(RAM_SIZE_IN_KB/LINE_SIZE_IN_KB*SAFETY_FACTOR_FOR_OVERHEAD)

print(f"Running with SAFETY_FACTOR = {SAFETY_FACTOR_FOR_OVERHEAD}")
print(f"Max lines in memory: {MAX_AMOUNT_OF_LINES_IN_MEMORY}")

with open("input.txt", "r", encoding="ascii") as file:
    for count, line in enumerate(file, start=1):
        # remove \n for lexicographic sorting
        clean_line = line.rstrip("\n")
        heapq.heappush(min_heap, clean_line)
        #ignore overhead of calculation
        if count%MAX_AMOUNT_OF_LINES_IN_MEMORY ==0:
            file_name=f"output_{file_number}.txt"
            file_names.append(file_name)
            with open(file_name, "w", encoding="ascii") as output_file:
                while min_heap:
                    #append \n (we removed them before for sorting
                    output_file.write(heapq.heappop(min_heap) + "\n")
            file_number+=1

    if min_heap:
        file_name=f"output_{file_number}.txt"
        file_names.append(file_name)
        with open(file_name, "w", encoding="ascii") as output_file:
            while min_heap:
                #append \n (we removed them before for sorting
                output_file.write(heapq.heappop(min_heap) + "\n")


min_heap_runner = []
files: list = []
try:
    for name in file_names:
        files.append(open(name, "r", encoding="ascii"))

    for file_number, file in enumerate(files):
        first_line = file.readline()
        if first_line:
            clean_line = first_line.rstrip("\n")
            heapq.heappush(min_heap_runner, (clean_line, file_number))

    last_written_line = None
    with open("output.txt", "w", encoding="ascii") as output_file:
        while min_heap_runner:
            current_line, file_number = heapq.heappop(min_heap_runner)
            if current_line != last_written_line:
                output_file.write(current_line + "\n")
            last_written_line = current_line
            next_line = files[file_number].readline()
            if next_line:
                clean_line = next_line.rstrip("\n")
                heapq.heappush(min_heap_runner, (clean_line, file_number))
finally:
    for file in files:
        file.close()






